"""Evidence construction and deterministic span verification (Phase-2 §8.4).

The control that turns "the model says the policy quotes X" into "the page says X,
at these characters". A quote that cannot be located is **rejected**; it never
enters the trusted domain, and anything that depended on it is quarantined rather
than softened.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from ..errors import FabricatedQuote
from ..schema import (
    Document,
    DocRole,
    Evidence,
    ExtractionMeta,
    Page,
    VerifyMethod,
    sha256_text,
)


class PageIndex:
    """In-memory page store used by the deterministic core and the tests."""

    def __init__(self, pages: Iterable[Page] = ()) -> None:
        self._pages: dict[tuple[str, int], Page] = {}
        for p in pages:
            self.add(p)

    def add(self, page: Page) -> None:
        self._pages[(page.document_id, page.page_number)] = page

    def get(self, document_id: str, page_number: int) -> Page | None:
        return self._pages.get((document_id, page_number))

    def pages_of(self, document_id: str) -> tuple[Page, ...]:
        return tuple(sorted(
            (p for (d, _), p in self._pages.items() if d == document_id),
            key=lambda p: p.page_number,
        ))


# ---------------------------------------------------------------------------
# Text normalisation used for matching
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def normalise_for_match(text: str) -> str:
    """NFKC, unify quotes/dashes, collapse whitespace, fold case. Matching only."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = (s.replace("\u2018", "'").replace("\u2019", "'")
          .replace("\u201c", '"').replace("\u201d", '"')
          .replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-"))
    s = s.replace("\u00ad", "")  # soft hyphen
    s = "".join(ch for ch in s if ch == " " or unicodedata.category(ch)[0] != "C")
    s = _WS.sub(" ", s)
    return s.strip().casefold()


def _map_normalised_offsets(source: str, normalised: str) -> list[int]:
    """Map each index of ``normalised`` back to an index in ``source``.

    Built by walking the source character by character with the same
    transformations, so spans can be reported against the *original* page text.
    """
    mapping: list[int] = []
    out: list[str] = []
    prev_space = True
    for idx, ch in enumerate(source):
        c = unicodedata.normalize("NFKC", ch)
        if c in ("\u2018", "\u2019"):
            c = "'"
        elif c in ("\u201c", "\u201d"):
            c = '"'
        elif c in ("\u2013", "\u2014", "\u2212"):
            c = "-"
        if c == "\u00ad":
            continue
        if c == " " or unicodedata.category(c)[0] == "C":
            if prev_space:
                continue
            out.append(" ")
            mapping.append(idx)
            prev_space = True
            continue
        prev_space = False
        for sub in c:
            out.append(sub.casefold())
            mapping.append(idx)
    built = "".join(out).strip()
    # trim leading/trailing spaces introduced by strip()
    lead = len("".join(out)) - len("".join(out).lstrip())
    mapping = mapping[lead: lead + len(built)]
    return mapping


@dataclass(frozen=True)
class VerifyOutcome:
    method: VerifyMethod
    score: float
    char_start: int | None = None
    char_end: int | None = None
    matched_text: str | None = None
    note: str | None = None

    @property
    def accepted(self) -> bool:
        return self.method.accepted


_NUM_TOKEN_RE = re.compile(r"\d[\d,\.\u00a0 ]*\d|\d")
_OCR_DIGITS = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}


def numeric_signature(text: str) -> tuple[tuple[str, int], ...]:
    """Every number in the text, normalised, as a multiset.

    Indian grouping, decimals and OCR digit confusions are folded; nothing else is.
    Two spans with the same signature printed the same numbers and percentages.
    """
    counts: dict[str, int] = {}
    for m in _NUM_TOKEN_RE.finditer(text or ""):
        raw = m.group(0)
        cleaned = "".join(_OCR_DIGITS.get(ch, ch) for ch in raw)
        cleaned = cleaned.replace(",", "").replace("\u00a0", "").replace(" ", "")
        cleaned = cleaned.strip(".")
        if not cleaned:
            continue
        counts[cleaned] = counts.get(cleaned, 0) + 1
    return tuple(sorted(counts.items()))


