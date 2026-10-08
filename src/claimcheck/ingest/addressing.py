"""Clause addressing: span-based identity (Phase 3D §14).

Phase 3C measured **267 heading-ID collisions** in 650 clauses segmented from 7 wordings.
The cause was that a clause's name was its printed heading: ``1``, ``I``, ``Section 6`` and
``4.2`` repeat many times inside one wording (a definitions list restarts at 1; an annexure
repeats the section numbering). Any system that treats a heading as an identity will
therefore silently merge distinct clauses — and a rule linked to "clause 4" of a policy
would then be linked to whichever clauses happen to share that label.

The fix is to make the **source span** the identity and demote the heading to a label:

    clause_id = "CL-" + H( document_id | page | char_start | char_end | text )

which gives exactly the properties the phase requires:

* same heading, same document, different span  -> **different** ids
* identical span                               -> **same** id, re-derived, not remembered
* modified source                              -> a different ``document_id`` (the content
  hash changed), so every clause id moves with it and the previous snapshot stays
  addressable

The last point matters for provenance: an id that stayed stable across an edit would let a
verdict cite text that is no longer there.

Existing rule -> clause links are **not broken**. The corpus layer's declared clause ids
(``"<corpus_document_id>#<label>"``) are human labels attached to hand-curated text; they
keep working, and :func:`span_address_for_declared_clause` gives such a clause a span
address when one can be established and says so explicitly when one cannot. A declared id is
never silently promoted to an address.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .model import EditOp
from .records import SourceEvidence, span_fingerprint

#: A heading is only ever a *label*. These patterns are used to read one, not to identify.
HEADING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^[ \t]*(?P<label>\d{1,2}(?:\.\d{1,2}){0,3})[.)]?[ \t]+(?P<head>[A-Z][^\n]{2,110})$"),
    re.compile(r"(?m)^[ \t]*(?P<label>Section[ \t]+\d{1,2})[.:]?[ \t]+(?P<head>[^\n]{2,110})$"),
    re.compile(r"(?m)^[ \t]*(?P<label>[IVXLC]{1,5})[.)]?[ \t]+(?P<head>[A-Z][^\n]{2,110})$"),
)


@dataclass(frozen=True)
class ClauseAddress:
    """The canonical identity of a clause, and its human label kept separate."""

    clause_id: str
    document_id: str
    page_number: int
    char_start: int
    char_end: int
    span_id: str
    label: str | None
    heading_path: tuple[str, ...]
    text_sha16: str
    located: bool                  # False for a declared-but-unlocatable clause

    def as_dict(self) -> dict[str, Any]:
        return {"clause_id": self.clause_id, "document_id": self.document_id,
                "page": self.page_number, "char_start": self.char_start,
                "char_end": self.char_end, "span_id": self.span_id, "label": self.label,
                "heading_path": list(self.heading_path), "text_sha16": self.text_sha16,
                "located": self.located}


def clause_id_for(document_id: str, page_number: int, char_start: int, char_end: int,
                  text: str) -> str:
    """Deterministic, span-derived clause id. The only identity a clause has."""
    span = span_fingerprint(document_id, page_number, char_start, char_end, text)
    return "CL-" + span[:20].upper()


def address_clause(*, document_id: str, page_number: int, char_start: int, char_end: int,
                   text: str, label: str | None = None,
                   heading_path: Iterable[str] = (), span_id: str | None = None) -> ClauseAddress:
    return ClauseAddress(
        clause_id=clause_id_for(document_id, page_number, char_start, char_end, text),
        document_id=document_id, page_number=page_number, char_start=char_start,
        char_end=char_end,
        span_id=span_id or ("SP-" + span_fingerprint(
            document_id, page_number, char_start, char_end, text)[:16].upper()),
        label=label, heading_path=tuple(heading_path),
        text_sha16=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        located=True)


def declared_clause_address(corpus_document_id: str, label: str, text: str,
                            char_start: int, char_end: int,
                            page_number: int | None = None,
                            heading_path: str | None = None) -> ClauseAddress:
    """Address a corpus clause that *declares* its own id.

    The declared id is preserved as the human ``label`` and remains the string the rule pack
    links to; the address adds the span identity alongside it. When no span is known the
    address is marked ``located=False`` and the derived id is scoped to the declared id —
    so it is stable, and visibly weaker than a measured span.
    """
    if page_number is None or char_end <= char_start:
        material = f"declared|{corpus_document_id}|{label}|{text}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return ClauseAddress(
            clause_id="CL-DECL-" + digest[:20].upper(), document_id=corpus_document_id,
            page_number=page_number if page_number is not None else 0,
            char_start=char_start, char_end=char_end,
            span_id="SP-DECL-" + digest[:16].upper(), label=label,
            heading_path=(heading_path or "",), text_sha16=digest[:16], located=False)
    return address_clause(document_id=corpus_document_id, page_number=page_number,
                          char_start=char_start, char_end=char_end, text=text, label=label,
                          heading_path=(heading_path or "",))


# --------------------------------------------------------------------------- collisions


@dataclass(frozen=True)
class CollisionReport:
    clauses: int
    distinct_labels: int
    label_collisions: int
    distinct_ids: int
    id_collisions: int
    worst_labels: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"clauses": self.clauses, "distinct_labels": self.distinct_labels,
                "label_collisions": self.label_collisions,
                "distinct_ids": self.distinct_ids, "id_collisions": self.id_collisions,
                "worst_labels": [{"label": l, "count": c} for l, c in self.worst_labels]}


def collision_report(addresses: Iterable[ClauseAddress]) -> CollisionReport:
    """Count how many clauses *would* have collided under heading-based identity.

    Reported as a before/after pair: ``label_collisions`` is the pre-fix number for the
    corpus at hand, ``id_collisions`` is what the span addressing produces (always 0 unless
    two clauses genuinely share one span, which would mean they *are* the same clause).
    """
    items = [a for a in addresses if a.located]
    labels: dict[str, int] = {}
    for a in items:
        key = a.label or ""
        labels[key] = labels.get(key, 0) + 1
    ids = {a.clause_id for a in items}
    collisions = sum(c - 1 for c in labels.values() if c > 1)
    return CollisionReport(
        clauses=len(items), distinct_labels=len(labels), label_collisions=collisions,
        distinct_ids=len(ids), id_collisions=len(items) - len(ids),
        worst_labels=tuple(sorted(((l, c) for l, c in labels.items() if c > 1),
                                  key=lambda t: (-t[1], t[0]))[:10]))


# --------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class HeadingHit:
    label: str
    heading: str
    start: int
    end: int


def find_headings(text: str) -> list[HeadingHit]:
    """Every heading-shaped line, by position. Used to cut a page into clause bodies."""
    hits: dict[int, HeadingHit] = {}
    for pattern in HEADING_PATTERNS:
        for m in pattern.finditer(text or ""):
            hits.setdefault(m.start(), HeadingHit(label=m.group("label").strip(),
                                                  heading=m.group("head").strip(),
                                                  start=m.start(), end=m.end()))
    return [hits[k] for k in sorted(hits)]


def evidence_for_span(*, document_id: str, page_number: int, text: str, char_start: int,
                      char_end: int, reader: str, method: EditOp, source_sha256: str,
                      page_text_sha256: str, bbox=None, bbox_is_measured: bool = False,
                      ocr_confidence: float | None = None) -> SourceEvidence:
    return SourceEvidence(
        document_id=document_id, page_number=page_number, text=text,
        char_start=char_start, char_end=char_end, reader=reader,
        extraction_method=method, source_sha256=source_sha256,
        page_text_sha256=page_text_sha256, bbox=bbox,
        bbox_is_measured=bbox_is_measured, ocr_confidence=ocr_confidence)
