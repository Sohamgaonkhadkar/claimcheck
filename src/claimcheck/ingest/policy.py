"""Policy-wording parsing: document -> page -> heading -> clause span (Phase 3D §5).

What this module does:

* cuts a wording into clauses at heading-shaped lines, using the **span** as the address;
* keeps, for every clause, the original text, a normalised form, its page, its heading
  path, its character span and the evidence record that locates it;
* reads product metadata (UIN, product name, effective dates) when the document states it.

What this module refuses to do:

* **invent policy language.** There is no generation here at all — a clause is a slice of
  the document or it does not exist;
* **rewrite clause text.** Normalisation produces a separate field; the original is kept
  byte-for-byte in ``original_text``;
* **force a common structure.** Different wordings number differently (``4.2``, ``Section
  6``, ``I``, ``Annexure 2``), and some have no numbered headings at all. A wording with no
  headings yields clauses cut at page level and says so, rather than being fitted to a
  template it does not have.

A clause never spans a page boundary in the record. Where a body obviously continues onto
the next page, the continuation is marked as such — the alternative, silently gluing two
pages into one span, would produce a quote that cannot be located on any single page.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..evidence.spans import normalise_for_match
from .addressing import (
    ClauseAddress,
    address_clause,
    find_headings,
)
from .model import EditOp, PageExtraction
from .records import PolicyClauseRecord, SourceEvidence

#: Minimum body size for a heading-to-heading slice to count as a clause. Below this it is
#: almost always a table row or a running header that happens to look like a heading.
MIN_CLAUSE_CHARS = 80

#: A body this long with a single heading is a page-level cut, not a designated clause.
PAGE_CUT_MIN_CHARS = 200

_UIN = re.compile(r"\b([A-Z]{2,5}HL[A-Z]{2,5}\d{5}V\d{6})\b")
_UIN_ALT = re.compile(r"\bUIN\s*[:\-]?\s*([A-Z0-9]{8,25})\b", re.IGNORECASE)
_PRODUCT_FROM_HEAD = re.compile(
    r"(?im)^[ \t]*(?:POLICY WORDINGS?|POLICY WORDING|POLICY DOCUMENT)[ \t]*[:\-]?[ \t]*([A-Z][A-Z0-9 &\-'/]{3,60})$")
_EFFECTIVE = re.compile(
    r"(?i)\b(?:effective|w\.e\.f\.?|with effect from|valid from)\b[^\n\d]{0,24}"
    r"(\d{1,2}[-/ ](?:[A-Za-z]{3,9}|\d{1,2})[-/ ]\d{2,4})")
_POLICY_PERIOD = re.compile(
    r"(?i)\bpolicy period\b[^\n\d]{0,40}(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})[^\n\d]{0,20}"
    r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")


@dataclass(frozen=True)
class PolicyMetadata:
    document_id: str
    uin: str | None = None
    product: str | None = None
    effective_text: str | None = None
    period_text: tuple[str, str] | None = None
    heading_lines: int = 0
    structure: str = "unknown"        # 'numbered' | 'sectioned' | 'roman' | 'page-cut'
    pages: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "uin": self.uin, "product": self.product,
                "effective_text": self.effective_text,
                "period_text": list(self.period_text) if self.period_text else None,
                "heading_lines": self.heading_lines, "structure": self.structure,
                "pages": self.pages}


@dataclass
class ParsedPolicy:
    document_id: str
    metadata: PolicyMetadata
    clauses: list[PolicyClauseRecord] = field(default_factory=list)
    addresses: list[ClauseAddress] = field(default_factory=list)
    page_text_sha: dict[int, str] = field(default_factory=dict)
    readers: tuple[str, ...] = ()

    def clause_text(self, clause_id: str) -> str | None:
        for c in self.clauses:
            if c.clause_id == clause_id:
                return c.original_text
        return None


def _structure_of(headings: list[Any]) -> str:
    labels = [h.label for h in headings]
    if not labels:
        return "page-cut"
    if all(re.fullmatch(r"[IVXLC]+", l) for l in labels):
        return "roman"
    if all(l.lower().startswith("section") for l in labels):
        return "sectioned"
    if any(re.match(r"\d", l) for l in labels):
        return "numbered"
    return "unknown"


def read_metadata(full_text: str, document_id: str, pages: int,
                  headings: list[Any]) -> PolicyMetadata:
    uin = None
    m = _UIN.search(full_text) or _UIN_ALT.search(full_text)
    if m:
        uin = m.group(1).strip().upper()
    product = None
    pm = _PRODUCT_FROM_HEAD.search(full_text[:6000])
    if pm:
        product = pm.group(1).strip()
    eff = _EFFECTIVE.search(full_text)
    period = _POLICY_PERIOD.search(full_text)
    return PolicyMetadata(
        document_id=document_id, uin=uin, product=product,
        effective_text=eff.group(1).strip() if eff else None,
        period_text=(period.group(1), period.group(2)) if period else None,
        heading_lines=len(headings), structure=_structure_of(headings), pages=pages)


def parse_policy(document_id: str, pages: Iterable[PageExtraction], *,
                 source_sha256: str, uin_hint: str | None = None) -> ParsedPolicy:
    """Cut a wording into clause records, page by page.

    ``pages`` must be the **layout** reader's output: clause spans are only meaningful
    against a page text whose offsets the reader that produced it also owns.
    """
    page_list = [p for p in pages if p is not None]
    full = "\n".join(p.text for p in page_list)
    all_headings = []
    for p in page_list:
        for h in find_headings(p.text):
            all_headings.append(h)
    meta = read_metadata(full, document_id, len(page_list), all_headings)
    if uin_hint and not meta.uin:
        meta = PolicyMetadata(**{**meta.__dict__, "uin": uin_hint})

    clauses: list[PolicyClauseRecord] = []
    addresses: list[ClauseAddress] = []
    page_sha: dict[int, str] = {}

    for page in page_list:
        text = page.text
        page_sha[page.page_number] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        headings = find_headings(text)
        if not headings:
            body = text.strip()
            if len(body) >= PAGE_CUT_MIN_CHARS:
                clauses.append(_make_clause(
                    document_id=document_id, page=page, text=body,
                    char_start=text.index(body), label=None, path=(), source_sha256=source_sha256,
                    page_sha=page_sha[page.page_number], meta=meta))
            continue
        for i, h in enumerate(headings):
            end = headings[i + 1].start if i + 1 < len(headings) else len(text)
            body = text[h.start:end].strip()
            if len(body) < MIN_CLAUSE_CHARS:
                continue
            path = tuple(x for x in (h.heading,) if x)
            clause = _make_clause(document_id=document_id, page=page, text=body,
                                  char_start=text.index(body), label=h.label, path=path,
                                  source_sha256=source_sha256,
                                  page_sha=page_sha[page.page_number], meta=meta)
            clause = _mark_continuation(clause, previous=clauses[-1] if clauses else None,
                                       has_heading=True)
            clauses.append(clause)

    for c in clauses:
        addresses.append(address_clause(
            document_id=c.document_id, page_number=c.page_number, char_start=c.char_start,
            char_end=c.char_end, text=c.original_text, label=c.heading_label,
            heading_path=c.heading_path))
    return ParsedPolicy(document_id=document_id, metadata=meta, clauses=clauses,
                        addresses=addresses, page_text_sha=page_sha,
                        readers=tuple(sorted({p.reader for p in page_list})))


def _make_clause(*, document_id: str, page: PageExtraction, text: str, char_start: int,
                 label: str | None, path: tuple[str, ...], source_sha256: str,
                 page_sha: str, meta: PolicyMetadata) -> PolicyClauseRecord:
    char_end = char_start + len(text)
    evidence = SourceEvidence(
        document_id=document_id, page_number=page.page_number, text=text,
        char_start=char_start, char_end=char_end, reader=page.reader,
        extraction_method=page.method,
        source_sha256=source_sha256, page_text_sha256=page_sha,
        bbox=None, bbox_is_measured=False)
    clause_id = "CL-" + hashlib.sha256(
        f"{document_id}|{page.page_number}|{char_start}|{char_end}|{text}".encode()
    ).hexdigest()[:20].upper()
    return PolicyClauseRecord(
        clause_id=clause_id, heading_path=path, heading_label=label,
        document_id=document_id, page_number=page.page_number, char_start=char_start,
        char_end=char_end, original_text=text,
        normalised_text=normalise_for_match(text), evidence=evidence,
        uin=meta.uin, product=meta.product, effective_date=meta.effective_text)


def _mark_continuation(clause: PolicyClauseRecord,
                       previous: PolicyClauseRecord | None,
                       has_heading: bool) -> PolicyClauseRecord:
    import dataclasses

    if previous is None or has_heading:
        return clause
    if clause.page_number == previous.page_number:
        return clause
    flags = ("POSSIBLE_CONTINUATION_OF_PREVIOUS_PAGE",)
    return dataclasses.replace(clause, heading_path=clause.heading_path + flags)


# --------------------------------------------------------------------------- summaries


def clause_stats(parsed: ParsedPolicy) -> dict[str, Any]:
    lengths = sorted(len(c.original_text) for c in parsed.clauses)
    return {
        "document_id": parsed.document_id,
        "uin": parsed.metadata.uin,
        "product": parsed.metadata.product,
        "structure": parsed.metadata.structure,
        "pages": parsed.metadata.pages,
        "clauses": len(parsed.clauses),
        "clauses_by_page": _counts(c.page_number for c in parsed.clauses),
        "clause_length_chars": _quantiles(lengths),
        "distinct_heading_labels": len({c.heading_label for c in parsed.clauses
                                        if c.heading_label}),
        "clauses_without_heading": sum(1 for c in parsed.clauses if not c.heading_label),
    }


def _counts(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0))


def _quantiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {"min": 0, "median": 0, "p90": 0, "max": 0}
    def q(frac: float) -> int:
        return values[min(len(values) - 1, int(frac * len(values)))]
    return {"min": values[0], "median": q(0.5), "p90": q(0.9), "max": values[-1]}
