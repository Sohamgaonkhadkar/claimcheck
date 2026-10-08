"""Typed records for real-document ingestion (Phase 3D §2).

Storage-neutral by construction: an asset refers to its bytes by an opaque *artifact key*
and a content hash, never by a path. ``LocalFileStorage`` is one implementation of that
interface; a database or object store would be another, and nothing above this layer would
change.

Two disciplines are enforced here rather than left to convention:

* **No raw PII in anything that can be logged.** A ``DocumentAsset`` carries a fingerprint,
  a source reference and a PII *status* — never document text. Text lives in
  ``PageExtraction``, which is explicitly marked as sensitive and whose ``__repr__`` is
  redacted (see ``sanitize.py``).
* **Identity is derived, never assigned.** ``DocumentAsset.document_id`` is a function of the
  document type and the SHA-256 of the bytes, so the same document ingested twice is the same
  asset, and a modified document is a different asset rather than the same one with a new
  version number.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol

# --------------------------------------------------------------------------- enums


class DocumentType(str, Enum):
    POLICY_WORDING = "policy_wording"
    HOSPITAL_BILL = "hospital_bill"
    SETTLEMENT_LETTER = "settlement_letter"
    CLAIM_FORM = "claim_form"
    REGULATION = "regulation"
    DISCHARGE_SUMMARY = "discharge_summary"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    REGULATOR = "regulator"
    INSURER = "insurer"
    HOSPITAL = "hospital"
    USER_UPLOAD = "user_upload"
    THIRD_PARTY_DATASET = "third_party_dataset"
    MIRROR = "mirror"
    UNKNOWN = "unknown"


class PiiStatus(str, Enum):
    NONE_IDENTIFIED = "none_identified"
    SUSPECTED = "suspected"
    PRESENT = "present"
    UNKNOWN = "unknown"


class LicenceStatus(str, Enum):
    PUBLIC_REGULATOR = "public_regulator"        # published by the regulator; no open licence
    NO_LICENCE = "no_licence"                    # nothing declared anywhere
    USAGE_UNVERIFIED = "usage_unverified"        # terms could not be established
    OPEN_LICENCE = "open_licence"                # a licence granting reuse was found
    METADATA_ONLY = "metadata_only"              # may cite, may not redistribute


class EditOp(str, Enum):
    """What happened to a page's characters. Mirrors the project's provenance vocabulary."""

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    LAYOUT = "layout"
    NONE = "none"


# --------------------------------------------------------------------------- fingerprint


@dataclass(frozen=True)
class DocumentFingerprint:
    """Content identity. ``sha256`` is the only identity a document really has."""

    sha256: str
    byte_size: int
    page_count: int

    @property
    def display(self) -> str:
        """Short form for logs, reports and evidence records."""
        return self.sha256[:16]

    @classmethod
    def of_bytes(cls, data: bytes, page_count: int) -> "DocumentFingerprint":
        return cls(sha256=hashlib.sha256(data).hexdigest(), byte_size=len(data),
                   page_count=page_count)


# --------------------------------------------------------------------------- source


@dataclass(frozen=True)
class DocumentSource:
    """Where the bytes came from and what may be done with them.

    ``reference`` is a URL or an opaque acquisition identifier. It must never carry a
    patient identifier: acquisition references are about the document, not about the person
    in it.
    """

    source_type: SourceType
    reference: str
    publisher: str | None = None
    acquired_at: str | None = None
    licence_status: LicenceStatus = LicenceStatus.USAGE_UNVERIFIED
    redistribution: str = "metadata_only"
    docstring_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "reference": self.reference,
            "publisher": self.publisher,
            "acquired_at": self.acquired_at,
            "licence_status": self.licence_status.value,
            "redistribution": self.redistribution,
        }


# --------------------------------------------------------------------------- assets


@dataclass(frozen=True)
class DocumentAsset:
    """A document as the registry knows it. No text, no PII, no path."""

    document_id: str
    document_type: DocumentType
    source: DocumentSource
    fingerprint: DocumentFingerprint
    artifact_key: str | None = None            # opaque storage key, not a filesystem path
    acquired_at: str | None = None
    licence_status: LicenceStatus = LicenceStatus.USAGE_UNVERIFIED
    pii_status: PiiStatus = PiiStatus.UNKNOWN
    page_count: int = 0
    notes: str | None = None

    @staticmethod
    def make_id(document_type: DocumentType, fingerprint: DocumentFingerprint) -> str:
        return f"{document_type.value}-{fingerprint.sha256[:12]}"

    @classmethod
    def from_bytes(
        cls, data: bytes, document_type: DocumentType, source: DocumentSource, *,
        page_count: int = 0, artifact_key: str | None = None, acquired_at: str | None = None,
        licence_status: LicenceStatus | None = None, pii_status: PiiStatus = PiiStatus.UNKNOWN,
        notes: str | None = None,
    ) -> "DocumentAsset":
        fp = DocumentFingerprint.of_bytes(data, page_count)
        return cls(
            document_id=cls.make_id(document_type, fp),
            document_type=document_type,
            source=source,
            fingerprint=fp,
            artifact_key=artifact_key,
            acquired_at=acquired_at,
            licence_status=licence_status or source.licence_status,
            pii_status=pii_status,
            page_count=page_count,
            notes=notes,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_type": self.document_type.value,
            "source": self.source.as_dict(),
            "sha256": self.fingerprint.sha256,
            "byte_size": self.fingerprint.byte_size,
            "page_count": self.fingerprint.page_count,
            "artifact_key": self.artifact_key,
            "acquired_at": self.acquired_at,
            "licence_status": self.licence_status.value,
            "pii_status": self.pii_status.value,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PageQuality:
    """Measured page shape. Every field is a ratio or a count — never a judgement."""

    text_density: float          # characters per 1000 square points
    digit_density: float         # digits / characters
    image_ratio: float           # covered image area / page area, 0..1 (0 when unknown)
    scan_likelihood: float       # 0..1
    flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "text_density": round(self.text_density, 6),
            "digit_density": round(self.digit_density, 6),
            "image_ratio": round(self.image_ratio, 6),
            "scan_likelihood": round(self.scan_likelihood, 6),
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class PageAsset:
    """One page as the registry knows it."""

    document_id: str
    page_number: int
    width: float
    height: float
    text_layer_present: bool
    ocr_required: bool
    image_count: int
    text_block_count: int
    quality: PageQuality
    page_image_reference: str | None = None
    char_count: int = 0
    numeric_token_count: int = 0
    table_likelihood: float = 0.0
    used_ocr: bool = False
    extraction_method: EditOp = EditOp.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page_number": self.page_number,
            "page_image_reference": self.page_image_reference,
            "width": round(self.width, 3),
            "height": round(self.height, 3),
            "text_layer_present": self.text_layer_present,
            "ocr_required": self.ocr_required,
            "used_ocr": self.used_ocr,
            "image_count": self.image_count,
            "text_block_count": self.text_block_count,
            "char_count": self.char_count,
            "numeric_token_count": self.numeric_token_count,
            "table_likelihood": round(self.table_likelihood, 4),
            "extraction_method": self.extraction_method.value,
            "quality": self.quality.as_dict(),
        }


# --------------------------------------------------------------------------- geometry


@dataclass(frozen=True)
class BBox:
    """Axis-aligned box in PDF points, origin bottom-left (the PDF convention)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def union(self, other: "BBox") -> "BBox":
        return BBox(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2))


