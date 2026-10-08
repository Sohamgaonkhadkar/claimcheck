"""The PII-safe boundary (Phase 3D §16).

Raw document text from a real hospital bill is patient data. It must not reach standard
logs, test failure output, benchmark summaries or reports. This module is the single place
that decides what may cross that line, so the rule is enforced by code rather than by
every author remembering it.

What may cross:

* document id, document hash, page number
* span coordinates (character offsets and bounding box)
* a **redacted diagnostic reason** — a short, whitelisted description of *what kind* of
  content was seen, never the content
* counts, ratios and codes

What may not cross: any run of document text longer than :data:`MAX_LOG_CHARS`, any token
that looks like a personal identifier, and anything from a document whose
``pii_status`` is ``PRESENT`` other than the fields above.

Two mechanisms:

``redact_text``     — deterministic masking used when text must be *shown* in a diagnostic
                      (it is masked to structure, e.g. ``"<LABEL:12> Rs <MONEY>"``).
``SafeRecord`` / ``safe_log`` — the only sanctioned way to emit a line about a document.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .model import PiiStatus

#: No document text longer than this may appear in any log, report or error message.
MAX_LOG_CHARS = 64

#: Tokens that identify a person or a document. Anything matching one of these is
#: **hard**: it may never appear in a log, a report or a failing test.
#: A pattern that fires on its own metadata (for example an ISO timestamp in a run record)
#: does not belong here — see :data:`_SOFT_MARKERS`.
_ID_LIKE: tuple[tuple[str, str], ...] = (
    (r"\b[A-Z]{5}\d{4}[A-Z]\b", "PAN"),                       # ABCDE1234F
    (r"\b[2-9]\d{11}\b", "AADHAAR12"),
    (r"\b\d{4}\s?\d{4}\s?\d{4}\b", "AADHAAR_SPACED"),
    (r"\b[6-9]\d{9}\b", "MOBILE"),
    (r"\b[A-Z]{3}\d{7}\b", "PASSPORTISH"),
    (r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9]\b", "GSTIN"),
    (r"\b9\d{9}\b", "AADHAAR_ALT"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "EMAIL"),
    # a bill/serial number from an insurance or hospital system: two or three letters and a
    # run of digits (INT2043376, MPF252125). Uppercase only, so a content hash is not a hit.
    # A currency code is not a document prefix: "Rs 1000000" is money, and a detector that
    # calls it a document number reports a leak on every bill in the corpus.
    (r"\b(?![A-Za-z]{2}\b(?![-/#:])|Rs\b|INR\b|USD\b|EUR\b|GBP\b|MRP\b)"
     r"[A-Z]{2,4}[\s:/#-]?\d{6,12}\b", "DOCUMENT_NUMBER"),
    # a hospital UHID / MRN / IP number introduced by its own label
    (r"\b(?:UHID|MRN|MR\s?NO|IP\s?NO|REG(?:ISTRATION)?\s?NO)[\s:/#-]*[A-Z0-9]{4,14}\b",
     "PATIENT_NUMBER"),
)

#: Document content that is not an identifier on its own but must not be published either:
#: dates written the Indian day-first way, amounts, and printed labels. Kept separate from
#: :data:`_ID_LIKE` because a run record legitimately contains an ISO timestamp, and a
#: detection rule that fires on the tool's own metadata gets switched off by the people
#: who have to live with it.
_SOFT_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "DATE_DMY"),
    (r"\b\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\b", "DATE_DMONY"),
    (r"(?:Rs\.?|\u20b9)\s?\d[\d,]*\.?\d{0,2}", "MONEY"),
)

#: Column headers and field names on real Indian claim documents that introduce a person.
_PERSON_FIELD_HINTS: tuple[str, ...] = (
    "patient name", "name of patient", "insured name", "guardian", "father", "mother",
    "address", "uhid", "ip no", "mrn", "mobile", "phone", "aadhaar", "pan", "email",
)

_REDACTION_MAP: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _ID_LIKE)


class PiiLeak(RuntimeError):
    """Raised when something tries to emit unsafe text. Never caught silently."""


# --------------------------------------------------------------------------- detection


def find_markers(text: str) -> list[tuple[str, int, int]]:
    """Non-identifier document content: dates and money. Reportable, never publishable."""
    out: list[tuple[str, int, int]] = []
    for pattern, kind in _SOFT_MARKERS:
        for m in re.finditer(pattern, text or ""):
            out.append((kind, m.start(), m.end()))
    return sorted(out, key=lambda t: (t[1], t[2]))


#: A line that stores a digest is not a line that stores a person. Without this guard a
#: 12-character hash whose hex digits happen to be all decimal ("196573435217") is reported
#: as an Aadhaar number, and a privacy check that cries wolf is a privacy check people
#: switch off. Only the numeric patterns are excused on such a line — a name is still a name.
_HASH_CONTEXT = re.compile(r"(?:hash|sha256|sha16|digest)", re.IGNORECASE)
_NUMERIC_KINDS = frozenset({"AADHAAR12", "AADHAAR_SPACED", "AADHAAR_ALT", "MOBILE"})


#: A line that is nothing but one quoted lowercase-hex token is a digest array element:
#: ``"196573435217"``. JSON written with an indent puts array elements on their own line, so a
#: rule that only looks for the word "hash" on the same line misses them.
_QUOTED_HEX_LINE = re.compile(r'^\s*"[0-9a-f]{8,}"\s*,?\s*$')


def _hash_context_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        stripped = line.rstrip("\n")
        if _HASH_CONTEXT.search(stripped) or _QUOTED_HEX_LINE.match(stripped):
            spans.append((start, offset))
    return spans


def find_identifiers(text: str, *, include_field_hints: bool = True) -> list[tuple[str, int, int]]:
    """Every identifier-shaped span in ``text`` as ``(kind, start, end)``. Deterministic.

    ``include_field_hints`` separates two questions that look alike:

    * **is there a person's data in this text?** — the default, used when scanning a document
      or a log line, where ``Address :`` introduces somebody's address;
    * **does this record carry an identifier value?** — ``include_field_hints=False``, used when
      scanning our *own* artefacts, where ``address_basis`` is a field name and
      ``PAN card Copies (Not mandatory)`` is a policy heading, not a person.

    Hard identifier *values* are always checked either way.
    """
    text = text or ""
    hash_spans = _hash_context_spans(text)

    def in_hash_context(start: int) -> bool:
        return any(a <= start < b for a, b in hash_spans)

    out: list[tuple[str, int, int]] = []
    for rx, kind in _REDACTION_MAP:
        for m in rx.finditer(text):
            if kind in _NUMERIC_KINDS and in_hash_context(m.start()):
                continue
            out.append((kind, m.start(), m.end()))
    if not include_field_hints:
        return sorted(out, key=lambda t: (t[1], t[2]))
    for hint in _PERSON_FIELD_HINTS:
        # Word boundaries, because these hints are short: without them "pan" matches inside
        # "company" and "discrepancy", and the detector reports a leak that is not there.
        # A false alarm in a privacy check is not harmless — it trains people to ignore it.
        for m in re.finditer(rf"\b{re.escape(hint)}\b", text, re.IGNORECASE):
            out.append((f"FIELD:{hint}", m.start(), m.end()))
    return sorted(out, key=lambda t: (t[1], t[2]))


def looks_like_pii(text: str) -> bool:
    return bool(find_identifiers(text))


def classify_pii(text: str) -> PiiStatus:
    """Status from observed evidence, not from the document *type*."""
    hits = find_identifiers(text or "")
    kinds = {k for k, _s, _e in hits}
    hard = {k for k in kinds if k in {"PAN", "AADHAAR12", "AADHAAR_SPACED", "GSTIN", "EMAIL",
                                      "MOBILE"}}
    person_fields = {k for k in kinds if k.startswith("FIELD:") and k.split(":", 1)[1] in {
        "patient name", "name of patient", "insured name", "guardian", "father", "mother",
        "address", "uhid", "ip no", "mrn"}}
    if hard or person_fields:
        return PiiStatus.PRESENT
    if kinds:
        return PiiStatus.SUSPECTED
    return PiiStatus.NONE_IDENTIFIED


# --------------------------------------------------------------------------- redaction


@dataclass(frozen=True)
class Redacted:
    """A masked rendering that preserves *shape* and destroys *content*."""

    text: str
    kinds: tuple[str, ...] = ()
    source_chars: int = 0
    source_sha256_16: str = ""

    def __str__(self) -> str:
        return self.text


_MONEY = re.compile(r"(?:Rs\.?|\u20b9)?\s?\d[\d,]*\.?\d{0,2}")
_ALPHA_WORD = re.compile(r"[A-Za-z]{2,}")
_DIGITS = re.compile(r"\d+")


def redact_text(text: str, *, keep: int = 3, max_words: int = 6) -> Redacted:
    """Mask a string to structure for diagnostics.

    A label keeps its first few alphabetic characters and its length; money keeps its
    shape but loses its digits; identifiers vanish entirely. The result is enough to say
    *what kind of thing was there* and never enough to identify anyone.
    """
    src = text or ""
    masked = src
    kinds: list[str] = []
    for rx, kind in _REDACTION_MAP:
        def _sub(m: re.Match[str]) -> str:
            kinds.append(kind)
            return f"<{kind}>"
        masked = rx.sub(_sub, masked)

    for hint in _PERSON_FIELD_HINTS:
        if re.search(rf"\b{re.escape(hint)}\b", masked, re.IGNORECASE):
            kinds.append(f"FIELD:{hint}")
            masked = re.sub(rf"\b{re.escape(hint)}\b", f"<{hint.upper()}>", masked,
                            flags=re.IGNORECASE)

    def _word(m: re.Match[str]) -> str:
        w = m.group(0)
        if len(w) <= keep + 1:
            return w[0] + "\u2022" * max(0, len(w) - 1)
        return w[:keep] + "\u2022" * (len(w) - keep)

    masked = _ALPHA_WORD.sub(_word, masked)
    masked = _DIGITS.sub(lambda m: "\u2022" * len(m.group(0)), masked)
    parts = masked.split()
    if len(parts) > max_words:
        masked = " ".join(parts[:max_words]) + "\u2026"
    return Redacted(text=masked, kinds=tuple(sorted(set(kinds))), source_chars=len(src),
                    source_sha256_16=hashlib.sha256(src.encode("utf-8")).hexdigest()[:16])


#: Detail keys whose values are safe by construction: they are produced by this system
#: (span ids, clause ids, rule names, reader names) or are small integers. Values under any
#: other key are treated as document text and masked.
_SAFE_DETAIL_KEYS = frozenset({
    "span", "span_id", "clause", "clause_id", "code", "rule", "reader", "kind", "class",
    "page", "line", "offset", "start", "end", "sha16", "document", "document_id", "count",
    "reason_class", "field", "expected", "observed",
})


def redact_reason(kind: str, **detail: object) -> str:
    """A structured diagnostic reason. The only sanctioned way to describe a failure.

    ``redact_reason("AMOUNT_DECIMAL_COMMA", page=2, line=31)`` is safe to log and to show a
    user. It says what went wrong, not what the document said.

    Values under :data:`_SAFE_DETAIL_KEYS` pass through; every other string is masked, so
    ``redact_reason("x", label=document_text)`` cannot leak by accident either.
    """
    parts = [kind]
    for key in sorted(detail):
        value = detail[key]
        if isinstance(value, str) and key not in _SAFE_DETAIL_KEYS:
            value = redact_text(value, max_words=2).text
        parts.append(f"{key}={value}")
    return " ".join(parts)


# --------------------------------------------------------------------------- enforcement


@dataclass
class SafeRecord:
    """A record that has been checked before it can be emitted."""

    event: str
    document_id: str | None = None
    document_sha16: str | None = None
    page: int | None = None
    span: tuple[int, int] | None = None
    reason: str | None = None
    numbers: Mapping[str, float | int] = field(default_factory=dict)
    codes: tuple[str, ...] = ()
    pii_status: PiiStatus = PiiStatus.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"event": self.event}
        if self.document_id:
            d["document_id"] = self.document_id
        if self.document_sha16:
            d["document"] = self.document_sha16
        if self.page is not None:
            d["page"] = self.page
        if self.span is not None:
            d["span"] = [self.span[0], self.span[1]]
        if self.reason:
            d["reason"] = self.reason
        if self.numbers:
            d["numbers"] = {k: self.numbers[k] for k in sorted(self.numbers)}
        if self.codes:
            d["codes"] = list(self.codes)
        if self.pii_status is not PiiStatus.UNKNOWN:
            d["pii"] = self.pii_status.value
        return d

    def render(self) -> str:
        d = self.as_dict()
        return " ".join(f"{k}={v}" for k, v in d.items())


def assert_safe(payload: str) -> None:
    """Fail loudly rather than leak. Used by tests and by :func:`safe_log`."""
    if not payload:
        return
    ids = find_identifiers(payload)
    if ids:
        raise PiiLeak(f"identifier-shaped content in a log record: "
                      f"{sorted({k for k, _s, _e in ids})}")
    if len(payload) > 400:
        raise PiiLeak("log record too long to be a structured record "
                      f"({len(payload)} chars)")
    for word in re.findall(r"[A-Za-z]{3,}", payload):
        if len(word) > MAX_LOG_CHARS:
            raise PiiLeak("token longer than the logging limit")


def safe_log(logger: logging.Logger, record: SafeRecord) -> None:
    """Emit ``record`` on ``logger`` after proving it carries no PII."""
    payload = record.render()
    assert_safe(payload)
    logger.info("%s", payload)


def summarize_document(pii_status: PiiStatus, *, pages: int, ocr_pages: int,
                       chars: int) -> SafeRecord:
    """The standard, PII-free aggregate record for a document."""
    return SafeRecord(event="document.ingested", pii_status=pii_status,
                      numbers={"pages": pages, "ocr_pages": ocr_pages, "chars": chars})


def for_report(row: Mapping[str, Any], *, allow: Iterable[str]) -> dict[str, Any]:
    """Filter a result row down to reportable fields.

    Reports may contain identifiers' *hashes* and spans, never text. Anything not on the
    allow-list is dropped rather than trusted.
    """
    permitted = set(allow)
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in permitted:
            continue
        if isinstance(value, str):
            out[key] = redact_text(value, max_words=4).text
        else:
            out[key] = value
    return out
