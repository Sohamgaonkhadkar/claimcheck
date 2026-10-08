"""Extraction records: what was read, where it came from, and how sure the readers are.

These structures describe **text and numbers found in documents**. They deliberately
cannot express an outcome: there is no field here for SUPPORTED, POTENTIALLY INCONSISTENT
or UNDETERMINED, and no arithmetic. Conclusions remain the job of
``claimcheck.rules`` / ``claimcheck.graph`` / ``claimcheck.verdict``, which is the only
place allowed to produce a financial judgement (Phase 3D §10).

The canonical address of anything extracted is its **source span**: document + page +
character offsets, with the page text hash and the document hash behind it. Identifiers are
derived from that span, so two clauses that share a heading are still two clauses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .model import BBox, EditOp


# --------------------------------------------------------------------------- evidence


def span_fingerprint(document_id: str, page_number: int, char_start: int, char_end: int,
                     text: str) -> str:
    """Deterministic identity of a source span. The *only* clause/line identity."""
    material = f"{document_id}|{page_number}|{char_start}|{char_end}|{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceEvidence:
    """A pointer into a real document. Everything needed to re-find the text by machine."""

    document_id: str
    page_number: int
    text: str
    char_start: int
    char_end: int
    reader: str                             # 'layout' | 'native'
    extraction_method: EditOp
    source_sha256: str                      # the document's hash: provenance anchor
    page_text_sha256: str
    bbox: BBox | None = None
    bbox_is_measured: bool = False
    ocr_confidence: float | None = None
    span_id: str = ""

    def __post_init__(self) -> None:
        if not self.span_id:
            object.__setattr__(self, "span_id", "SP-" + span_fingerprint(
                self.document_id, self.page_number, self.char_start, self.char_end,
                self.text)[:16].upper())

    # -- the bridge to the existing, unchanged evidence system -------------------------
    def as_quote_request(self) -> dict[str, Any]:
        """Exactly the arguments ``evidence.EvidenceBuilder.build`` needs.

        The ingest layer does not build a second evidence system; it feeds the existing
        one. The quote is verified there against the same page text, so a span that this
        layer produced cannot become trusted unless the core can re-find it.
        """
        return {"document_id": self.document_id, "page_number": self.page_number,
                "quote": self.text}

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "document_id": self.document_id,
            "page": self.page_number,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "reader": self.reader,
            "method": self.extraction_method.value,
            "source_sha256": self.source_sha256[:16],
            "page_text_sha256": self.page_text_sha256[:16],
            "bbox": self.bbox.as_tuple() if self.bbox else None,
            "bbox_is_measured": self.bbox_is_measured,
            "ocr_confidence": self.ocr_confidence,
            "text_preview": _preview(self.text),
        }


def _preview(text: str, limit: int = 48) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "\u2026"


# --------------------------------------------------------------------------- money


class MoneyFormat(str, Enum):
    PLAIN = "plain"
    INDIAN_GROUPED = "indian_grouped"
    WESTERN_GROUPED = "western_grouped"
    DECIMAL_COMMA = "decimal_comma"      # 350,00 — the defect Phase 3C measured
    AMBIGUOUS_GROUPING = "ambiguous_grouping"
    WORDS = "words"
    UNPARSABLE = "unparsable"


@dataclass(frozen=True)
class MoneyReading:
    """One reader's reading of one money token, under one named rule.

    ``paise`` is ``None`` whenever the token is ambiguous, malformed, or was read by a rule
    that is not allowed to produce a value. A reading never carries a *repaired* magnitude:
    the alternative interpretation is reported separately, by :mod:`money`, and the choice
    between them is a decision the trust core refuses to make silently.
    """

    reader: str
    rule: str = "strict_tail"
    raw: str = ""
    paise: int | None = None
    money_format: MoneyFormat = MoneyFormat.PLAIN
    alternative_paise: int | None = None
    alternative_reason: str | None = None
    flags: tuple[str, ...] = ()
    evidence: SourceEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.paise is not None


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class PolicyClauseRecord:
    """One clause: its span is its identity, its heading is only a label."""

    clause_id: str                       # span-derived (see addressing.py)
    heading_path: tuple[str, ...]
    heading_label: str | None            # as printed: "4.2", "Section 6", "I"
    document_id: str
    page_number: int
    char_start: int
    char_end: int
    original_text: str
    normalised_text: str
    evidence: SourceEvidence
    uin: str | None = None
    product: str | None = None
    effective_date: str | None = None
    byte_offset_in_page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "heading_path": list(self.heading_path),
            "heading_label": self.heading_label,
            "document_id": self.document_id,
            "page": self.page_number,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "chars": len(self.original_text),
            "uin": self.uin,
            "product": self.product,
            "effective_date": self.effective_date,
            "text_preview": _preview(self.original_text, 96),
        }


@dataclass(frozen=True)
class PolicyFact:
    """A policy-stated value (a limit, a percentage, a waiting period).

    It records what the document *says*. Whether that value governs a claim is decided
    later and elsewhere.
    """

    fact_id: str
    kind: str                            # 'sub_limit' | 'co_pay' | 'waiting_period' | ...
    raw_text: str
    value_text: str
    amount_paise: int | None = None
    percent: str | None = None           # exact decimal string, e.g. "20"
    months: int | None = None
    clause_id: str | None = None
    evidence: SourceEvidence | None = None
    readers_agree: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id, "kind": self.kind,
            "value_text": self.value_text, "amount_paise": self.amount_paise,
            "percent": self.percent, "months": self.months,
            "clause_id": self.clause_id,
            "evidence": self.evidence.as_dict() if self.evidence else None,
            "readers_agree": self.readers_agree,
        }


# --------------------------------------------------------------------------- bill


@dataclass(frozen=True)
class BillLine:
    """One billed item as read. No category, no verdict, no legality."""

    line_id: str
    document_id: str
    page_number: int
    line_number: int
    raw_label: str
    label: str
    rate_paise: int | None = None
    quantity_milli: int | None = None    # exact: quantity x 1000
    amount_paise: int | None = None
    amount_readings: tuple[MoneyReading, ...] = ()
    printed_amount_tokens: tuple[str, ...] = ()
    date_text: str | None = None
    code: str | None = None
    is_summary_row: bool = False
    summary_kind: str | None = None      # 'subtotal' | 'grand_total' | 'net_payable' | ...
    evidence: SourceEvidence | None = None
    flags: tuple[str, ...] = ()

    @property
    def has_usable_amount(self) -> bool:
        return self.amount_paise is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id, "document_id": self.document_id,
            "page": self.page_number, "line_number": self.line_number,
            "label": self.label, "rate_paise": self.rate_paise,
            "quantity_milli": self.quantity_milli, "amount_paise": self.amount_paise,
            "date_text": self.date_text, "code": self.code,
            "is_summary_row": self.is_summary_row, "summary_kind": self.summary_kind,
            "flags": list(self.flags),
            "amount_readers": [
                {"reader": r.reader, "raw": r.raw, "paise": r.paise,
                 "format": r.money_format.value, "flags": list(r.flags)}
                for r in self.amount_readings],
            "evidence": self.evidence.as_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class BillSubtotal:
    """A printed category subtotal. It is a *claim by the document* about its own lines."""

    subtotal_id: str
    document_id: str
    page_number: int
    category_text: str
    amount_paise: int | None
    amount_readings: tuple[MoneyReading, ...] = ()
    evidence: SourceEvidence | None = None
    contributing_line_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "subtotal_id": self.subtotal_id, "document_id": self.document_id,
            "page": self.page_number, "category": self.category_text,
            "amount_paise": self.amount_paise,
            "contributing_lines": len(self.contributing_line_ids),
            "evidence": self.evidence.as_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class BillTotal:
    """A printed total. Several may exist on one document, and Phase 3C found one that
    disagrees with another by three paise — so they are all kept, never merged."""

    total_id: str
    document_id: str
    page_number: int
    kind: str                            # 'grand_total' | 'net_payable' | ...
    amount_paise: int | None
    amount_readings: tuple[MoneyReading, ...] = ()
    evidence: SourceEvidence | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_id": self.total_id, "document_id": self.document_id,
            "page": self.page_number, "kind": self.kind, "amount_paise": self.amount_paise,
            "evidence": self.evidence.as_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class SettlementLine:
    """A line of a settlement/rejection communication.

    Nothing in the acquired corpus produces one of these yet (Phase 3C: 0 of 17 documents
    contain any approval, deduction or rejection text). The schema exists so the pipeline
    has a defined destination when such a document *is* supplied — and so that its absence
    is visible rather than papered over by a workaround.
    """

    settlement_line_id: str
    document_id: str
    page_number: int
    description: str
    amount_paise: int | None = None
    reason_text: str | None = None
    clause_reference: str | None = None
    evidence: SourceEvidence | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "settlement_line_id": self.settlement_line_id,
            "document_id": self.document_id, "page": self.page_number,
            "description": _preview(self.description, 64), "amount_paise": self.amount_paise,
            "reason_text": _preview(self.reason_text or "", 64) or None,
            "clause_reference": self.clause_reference,
            "evidence": self.evidence.as_dict() if self.evidence else None,
        }


@dataclass
class ParsedBill:
    """Everything one bill yielded. Pure extraction — no arithmetic is performed here."""

    document_id: str
    lines: list[BillLine] = field(default_factory=list)
    subtotals: list[BillSubtotal] = field(default_factory=list)
    totals: list[BillTotal] = field(default_factory=list)
    pages_read: int = 0
    readers_used: tuple[str, ...] = ()

    @property
    def item_lines(self) -> list[BillLine]:
        return [l for l in self.lines if not l.is_summary_row]
