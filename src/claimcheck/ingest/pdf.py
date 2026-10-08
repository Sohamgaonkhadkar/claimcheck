"""Deterministic PDF ingestion: text layer, geometry, rendering, OCR fallback (§3).

The order is fixed and is the whole point of the module:

    A. native text layer  (pypdfium2 character boxes — gives *both* text and coordinates)
    B. page rendering     (pypdfium2, deterministic scale)
    C. layout / block coordinates (derived from A, or from OCR word boxes when A is empty)
    D. OCR — only on pages where A is insufficient

No VLM. No fine-tuned OCR. No embeddings. The only model-shaped component is Tesseract,
which is a fixed, versioned engine invoked with a locked configuration; it is used exactly
where Phase 3C measured it to be necessary (98 of 1,012 pages had no text layer).

Two readers are produced per page so that load-bearing facts can be cross-checked (§11):

* **Reader A — ``layout``**: characters grouped into visual lines by their coordinates.
  Reading order is the order a human sees.
* **Reader B — ``native``**: the text layer in content-stream order (born-digital), or an
  independently rendered OCR pass at a different resolution (scanned). Reading order is the
  order the producer wrote, which on multi-column Indian bills is measurably different
  (Phase 3C measured order similarity 0.7155 between these two families of paths).

Neither reader is allowed to repair a number. Where they disagree the fact is quarantined.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass

from .model import (
    BBox,
    DocumentType,
    EditOp,
    PageAsset,
    PageExtraction,
    PageQuality,
    PiiStatus,
    TextLine,
    count_numeric_tokens,
    digit_density,
    scan_likelihood,
    table_likelihood,
    text_density,
)

# --------------------------------------------------------------------------- policy

#: A page whose text layer has fewer characters than this is treated as needing OCR.
#: Phase 3C used exactly this threshold (``extract.py``); changing it would silently move
#: the 98-page figure it measured.
MIN_TEXT_LAYER_CHARS = 12

#: Locked OCR configuration. ``--psm 6`` assumes one uniform block of text, which is what an
#: itemised bill is. Changing either value changes every number the pipeline reports.
OCR_SCALE = 2.0
OCR_SCALE_ALT = 3.0
OCR_PSM = 6


@dataclass(frozen=True)
class OcrPolicy:
    enabled: bool = True
    max_pages_per_document: int | None = None      # None = every page that needs it
    scale: float = OCR_SCALE
    alt_scale: float = OCR_SCALE_ALT
    psm: int = OCR_PSM

    def allows(self, page_index: int, ocr_done: int) -> bool:
        if not self.enabled:
            return False
        if self.max_pages_per_document is None:
            return True
        return ocr_done < self.max_pages_per_document


# --------------------------------------------------------------------------- geometry


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def chars_to_lines(chars: list[tuple[str, BBox]], page_number: int) -> list[TextLine]:
    """Group characters into visual lines using coordinates alone.

    Deterministic and layout-blind: characters are ordered top-to-bottom, then
    left-to-right, and cut into lines wherever the vertical centre moves further than the
    page's own median glyph height. No column model is assumed, because Indian bills put
    different numbers of columns on adjacent lines.
    """
    visible = [(c, b) for c, b in chars if c and (c.strip() or c == " ") and b.area >= 0]
    if not visible:
        return []
    heights = [b.height for _c, b in visible if b.height > 0]
    tol = max(1.2, 0.6 * _median(heights) or 1.2)
    widths = [b.width for _c, b in visible if 0 < b.width < 40]
    space_gap = max(1.5, 0.55 * (_median(widths) or 2.0))

    ordered = sorted(visible, key=lambda cb: (-(cb[1].y0 + cb[1].y1) / 2.0, cb[1].x0))
    groups: list[list[tuple[str, BBox]]] = []
    for ch, box in ordered:
        centre = (box.y0 + box.y1) / 2.0
        if groups:
            prev = groups[-1][-1][1]
            prev_centre = (prev.y0 + prev.y1) / 2.0
            if abs(centre - prev_centre) <= tol:
                groups[-1].append((ch, box))
                continue
        groups.append([(ch, box)])

    lines: list[TextLine] = []
    cursor = 0
    for gi, group in enumerate(groups, start=1):
        group.sort(key=lambda cb: cb[1].x0)
        parts: list[str] = []
        prev_box: BBox | None = None
        for ch, box in group:
            if prev_box is not None and (box.x0 - prev_box.x1) > space_gap and parts:
                parts.append(" ")
            parts.append(ch)
            prev_box = box
        text = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
        if not text:
            continue
        union = group[0][1]
        for _c, box in group[1:]:
            union = union.union(box)
        # newline sits between lines, so the offset of the next line is cursor + len + 1
        start = cursor
        end = start + len(text)
        lines.append(TextLine(text=text, bbox=union, page_number=page_number,
                              char_start=start, char_end=end, line_number=gi,
                              words=tuple(text.split())))
        cursor = end + 1
    return lines


def lines_to_text(lines: list[TextLine]) -> str:
    """Canonical page text: the joining of its lines with single newlines.

    Offsets in :class:`TextLine` are computed against exactly this string, so a span is
    always re-findable by ``page_text[char_start:char_end]``.
    """
    return "\n".join(l.text for l in lines)


def split_text_to_lines(text: str, page_number: int, width: float, height: float) -> list[TextLine]:
    """Line objects for a reader that has text but no coordinates.

    Bounding boxes are placed on a synthetic uniform grid so that a caller which expects
    geometry still gets something ordered and addressable — and the synthetic nature is
    disclosed through the ``ocr_confidence`` field being ``None`` while ``bbox`` spans the
    full page width. A synthetic box is never reported as a measured one.
    """
    raw = [l for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[TextLine] = []
    cursor = 0
    keep = [l for l in raw if l.strip()]
    n = max(1, len(keep))
    step = (height or 1.0) / n
    for i, line in enumerate(keep, start=1):
        start = cursor
        end = start + len(line)
        y1 = (height or 1.0) - (i - 1) * step
        out.append(TextLine(text=line, bbox=BBox(0.0, y1 - step, width or 1.0, y1),
                            page_number=page_number, char_start=start, char_end=end,
                            line_number=i, words=tuple(line.split())))
        cursor = end + 1
    return out


# --------------------------------------------------------------------------- ocr


def _render(page, scale: float):
    bitmap = page.render(scale=scale)
    return bitmap.to_pil()


def ocr_plain(image, *, lang: str = "eng", psm: int = OCR_PSM) -> str:
    """Deterministic plain-text OCR. Returns '' when the engine is unavailable."""
    try:
        import pytesseract
    except Exception:                                   # pragma: no cover - environment
        return ""
    return pytesseract.image_to_string(image, lang=lang, config=f"--psm {psm}")


def ocr_words(image, *, lang: str = "eng", psm: int = OCR_PSM):
    """OCR with word boxes. Returns ``(text, [(word, conf, left, top, w, h, key)], scale)``."""
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:                                   # pragma: no cover - environment
        return "", []
    data = pytesseract.image_to_data(image, lang=lang, config=f"--psm {psm}",
                                     output_type=Output.DICT)
    words = []
    n = len(data.get("text", []))
    for i in range(n):
        token = (data["text"][i] or "").strip()
        if not token:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        words.append((token, conf, float(data["left"][i]), float(data["top"][i]),
                      float(data["width"][i]), float(data["height"][i]), key))
    text = " ".join(w[0] for w in words)
    return text, words


def ocr_lines_from_words(words, page_number: int, *, image_height: float,
                         scale: float) -> list[TextLine]:
    """Turn Tesseract word boxes into the same :class:`TextLine` shape as the text layer.

    Image coordinates run top-down while PDF points run bottom-up, so the vertical axis is
    flipped here. This is the only place that conversion happens.
    """
    buckets: dict[tuple, list] = {}
    order: list[tuple] = []
    for w in words:
        key = w[6]
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(w)
    lines: list[TextLine] = []
    cursor = 0
    for i, key in enumerate(order, start=1):
        group = sorted(buckets[key], key=lambda w: w[2])
        text = " ".join(w[0] for w in group)
        if not text.strip():
            continue
        x0 = min(w[2] for w in group) / scale
        x1 = max(w[2] + w[4] for w in group) / scale
        top = min(w[3] for w in group) / scale
        bottom = max(w[3] + w[5] for w in group) / scale
        bbox = BBox(x0, image_height - bottom, x1, image_height - top)
        confs = [w[1] for w in group if w[1] >= 0]
        start = cursor
        end = start + len(text)
        lines.append(TextLine(text=text, bbox=bbox, page_number=page_number,
                              char_start=start, char_end=end, line_number=i,
                              words=tuple(text.split()),
                              ocr_confidence=(sum(confs) / len(confs) / 100.0) if confs else None))
        cursor = end + 1
    return lines


# --------------------------------------------------------------------------- page reader


@dataclass
class PageRead:
    """Everything one page produced, from both readers, plus its asset record."""

    asset: PageAsset
    layout: PageExtraction | None = None
    native: PageExtraction | None = None

    @property
    def readers(self) -> tuple[PageExtraction, ...]:
        return tuple(r for r in (self.layout, self.native) if r is not None)


def read_page(page, *, document_id: str, page_number: int, ocr: OcrPolicy,
              ocr_pages_done: int = 0, store_image: bool = False,
              storage=None) -> PageRead:
    """Read one page with both readers. Pure function of the page and the policy."""
    import pypdfium2 as pdfium  # noqa: F401  (imported for the reader type only)

    width, height = (float(v) for v in page.get_size())
    page_image_ref = None

    layer_chars: list[tuple[str, BBox]] = []
    try:
        textpage = page.get_textpage()
        for i in range(textpage.count_chars()):
            ch = textpage.get_text_range(i, 1)
            if not ch:
                continue
            if ch in ("\r", "\n"):
                continue
            box = textpage.get_charbox(i)
            layer_chars.append((ch, BBox(float(box[0]), float(box[1]), float(box[2]), float(box[3]))))
    except Exception:
        layer_chars = []

    layer_text_chars = sum(1 for c, _b in layer_chars if c.strip())
    text_layer_present = layer_text_chars >= MIN_TEXT_LAYER_CHARS
    ocr_required = not text_layer_present

    layout: PageExtraction | None = None
    native: PageExtraction | None = None
    used_ocr = False

    # ---- A: native text layer ----------------------------------------------------------
    if text_layer_present:
        layout_lines = chars_to_lines(layer_chars, page_number)
        layout = PageExtraction(
            document_id=document_id, page_number=page_number, reader="layout",
            method=EditOp.LAYOUT, text=lines_to_text(layout_lines),
            lines=tuple(layout_lines), width=width, height=height)
        native_text = _pypdf_native_text(page)
        native_lines = split_text_to_lines(native_text, page_number, width, height)
        native = PageExtraction(
            document_id=document_id, page_number=page_number, reader="native",
            method=EditOp.TEXT_LAYER, text="\n".join(l.text for l in native_lines),
            lines=tuple(native_lines), width=width, height=height)

    # ---- D: OCR, only where A was insufficient -----------------------------------------
    if ocr_required and ocr.allows(page_number, ocr_pages_done):
        image = _render(page, ocr.scale)
        words_text, words = ocr_words(image, psm=ocr.psm)
        if words:
            used_ocr = True
            ocr_lines = ocr_lines_from_words(words, page_number,
                                             image_height=image.height / ocr.scale,
                                             scale=ocr.scale)
            layout = PageExtraction(
                document_id=document_id, page_number=page_number, reader="layout",
                method=EditOp.OCR, text=lines_to_text(ocr_lines), lines=tuple(ocr_lines),
                width=width, height=height,
                mean_confidence=_mean_conf(ocr_lines))
        else:
            plain = ocr_plain(image, psm=ocr.psm)
            if plain.strip():
                used_ocr = True
                ocr_lines = split_text_to_lines(plain, page_number, width, height)
                layout = PageExtraction(
                    document_id=document_id, page_number=page_number, reader="layout",
                    method=EditOp.OCR, text="\n".join(l.text for l in ocr_lines),
                    lines=tuple(ocr_lines), width=width, height=height)
        # Reader B for a scanned page: the same engine at a different resolution. It is
        # *not* statistically independent of reader A, and the report says so; what it does
        # catch is resolution-dependent digit errors, which is precisely the failure class
        # Phase 3C measured on the real bill.
        try:
            alt_image = _render(page, ocr.alt_scale)
            alt_text = ocr_plain(alt_image, psm=ocr.psm)
        except Exception:
            alt_text = ""
        if alt_text.strip():
            alt_lines = split_text_to_lines(alt_text, page_number, width, height)
            native = PageExtraction(
                document_id=document_id, page_number=page_number, reader="native",
                method=EditOp.OCR, text="\n".join(l.text for l in alt_lines),
                lines=tuple(alt_lines), width=width, height=height)

    if store_image and storage is not None:
        try:
            image = _render(page, ocr.scale)
            key = f"pageimage/{document_id}/{page_number:04d}.png"
            buf = _png_bytes(image)
            storage.put(key, buf)
            page_image_ref = key
            image_count = _image_count(page)
        except Exception:
            page_image_ref = None
            image_count = _image_count(page)
    else:
        image_count = _image_count(page)

    working = layout or native
    text = working.text if working else ""
    lines = working.lines if working else ()
    area = max(1.0, width * height)
    image_area_ratio = _image_area_ratio(page, area)

    quality = PageQuality(
        text_density=text_density(text, width, height),
        digit_density=digit_density(text),
        image_ratio=image_area_ratio,
        scan_likelihood=scan_likelihood(
            text_layer_chars=layer_text_chars, image_count=image_count,
            image_ratio=image_area_ratio, width=width, height=height),
        flags=_quality_flags(text_layer_chars=layer_text_chars, text=text,
                             lines=lines, used_ocr=used_ocr),
    )
    asset = PageAsset(
        document_id=document_id, page_number=page_number, width=width, height=height,
        text_layer_present=text_layer_present, ocr_required=ocr_required,
        used_ocr=used_ocr, image_count=image_count, text_block_count=len(lines),
        char_count=len(text), numeric_token_count=count_numeric_tokens(text),
        table_likelihood=table_likelihood(lines),
        page_image_reference=page_image_ref,
        extraction_method=(working.method if working else EditOp.NONE),
        quality=quality)
    return PageRead(asset=asset, layout=layout, native=native)


def _mean_conf(lines: tuple[TextLine, ...]) -> float | None:
    confs = [l.ocr_confidence for l in lines if l.ocr_confidence is not None]
    return round(sum(confs) / len(confs), 4) if confs else None


def _quality_flags(*, text_layer_chars: int, text: str, lines: tuple[TextLine, ...],
                   used_ocr: bool) -> tuple[str, ...]:
    flags: list[str] = []
    if text_layer_chars < MIN_TEXT_LAYER_CHARS:
        flags.append("NO_TEXT_LAYER")
    if used_ocr:
        flags.append("OCR_APPLIED")
    if not text.strip():
        flags.append("NO_TEXT")
    low_conf = [l for l in lines if l.ocr_confidence is not None and l.ocr_confidence < 0.6]
    if low_conf:
        flags.append(f"LOW_OCR_CONFIDENCE_LINES:{len(low_conf)}")
    if any(re.search(r"\d,\d{2}(?!\d)", l.text) for l in lines):
        flags.append("DECIMAL_COMMA_CANDIDATE")     # the exact defect Phase 3C found
    if any(re.search(r"\d{6,}", l.text) for l in lines):
        flags.append("LONG_DIGIT_RUN")
    return tuple(flags)


def _pypdf_native_text(page) -> str:
    """Content-stream text for the same page, via the document's pypdf twin."""
    getter = getattr(page, "native_text", None)
    if callable(getter):
        return getter()
    return ""