def _align_window(window: str, quote: str) -> str:
    """Trim a sliding window down to the part actually aligned with the quote.

    The window is the quote's length, so its last characters can belong to the next
    line. Comparing numbers over the untrimmed window would refuse a legitimate
    match; comparing them over an untrimmed window and *accepting* would be worse.
    """
    sm = SequenceMatcher(None, window, quote, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return window
    return window[blocks[0].a: blocks[-1].a + blocks[-1].size]


def _numbers_differ(quote: str, window: str) -> str | None:
    q, w = numeric_signature(quote), numeric_signature(window)
    if q == w:
        return None
    return (f"numbers differ: quote has {q} but the matched span has {w}")


#: Words whose presence or absence changes what a sentence *does*, independently of how
#: similar the two strings look. Dropping a "not" costs four characters out of a hundred,
#: which no edit-distance threshold can be trusted to notice; the same is true of
#: "shall" -> "may". Load-bearing words are therefore compared as multisets and never
#: scored by similarity.
NEGATION_TOKENS = frozenset({
    "not", "no", "never", "none", "neither", "nor", "cannot", "without", "unless",
    "except", "excluding", "exclude", "excluded", "prohibit", "prohibited",
})
MODAL_TOKENS = frozenset({
    "shall", "must", "may", "should", "will", "would", "ought", "required", "permitted",
})


def load_bearing_signature(text: str) -> tuple[tuple[str, int], ...]:
    """Counts of negation and modality words, folded case, as a sorted multiset."""
    words = re.findall(r"[a-z]+", normalise_for_match(text or ""))
    counts: dict[str, int] = {}
    for word in words:
        if word in NEGATION_TOKENS or word in MODAL_TOKENS:
            counts[word] = counts.get(word, 0) + 1
    return tuple(sorted(counts.items()))


def _space_normalised(text: str, other: str) -> str:
    """``text`` rendered without its *spacing* differences from ``other``.

    A PDF text layer glues words together ("donot follow", "guidelineshall"); a faithful
    quotation restores the spaces. Neither changes a letter, so both are noise -- and the
    noise is removed on both sides of the comparison, symmetrically:

    * content that only ``text`` has is **kept** (it is a genuine difference)
    * content that only ``other`` has is **dropped** (it belongs to the other side)
    * whitespace is **shared**: either side's space is rendered as a space on both sides

    Comparing the load-bearing words of ``_space_normalised(page, quote)`` with those of
    ``_space_normalised(quote, page)`` therefore asks: *with spacing set aside, do the two
    texts make the same statement?* An omitted "not", an inserted "not", or a "shall"
    swapped for a "may" survives on its own side and is refused; the capture's missing
    spaces do not.
    """
    sm = SequenceMatcher(None, text, other, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        text_piece, other_piece = text[i1:i2], other[j1:j2]
        if tag == "equal":
            out.append(text_piece)
        elif tag == "insert":                       # only ``other`` has this piece
            out.append(" " * len(other_piece) if other_piece.strip() == "" else "")
        elif tag == "delete":                       # only ``text`` has this piece
            out.append(" " * len(text_piece) if text_piece.strip() == "" else text_piece)
        elif text_piece.replace(" ", "") == other_piece.replace(" ", ""):
            # same letters, different spacing: render the less glued of the two
            out.append(max((text_piece, other_piece), key=lambda piece: piece.count(" ")))
        else:
            out.append(text_piece)                  # a genuine difference is kept
    return "".join(out)


def _load_bearing_differ(quote: str, window: str) -> str | None:
    """Refuse a fuzzy match that changes a negation or a modal verb.

    "shall not recover" and "shall recover" are three characters apart and two different
    rules. Edit distance cannot be asked to notice; this comparison can.
    """
    quote_side = dict(load_bearing_signature(_space_normalised(quote, window)))
    window_side = dict(load_bearing_signature(_space_normalised(window, quote)))
    missing_from_window: dict[str, tuple[int, int]] = {}
    missing_from_quote: dict[str, tuple[int, int]] = {}
    for word in sorted(set(quote_side) | set(window_side)):
        q, w = quote_side.get(word, 0), window_side.get(word, 0)
        if q == w:
            continue
        if q > w:
            missing_from_window[word] = (q, w)
        else:
            missing_from_quote[word] = (w, q)
    if not missing_from_window and not missing_from_quote:
        return None
    detail = []
    if missing_from_window:
        detail.append("the quote has "
                      + ", ".join(f"{w} x{q} but the span x{s}"
                                  for w, (q, s) in missing_from_window.items()))
    if missing_from_quote:
        detail.append("the span has "
                      + ", ".join(f"{w} x{w_} but the quote x{q}"
                                  for w, (w_, q) in missing_from_quote.items()))
    return ("load-bearing words differ: " + "; ".join(detail) +
            " - a close match is not the same as the same statement")


def verify_quote(quote: str, page_text: str, *, fuzzy_threshold: float = 0.92,
                 min_fuzzy_len: int = 12) -> VerifyOutcome:
    """Locate ``quote`` inside ``page_text`` deterministically.

    1. exact substring on the raw text, accepted only if it occurs **once**
    2. exact substring after whitespace/case/quote normalisation (offsets mapped back),
       likewise accepted only if unique
    3. fuzzy window match, accepted only if the best window is unique **and** the
       numbers in the matched window are exactly the numbers in the quote
    4. otherwise REJECTED

    A quote that occurs twice is AMBIGUOUS, not a coin toss: choosing an occurrence
    is how a real span becomes a convenient one.
    """
    if not quote or not quote.strip():
        return VerifyOutcome(VerifyMethod.REJECTED, 0.0, note="empty quote")
    if page_text is None:
        return VerifyOutcome(VerifyMethod.REJECTED, 0.0, note="no page text")

    occurrences = page_text.count(quote)
    if occurrences > 1:
        return VerifyOutcome(VerifyMethod.AMBIGUOUS, 1.0, None, None, quote,
                             f"the quote appears {occurrences} times on this page; "
                             f"which occurrence it came from cannot be established")
    idx = page_text.find(quote)
    if idx >= 0:
        return VerifyOutcome(VerifyMethod.EXACT, 1.0, idx, idx + len(quote), quote)

    norm_page = normalise_for_match(page_text)
    norm_quote = normalise_for_match(quote)
    if norm_quote and norm_quote in norm_page:
        n_occ = norm_page.count(norm_quote)
        if n_occ > 1:
            return VerifyOutcome(VerifyMethod.AMBIGUOUS, 0.98, None, None, quote,
                                 f"the quote appears {n_occ} times on this page after "
                                 f"normalisation")
        mapping = _map_normalised_offsets(page_text, norm_page)
        start_n = norm_page.index(norm_quote)
        end_n = start_n + len(norm_quote)
        if mapping and end_n - 1 < len(mapping):
            start = mapping[start_n]
            end = mapping[end_n - 1] + 1
            return VerifyOutcome(VerifyMethod.NORMALISED, 0.98, start, end, page_text[start:end])
        # Offset mapping unavailable: accept the match but without offsets.
        return VerifyOutcome(VerifyMethod.NORMALISED, 0.9, None, None, quote, "offsets unavailable")

    if len(norm_quote) >= min_fuzzy_len:
        best = _best_fuzzy_windows(norm_page, norm_quote)
        if not best and len(norm_page) < len(norm_quote):
            # A *capture* can be shorter than the quote it contains: PDF text layers drop
            # inter-word spaces (and, occasionally, a character) that a faithful quotation
            # restores. When the page is shorter than the quote, there is exactly one
            # candidate window — the whole page — so score it, instead of concluding "not
            # present" from the arithmetic of lengths. The numeric-signature guard below
            # still applies: a window whose figures differ from the quote is refused.
            whole = SequenceMatcher(None, norm_page, norm_quote, autojunk=False).ratio()
            if whole >= 0.85:
                best = [(whole, 0, len(norm_page))]
        if best:
            score, start_n, end_n = best[0]
            unique = len(best) == 1 or (best[1][0] < score - 0.02)
            if score >= fuzzy_threshold and unique:
                window = _align_window(norm_page[start_n:end_n], norm_quote)
                mismatch = _numbers_differ(norm_quote, window) or \
                    _load_bearing_differ(quote, window)
                if mismatch:
                    # Close is not the same as equal, and neither a figure nor a negation
                    # is a matter of degree: a quote that turns "shall not recover" into
                    # "shall recover" is a different rule, whatever the edit distance.
                    return VerifyOutcome(VerifyMethod.REJECTED, score, None, None, None,
                                         f"fuzzy match refused - {mismatch}")
                mapping = _map_normalised_offsets(page_text, norm_page)
                if mapping and end_n - 1 < len(mapping):
                    start = mapping[start_n]
                    end = mapping[end_n - 1] + 1
                    return VerifyOutcome(VerifyMethod.FUZZY, score, start, end,
                                         page_text[start:end])
                return VerifyOutcome(VerifyMethod.FUZZY, score, None, None, None)
            if score >= fuzzy_threshold and not unique:
                return VerifyOutcome(VerifyMethod.AMBIGUOUS, score, note="multiple candidate windows")

    return VerifyOutcome(VerifyMethod.REJECTED, 0.0, note="no matching span on page")


def _best_fuzzy_windows(norm_page: str, norm_quote: str, top: int = 4) -> list[tuple[float, int, int]]:
    """Slide a window of the quote's length and score it. Deterministic and simple."""
    qlen = len(norm_quote)
    if qlen == 0 or len(norm_page) < qlen:
        return []
    step = max(1, qlen // 8)
    results: list[tuple[float, int, int]] = []
    for start in range(0, len(norm_page) - qlen + 1, step):
        window = norm_page[start:start + qlen]
        if abs(len(window) - qlen) > 0:
            continue
        ratio = SequenceMatcher(None, window, norm_quote, autojunk=False).ratio()
        if ratio >= 0.85:
            results.append((ratio, start, start + qlen))
    results.sort(key=lambda t: (-t[0], t[1]))
    return results[:top]


# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------
class EvidenceBuilder:
    """Builds Evidence objects against a page index and verifies every quote."""

    def __init__(self, pages: PageIndex, documents: Iterable[Document] = ()) -> None:
        self.pages = pages
        self._docs: dict[str, Document] = {d.document_id: d for d in documents}
        self._counter = 0

    def add_document(self, doc: Document) -> None:
        self._docs[doc.document_id] = doc

    def _next_id(self, document_id: str, page_number: int, quote: str,
                 start: int | None, end: int | None) -> str:
        """An id derived from the span, not from a running counter.

        Two facts anchored to the same characters are the same evidence, and an id
        that changes between runs would make the provenance chain unreproducible.
        """
        material = f"{document_id}|{page_number}|{start}|{end}|{quote}"
        return "EV-" + sha256_text(material)[:12].upper()

    def build(
        self,
        *,
        document_id: str,
        page_number: int,
        quote: str,
        extraction: ExtractionMeta | None = None,
        strict: bool = True,
    ) -> Evidence:
        page = self.pages.get(document_id, page_number)
        if page is None:
            raise FabricatedQuote(
                f"no such page {document_id}#{page_number}", document_id=document_id,
                page_number=page_number,
            )
        doc = self._docs.get(document_id)
        outcome = verify_quote(quote, page.text)
        flags = scan_for_injection(page.text, page.page_number)
        ev = Evidence(
            evidence_id=self._next_id(document_id, page_number, quote,
                                      outcome.char_start, outcome.char_end),
            document_id=document_id,
            document_role=doc.role if doc else DocRole.OTHER,
            page_number=page_number,
            quoted_text=quote,
            page_text_hash=sha256_text(page.text),
            char_start=outcome.char_start,
            char_end=outcome.char_end,
            verified=outcome.method,
            verify_score=outcome.score,
            extraction=extraction or ExtractionMeta(method="text_layer"),
            quality_score=page.quality_score,
            note=outcome.note,
            injection_flags=tuple(f"{f.pattern_name}@{f.char_start}" for f in flags),
        )
        if strict and not ev.is_trustworthy:
            raise FabricatedQuote(
                f"quote could not be verified on {document_id}#{page_number}: {quote!r}",
                document_id=document_id, page_number=page_number, quote=quote,
            )
        return ev

    def find_all(self, document_id: str, quote: str) -> tuple[Evidence, ...]:
        """Every verified occurrence of a quote across a document (for ambiguity work)."""
        out: list[Evidence] = []
        for page in self.pages.pages_of(document_id):
            outcome = verify_quote(quote, page.text)
            if outcome.accepted:
                out.append(self.build(document_id=document_id, page_number=page.page_number,
                                      quote=quote, strict=False))
        return tuple(out)


# ---------------------------------------------------------------------------
# Injection / instruction-like content detection (Phase-2 §26.3)
# ---------------------------------------------------------------------------
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore (all )?(previous|prior|above) (instructions|prompts|rules)", "ignore-instructions"),
    (r"disregard (the )?(above|previous|prior)", "disregard"),
    (r"you (are|must|should) (now )?(act as|behave as|respond as)", "role-override"),
    (r"\bsystem\s*:", "system-tag"),
    (r"\bassistant\s*:", "assistant-tag"),
    (r"mark (this|the) (claim|bill|deduction) (as )?(approved|payable|unlawful)", "outcome-instruction"),
    (r"do not (report|flag|deduct)", "suppression-instruction"),
    (r"\bprompt\s*injection\b", "meta"),
    (r"base64\s*:", "encoded-payload"),
    (r"<\|.*?\|>", "special-token"),
    (r"\boverride\b.*\b(verdict|rule|calculation)\b", "override"),
)

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


@dataclass(frozen=True)
class InjectionFlag:
    pattern_name: str
    matched_text: str
    char_start: int
    page_number: int


def scan_for_injection(page_text: str, page_number: int = 1) -> tuple[InjectionFlag, ...]:
    """Flag instruction-like content. Flagging never blocks a document by itself.

    The content is *always* treated as data; this exists so the case can show the
    user "this page contains a line that looks like an instruction to software —
    we ignored it", and so the pipeline can force dual reading on that page.
    """
    flags: list[InjectionFlag] = []
    if not page_text:
        return ()
    for pattern, name in INJECTION_PATTERNS:
        for m in re.finditer(pattern, page_text, flags=re.IGNORECASE | re.DOTALL):
            flags.append(InjectionFlag(name, m.group(0)[:120], m.start(), page_number))
    for m in _ZERO_WIDTH.finditer(page_text):
        flags.append(InjectionFlag("zero-width-char", repr(m.group(0)), m.start(), page_number))
    return tuple(flags)


def sanitise_untrusted(text: str) -> str:
    """Strip zero-width and bidi control characters from untrusted document text.

    This is a *presentation/safety* normalisation, not an interpretation: the text
    is still data, and the original bytes remain in storage.
    """
    return _ZERO_WIDTH.sub("", text or "")


def wrap_untrusted(text: str, *, role: str, page: int | None = None) -> str:
    """Delimit document text for any future prompt. Structural separation, §26.3."""
    page_attr = f' page="{page}"' if page is not None else ""
    return f'<document_text role="{role}"{page_attr}>\n{sanitise_untrusted(text)}\n</document_text>'
