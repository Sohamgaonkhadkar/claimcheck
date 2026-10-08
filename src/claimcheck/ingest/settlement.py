"""Literal settlement-communication parsing built on the existing page/read/evidence types.

This parser records only lines whose printed words explicitly contain settlement, payment,
rejection, deduction, approval, or disallowance language. It does not decide why an amount was
reduced, whether a deduction is lawful, or whether any amount is due. A monetary magnitude is
released only when the existing deterministic readers agree under ``reconcile_money``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .bill import align_lines
from .dual import DualMoneyFact, FactDisposition, Quarantine, reconcile_money
from .model import PageExtraction
from .money import as_reading, read_line_money
from .records import SettlementLine, SourceEvidence

_SETTLEMENT_LANGUAGE = re.compile(
    r"\b(?:settle(?:ment|d|d\s+amount)?|sanction(?:ed)?|approved|approval|"
    r"reject(?:ed|ion)?|denied|disallow(?:ed|ance)?|deduct(?:ed|ion)?|"
    r"non[- ]?payable|not payable|payable|reimburs(?:ed|ement)|paid|payment|"
    r"excess|shortfall|co[- ]?pay|co[- ]?payment)\b", re.IGNORECASE,
)


@dataclass
class ParsedSettlement:
    document_id: str
    lines: list[SettlementLine] = field(default_factory=list)
    amount_facts: list[DualMoneyFact] = field(default_factory=list)
    quarantine: Quarantine = field(default_factory=Quarantine)
    pages_read: int = 0
    claimed_amount_paise: int | None = None
    final_payable_paise: int | None = None
    claimed_amount_evidence: SourceEvidence | None = None
    final_payable_evidence: SourceEvidence | None = None


def parse_settlement(
    document_id: str,
    *,
    pages_layout: dict[int, PageExtraction],
    pages_native: dict[int, PageExtraction],
    source_sha256: str,
) -> ParsedSettlement:
    """Record literal settlement-related lines; no values are inferred from context."""
    result = ParsedSettlement(document_id=document_id, pages_read=len(pages_layout))
    
    full_text = ""
    first_page_sha = None
    first_page_method = None
    if pages_layout:
        first_page = min(pages_layout.keys())
        first_page_sha = hashlib.sha256(pages_layout[first_page].text.encode("utf-8")).hexdigest()
        first_page_method = pages_layout[first_page].method
        for p in sorted(pages_layout):
            full_text += pages_layout[p].text + "\n"
            
    claimed_match = re.search(r"(?:claim(?:ed)?\s+amount|total\s+bill\s+amount)[^\d]+([\d,]+(?:\.\d{1,2})?)", full_text, re.IGNORECASE)
    if claimed_match and first_page_sha:
        try:
            val = int(round(float(claimed_match.group(1).replace(",", "")) * 100))
            result.claimed_amount_paise = val
            result.claimed_amount_evidence = SourceEvidence(
                document_id=document_id, page_number=1,
                text=claimed_match.group(0), char_start=claimed_match.start(), char_end=claimed_match.end(),
                reader="layout", extraction_method=first_page_method,
                source_sha256=source_sha256, page_text_sha256=first_page_sha
            )
        except ValueError:
            pass
            
    payable_match = re.search(r"(?:final\s+)?payable\s+amount[^\d]+([\d,]+(?:\.\d{1,2})?)", full_text, re.IGNORECASE)
    if not payable_match:
        payable_match = re.search(r"net\s+payable[^\d]+([\d,]+(?:\.\d{1,2})?)", full_text, re.IGNORECASE)
        
    if payable_match and first_page_sha:
        try:
            val = int(round(float(payable_match.group(1).replace(",", "")) * 100))
            result.final_payable_paise = val
            result.final_payable_evidence = SourceEvidence(
                document_id=document_id, page_number=1,
                text=payable_match.group(0), char_start=payable_match.start(), char_end=payable_match.end(),
                reader="layout", extraction_method=first_page_method,
                source_sha256=source_sha256, page_text_sha256=first_page_sha
            )
        except ValueError:
            pass

    for page_number in sorted(pages_layout):
        layout = pages_layout[page_number]
        native = pages_native.get(page_number)
        page_sha = hashlib.sha256(layout.text.encode("utf-8")).hexdigest()
        aligned_lines = align_lines(
            list(layout.lines), list(native.lines) if native is not None else []
        )
        for line_number, aligned in enumerate(aligned_lines, start=1):
            line = aligned.layout
            native_line = aligned.native
            exact_line = line.text.strip()
            if not exact_line or not _SETTLEMENT_LANGUAGE.search(exact_line):
                continue
            if not re.search(r"\d", exact_line) and (native_line is None or not re.search(r"\d", native_line.text)):
                continue
            readings = []
            layout_money = read_line_money(exact_line)
            readings.append(as_reading(layout_money, "layout", "strict_tail"))
            if native_line is not None:
                native_money = read_line_money(native_line.text)
                readings.append(as_reading(native_money, "native", "strict_tail"))
            evidence = SourceEvidence(
                document_id=document_id,
                page_number=page_number,
                text=line.text,
                char_start=line.char_start,
                char_end=line.char_end,
                reader="layout",
                extraction_method=layout.method,
                source_sha256=source_sha256,
                page_text_sha256=page_sha,
                bbox=line.bbox,
                bbox_is_measured=True,
                ocr_confidence=line.ocr_confidence,
            )
            fact_id = "SF-" + hashlib.sha256(
                f"{document_id}|{page_number}|{line_number}|{line.text}".encode("utf-8")
            ).hexdigest()[:16].upper()
            fact = reconcile_money(
                subject=f"settlement:{fact_id}", rule="strict_tail", readings=readings
            )
            if layout_money.amount is not None or native_line is not None and \
                    read_line_money(native_line.text).amount is not None:
                result.amount_facts.append(fact)
                result.quarantine.add(fact)
            result.lines.append(SettlementLine(
                settlement_line_id=fact_id,
                document_id=document_id,
                page_number=page_number,
                description=line.text,
                amount_paise=(fact.paise if fact.disposition is FactDisposition.USABLE else None),
                evidence=evidence,
            ))
    return result