def _png_bytes(image) -> bytes:
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _image_count(page) -> int:
    try:
        return sum(1 for obj in page.get_objects() if obj.type == 3)
    except Exception:
        return -1


def _image_area_ratio(page, page_area: float) -> float:
    total = 0.0
    try:
        for obj in page.get_objects():
            if obj.type != 3:
                continue
            try:
                left, bottom, right, top = obj.get_bounds()
            except Exception:
                continue
            total += max(0.0, float(right) - float(left)) * max(0.0, float(top) - float(bottom))
    except Exception:
        return 0.0
    return round(min(1.0, total / page_area), 6) if page_area else 0.0


# --------------------------------------------------------------------------- document


@dataclass
class PdfDocumentReader:
    """Opens a PDF and hands out per-page reads. Holds no state between pages."""

    path: pathlib.Path
    document_id: str
    ocr: OcrPolicy = OcrPolicy()
    storage: object | None = None
    store_page_images: bool = False

    def __post_init__(self) -> None:
        import pathlib as _pathlib

        import pypdfium2 as pdfium

        self.path = _pathlib.Path(self.path)
        self._doc = pdfium.PdfDocument(str(self.path))
        self._pdf = None
        try:                                            # parallel pypdf handle for reader B
            from pypdf import PdfReader

            self._pdf = PdfReader(str(self.path))
        except Exception:
            self._pdf = None

    @property
    def page_count(self) -> int:
        return len(self._doc)

    def raw_bytes(self) -> bytes:
        return self.path.read_bytes()

    def sha256(self) -> str:
        h = hashlib.sha256()
        with self.path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def read_pages(self, pages: list[int] | None = None) -> list[PageRead]:
        out: list[PageRead] = []
        ocr_done = 0
        indices = list(range(self.page_count)) if pages is None else pages
        for idx in indices:
            page = self._doc[idx]
            if self._pdf is not None and idx < len(self._pdf.pages):
                _attach_native_text(page, self._pdf.pages[idx])
            read = read_page(page, document_id=self.document_id, page_number=idx + 1,
                             ocr=self.ocr, ocr_pages_done=ocr_done,
                             store_image=self.store_page_images, storage=self.storage)
            if read.asset.used_ocr:
                ocr_done += 1
            out.append(read)
        return out

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfDocumentReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _attach_native_text(page, pypdf_page) -> None:
    """Bind reader B's text source to the pypdfium2 page object, lazily."""
    try:
        text = pypdf_page.extract_text() or ""
    except Exception:
        text = ""

    def native_text() -> str:
        return text

    try:
        page.native_text = native_text
    except Exception:
        pass