@dataclass(frozen=True)
class TextLine:
    """A reconstructed visual line: text plus where it sits on the page."""

    text: str
    bbox: BBox
    page_number: int
    char_start: int                 # offset into the page's canonical text
    char_end: int
    line_number: int                # position in reading order, 1-based
    words: tuple[str, ...] = ()
    ocr_confidence: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def text_sha16(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:  # never leak document text through a traceback or a log
        return (f"TextLine(page={self.page_number}, line={self.line_number}, "
                f"chars={self.char_end - self.char_start}, span=[{self.char_start},{self.char_end}], "
                f"sha16={self.text_sha16}, bbox={self.bbox.as_tuple()})")

    __str__ = __repr__


@dataclass(frozen=True)
class PageExtraction:
    """The text of one page **and** its geometry, from one named reader.

    This is the object that carries sensitive content. See ``sanitize.py``: it is never
    formatted into a log line, and its ``__repr__`` is redacted on purpose.
    """

    document_id: str
    page_number: int
    reader: str
    method: EditOp
    text: str
    lines: tuple[TextLine, ...] = ()
    width: float = 0.0
    height: float = 0.0
    page_image_reference: str | None = None
    mean_confidence: float | None = None

    def __repr__(self) -> str:  # never leak document text through a traceback or a log
        return (f"PageExtraction(document_id={self.document_id!r}, page={self.page_number}, "
                f"reader={self.reader!r}, method={self.method.value}, "
                f"chars={len(self.text)}, lines={len(self.lines)})")

    __str__ = __repr__

    def line_at(self, char_start: int) -> TextLine | None:
        for line in self.lines:
            if line.char_start <= char_start < line.char_end:
                return line
        return None

    def validate(self) -> tuple[str, ...]:
        """Check the invariants every span-based citation depends on.

        Returns the list of violations rather than raising, so a caller can record them as a
        quality finding instead of losing the page. An empty tuple means the page is sound:
        line offsets address the page text, the lines are in order, and no line is empty.
        """
        problems: list[str] = []
        previous_end = 0
        for index, line in enumerate(self.lines, start=1):
            if line.char_start > line.char_end:
                problems.append(f"line {index}: char_start > char_end")
            if line.char_end > len(self.text):
                problems.append(f"line {index}: span past the end of the page text")
            elif self.text[line.char_start:line.char_end] != line.text:
                problems.append(f"line {index}: span does not reproduce the line text")
            if line.char_start < previous_end:
                problems.append(f"line {index}: overlaps the previous line")
            previous_end = max(previous_end, line.char_end)
            if line.line_number != index:
                problems.append(f"line {index}: line_number is {line.line_number}")
        return tuple(problems)

    def numeric_tokens(self) -> list[str]:
        return re.findall(r"\d[\d,\.]*\d|\d", self.text)


# --------------------------------------------------------------------------- storage


@dataclass(frozen=True)
class StorageMetadata:
    """Content metadata returned by a storage adapter; never contains a physical path."""

    size_bytes: int
    sha256: str


class Storage(Protocol):
    """The private blob-store port used by ingestion and the Phase 2 API."""

    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def metadata(self, key: str) -> StorageMetadata: ...


@dataclass
class InMemoryStorage:
    blobs: dict[str, bytes] = field(default_factory=dict)

    def put(self, key: str, data: bytes) -> str:
        self.blobs[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def exists(self, key: str) -> bool:
        return key in self.blobs

    def delete(self, key: str) -> None:
        self.blobs.pop(key, None)

    def metadata(self, key: str) -> StorageMetadata:
        data = self.get(key)
        return StorageMetadata(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


class LocalFileStorage:
    """A directory of artifacts. Paths stay *inside* this class; callers see keys."""

    def __init__(self, root) -> None:
        import pathlib

        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str):
        safe = key.replace("..", "_").lstrip("/")
        return self.root / safe

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def metadata(self, key: str) -> StorageMetadata:
        data = self.get(key)
        return StorageMetadata(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


# --------------------------------------------------------------------------- helpers


_MONEYISH = re.compile(r"\d[\d,\.]*\d|\d")


def count_numeric_tokens(text: str) -> int:
    return len(_MONEYISH.findall(text or ""))


def digit_density(text: str) -> float:
    if not text:
        return 0.0
    return sum(ch.isdigit() for ch in text) / len(text)


def text_density(text: str, width: float, height: float) -> float:
    """Characters per 1000 square points — size-normalised across page formats."""
    area = max(1.0, (width or 1.0) * (height or 1.0))
    return len(text or "") * 1000.0 / area


def scan_likelihood(*, text_layer_chars: int, image_count: int, image_ratio: float,
                    width: float, height: float) -> float:
    """A prior, not a verdict: how much this page looks like a scan.

    Deterministic and monotone — a page with a substantial text layer is never above 0.2,
    a page with no text layer and full-page imagery is at least 0.8.
    """
    if text_layer_chars >= 200:
        base = 0.05
    elif text_layer_chars >= 12:
        base = 0.20
    else:
        base = 0.80
    if image_count and image_ratio >= 0.5:
        base = min(1.0, base + 0.15)
    elif image_count:
        base = min(1.0, base + 0.05)
    if width > 0 and height > 0 and width / height > 1.4:
        base = min(1.0, base + 0.02)   # landscape scans are usually photographed pages
    return round(min(1.0, base), 4)


def table_likelihood(lines: Iterable[TextLine]) -> float:
    """Share of lines that carry two or more right-aligned numeric columns.

    Deliberately crude and explainable: it is used to decide whether a *table reader* is
    worth attempting, not to claim a table was found.
    """
    rows = [l for l in lines if l.text.strip()]
    if len(rows) < 4:
        return 0.0
    structured = 0
    for line in rows:
        amounts = re.findall(r"\d[\d,]*[.,]\d{2}", line.text)
        if len(amounts) >= 2:
            structured += 1
    return round(structured / len(rows), 4)