def classify_document(path: pathlib.Path, first_page_text: str = "") -> DocumentType:
    """Deterministic, explainable classification from the file name and first page.

    Returns ``UNKNOWN`` rather than guessing: a wrong type would choose the wrong parser.
    """
    name = path.name.lower()
    if "sample" in name and path.suffix.lower() == ".pdf":
        head = (first_page_text or "").lower()
        if any(k in head for k in ("bill", "invoice", "charge", "amount", "patient")):
            return DocumentType.HOSPITAL_BILL
    head = (first_page_text or "").lower()
    if "policy wording" in head or "policy wordings" in head or "uin" in head and "schedule" in head:
        return DocumentType.POLICY_WORDING
    if "master circular" in head or "regulations" in head or "irdai" in head:
        return DocumentType.REGULATION
    if "hospital bill" in head or "bill of supply" in head or "interim bill" in head:
        return DocumentType.HOSPITAL_BILL
    if "claim form" in head:
        return DocumentType.CLAIM_FORM
    return DocumentType.UNKNOWN


def pii_status_for(document_type: DocumentType, *, source_pii: PiiStatus = PiiStatus.UNKNOWN) -> PiiStatus:
    """A hospital bill is PII-bearing by nature; a regulation is not. State it, don't hope."""
    if document_type == DocumentType.HOSPITAL_BILL:
        return PiiStatus.PRESENT
    if document_type in (DocumentType.POLICY_WORDING, DocumentType.REGULATION):
        return PiiStatus.NONE_IDENTIFIED
    return source_pii
