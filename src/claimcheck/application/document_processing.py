"""Deterministic document-processing orchestration over the existing ingestion stack.

This module stops at versioned extraction/evidence state. It never constructs a trusted
StructuredCase, calls ``claimcheck.pipeline``, runs a calculator, or creates a claim verdict.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from claimcheck.evidence.spans import EvidenceBuilder, PageIndex
from claimcheck.ingest.bill import BillParseResult, bill_stats, parse_bill
from claimcheck.ingest.dual import FactDisposition
from claimcheck.ingest.model import DocumentType
from claimcheck.ingest.pdf import OcrPolicy, PageRead, PdfDocumentReader, classify_document
from claimcheck.ingest.policy import ParsedPolicy, clause_stats, parse_policy
from claimcheck.ingest.records import (
    BillLine,
    BillSubtotal,
    BillTotal,
    PolicyClauseRecord,
    SettlementLine,
    SourceEvidence,
)
from claimcheck.ingest.settlement import ParsedSettlement, parse_settlement
from claimcheck.schema import (
    DocRole,
    Document,
    ExtractionMeta,
    Page,
)

EXTRACTION_VERSION = "claimcheck-ingest-1"
SUPPORTED_ROLES = frozenset({"policy_wording", "policy_schedule", "bill", "settlement"})


class ProcessingFailure(Exception):
    """Safe, classification-only processing error; never carries source text or a path."""

    def __init__(self, error_code: str, *, retryable: bool = False) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True)
class EvidenceDraft:
    evidence_id: UUID
    page_number: int
    quote: str
    source_sha256: str
    page_text_sha256: str
    char_start: int | None
    char_end: int | None
    reader: str
    extraction_method: str
    verification_status: str
    verification_method: str
    verify_score: float
    bbox: list[float] | None
    bbox_is_measured: bool
    injection_flags: list[str]
    reason_code: str | None


@dataclass(frozen=True)
class FieldDraft:
    field_path: str
    value: Any
    state: str
    evidence_id: UUID | None = None
    reason_code: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordDraft:
    record_type: str
    record_key: str
    state: str
    evidence_id: UUID | None
    record: dict[str, Any]


@dataclass(frozen=True)
class PageDraft:
    page_number: int
    layout_state: str
    native_state: str
    layout_method: str | None
    native_method: str | None
    layout_text: str | None
    native_text: str | None
    width_points: float | None
    height_points: float | None
    ocr_confidence: float | None
    quality: dict[str, Any]


@dataclass(frozen=True)
class ProcessingOutput:
    status: str
    detected_role: str | None
    role_mismatch: bool
    page_count: int
    summary: dict[str, Any]
    pages: tuple[PageDraft, ...]
    evidence: tuple[EvidenceDraft, ...]
    fields: tuple[FieldDraft, ...]
    records: tuple[RecordDraft, ...]


def _preflight_pdf(data: bytes, *, expected_pages: int, max_pages: int) -> int:
    if not data.startswith(b"%PDF-"):
        raise ProcessingFailure("INVALID_PDF")
    try:
        from pypdf import PdfReader
        from io import BytesIO

        pdf = PdfReader(BytesIO(data), strict=True)
        if pdf.is_encrypted:
            raise ProcessingFailure("ENCRYPTED_PDF")
        count = len(pdf.pages)
    except ProcessingFailure:
        raise
    except Exception as exc:
        raise ProcessingFailure("INVALID_PDF") from exc
    if count < 1 or count > max_pages:
        raise ProcessingFailure("PAGE_LIMIT_EXCEEDED")
    if count != expected_pages:
        raise ProcessingFailure("STORED_PAGE_COUNT_MISMATCH")
    return count


def _role_proposal(first_page_text: str, path: Path) -> str | None:
    """Return a deterministic proposal only when the existing readers support one."""
    document_type = classify_document(path, first_page_text)
    head = (first_page_text or "").casefold()
    if document_type == DocumentType.HOSPITAL_BILL:
        return "bill"
    if document_type == DocumentType.POLICY_WORDING:
        schedule_keywords = ["policy number", "sum insured", "room rent", "icu", "co-pay", "policy schedule"]
        wording_keywords = ["coverage", "exclusions", "claims procedure", "waiting period", "definitions", "conditions"]
        
        schedule_score = sum(1 for k in schedule_keywords if k in head)
        wording_score = sum(1 for k in wording_keywords if k in head)
        
        if schedule_score > wording_score and schedule_score > 0:
            return "policy_schedule"
        return "policy_wording"
    if document_type == DocumentType.UNKNOWN and re.search(
            r"\b(settlement|settled|sanctioned|disallowed|deducted|claim decision)\b", head):
        return "settlement"
    return None


def _doc_role(role: str) -> DocRole:
    return {
        "policy_wording": DocRole.POLICY_WORDING,
        "policy_schedule": DocRole.POLICY_SCHEDULE,
        "bill": DocRole.BILL,
        "settlement": DocRole.SETTLEMENT,
    }[role]


def _classify_bill_line(label: str) -> str:
    text = (label or "").casefold()
    if any(k in text for k in ("room", "bed", "ward", "nursing", "accommodation")):
        return "room"
    if any(k in text for k in ("icu", "intensive care", "itu")):
        return "icu"
    if any(k in text for k in ("surgeon", "surgery", "operation", "ot charges")):
        return "surgeon" if "surgeon" in text else "ot_charges"
    if "anaesthesia" in text:
        return "anaesthesia"
    if any(k in text for k in ("pharmacy", "medicine", "injection", "tab", "iv fluid", "consumable", "drugs", "disposables")):
        return "pharmacy"
    if any(k in text for k in ("investigation", "test", "cbc", "ultrasound", "scan", "x-ray", "pathology", "lab")):
        return "diagnostics"
    if any(k in text for k in ("consultation", "doctor", "visit")):
        return "consultation"
    if any(k in text for k in ("procedure", "procedures")):
        return "procedures"
    if "ambulance" in text:
        return "ambulance"
    return "miscellaneous"


def _classify_settlement_head(description: str) -> str:
    text = (description or "").casefold()
    if any(k in text for k in ("registration", "admission")):
        return "registration"
    if any(k in text for k in ("consumables", "toiletries", "tissue", "gloves", "diet", "food", "nutrition")):
        return "consumables"
    if any(k in text for k in ("copay", "co-pay", "co payment")):
        return "co-pay"
    if any(k in text for k in ("room", "rent", "limit", "proportionate")):
        return "room rent proportionate deduction"
    if "non-medical" in text:
        return "non-medical expenses"
    return description


def _safe_json(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


def _span_key(run_id: UUID, source: SourceEvidence) -> UUID:
    material = "|".join((
        str(run_id), source.document_id, str(source.page_number), str(source.char_start),
        str(source.char_end), source.reader, source.extraction_method.value, source.text,
    ))
    return uuid5(NAMESPACE_URL, material)


def _quality_score(page_read: PageRead) -> float:
    flags = set(page_read.asset.quality.flags)
    if "NO_TEXT" in flags:
        return 0.0
    if any(flag.startswith("LOW_OCR_CONFIDENCE_LINES:") for flag in flags):
        return 0.5
    return 1.0


def _page_indexes(document_id: str, reads: list[PageRead]) -> dict[str, PageIndex]:
    indexes: dict[str, PageIndex] = {}
    for reader_name in ("layout", "native"):
        pages = []
        for page_read in reads:
            extraction = getattr(page_read, reader_name)
            if extraction is None:
                continue
            pages.append(Page(
                page_id=f"{document_id}:{page_read.asset.page_number}:{reader_name}",
                document_id=document_id,
                page_number=page_read.asset.page_number,
                text=extraction.text,
                width_px=None,
                height_px=None,
                quality_score=_quality_score(page_read),
                quality_codes=page_read.asset.quality.flags,
                text_layer_used=extraction.method.value != "ocr",
            ))
        indexes[reader_name] = PageIndex(pages)
    return indexes


def process_document_bytes(
    data: bytes,
    *,
    case_id: UUID,
    document_id: UUID,
    processing_run_id: UUID,
    assigned_role: str,
    expected_sha256: str,
    expected_pages: int,
    max_pages: int = 250,
    ocr_enabled: bool = True,
) -> ProcessingOutput:
    """Read immutable bytes, run existing dual-reader/parsers and build verified spans."""
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != expected_sha256:
        raise ProcessingFailure("STORED_HASH_MISMATCH")
    if assigned_role not in SUPPORTED_ROLES:
        raise ProcessingFailure("UNSUPPORTED_DOCUMENT_ROLE")
    count = _preflight_pdf(data, expected_pages=expected_pages, max_pages=max_pages)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="claimcheck-process-", suffix=".pdf",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with PdfDocumentReader(temp_path, str(document_id), ocr=OcrPolicy(enabled=ocr_enabled)) as reader:
            page_reads = reader.read_pages()
            if len(page_reads) != count:
                raise ProcessingFailure("PDF_READER_PAGE_COUNT_MISMATCH")
    except ProcessingFailure:
        raise
    except Exception as exc:
        if type(exc).__name__ == "TesseractNotFoundError":
            raise ProcessingFailure("OCR_ENGINE_UNAVAILABLE", retryable=True) from exc
        raise ProcessingFailure("PDF_EXTRACTION_FAILED") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    document_id_text = str(document_id)
    pages_layout = {p.asset.page_number: p.layout for p in page_reads if p.layout is not None}
    pages_native = {p.asset.page_number: p.native for p in page_reads if p.native is not None}
    primary_text = pages_layout or pages_native
    first_page_text = primary_text.get(1).text if 1 in primary_text else ""
    detected_role = _role_proposal(first_page_text, Path("uploaded.pdf"))
    role_mismatch = detected_role is not None and detected_role != assigned_role

    parser_output: ParsedPolicy | BillParseResult | ParsedSettlement | None = None
    relevant_record_count = 0
    amount_facts = []
    if assigned_role == "policy_wording":
        parser_output = parse_policy(
            document_id_text,
            pages_layout.values(),
            source_sha256=actual_sha,
        )
        relevant_record_count = len(parser_output.clauses)
    elif assigned_role == "bill":
        parser_output = parse_bill(
            document_id_text,
            pages_layout=pages_layout,
            pages_native=pages_native,
            source_sha256=actual_sha,
        )
        relevant_record_count = len(parser_output.bill.lines)
        amount_facts = parser_output.amount_facts
    elif assigned_role == "settlement":
        parser_output = parse_settlement(
            document_id_text,
            pages_layout=pages_layout,
            pages_native=pages_native,
            source_sha256=actual_sha,
        )
        relevant_record_count = len(parser_output.lines)
        amount_facts = parser_output.amount_facts

    indexes = _page_indexes(document_id_text, page_reads)
    source_document = Document(
        document_id=document_id_text,
        case_id=str(case_id),
        role=_doc_role(assigned_role),
        media_type="application/pdf",
        sha256=actual_sha,
        byte_size=len(data),
        object_key="private-original",
        page_count=count,
    )
    builders = {
        reader_name: EvidenceBuilder(index, (source_document,))
        for reader_name, index in indexes.items()
    }
    evidence: list[EvidenceDraft] = []
    evidence_ids: dict[tuple, UUID] = {}

    def add_evidence(source: SourceEvidence | None) -> UUID | None:
        if source is None:
            return None
        identity = (source.page_number, source.reader, source.char_start, source.char_end,
                    source.text, source.extraction_method.value)
        if identity in evidence_ids:
            return evidence_ids[identity]
        builder = builders.get(source.reader)
        if builder is None or indexes[source.reader].get(document_id_text, source.page_number) is None:
            draft_id = _span_key(processing_run_id, source)
            evidence_ids[identity] = draft_id
            evidence.append(EvidenceDraft(
                evidence_id=draft_id, page_number=source.page_number, quote=source.text,
                source_sha256=source.source_sha256, page_text_sha256=source.page_text_sha256,
                char_start=None, char_end=None, reader=source.reader,
                extraction_method=source.extraction_method.value,
                verification_status="REJECTED", verification_method="rejected", verify_score=0.0,
                bbox=source.bbox.as_tuple() if source.bbox else None,
                bbox_is_measured=source.bbox_is_measured,
                injection_flags=[], reason_code="PAGE_TEXT_UNAVAILABLE",
            ))
            return draft_id
        outcome = builder.build(
            document_id=document_id_text,
            page_number=source.page_number,
            quote=source.text,
            extraction=ExtractionMeta(
                method=source.extraction_method.value,
                engine="claimcheck.ingest.PdfDocumentReader",
                engine_version=EXTRACTION_VERSION,
                confidence=(source.ocr_confidence if source.ocr_confidence is not None else 1.0),
                dual_read=False,
            ),
            strict=False,
        )
        draft_id = _span_key(processing_run_id, source)
        evidence_ids[identity] = draft_id
        verify_status = outcome.verification_status
        reason = None
        if verify_status == "AMBIGUOUS":
            reason = "SPAN_AMBIGUOUS"
        elif verify_status != "VERIFIED":
            reason = "SPAN_UNVERIFIED"
        evidence.append(EvidenceDraft(
            evidence_id=draft_id,
            page_number=source.page_number,
            quote=source.text,
            source_sha256=source.source_sha256,
            page_text_sha256=outcome.page_text_hash,
            char_start=outcome.char_start,
            char_end=outcome.char_end,
            reader=source.reader,
            extraction_method=source.extraction_method.value,
            verification_status=verify_status,
            verification_method=outcome.verify_method,
            verify_score=outcome.verify_score,
            bbox=source.bbox.as_tuple() if source.bbox else None,
            bbox_is_measured=source.bbox_is_measured,
            injection_flags=list(outcome.injection_flags),
            reason_code=reason,
        ))
        return draft_id

    fields: list[FieldDraft] = []
    records: list[RecordDraft] = []

    detected_status = "MISSING" if detected_role is None else (
        "NEEDS_REVIEW" if role_mismatch else "PROPOSED"
    )
    fields.append(FieldDraft(
        field_path="document.role.detected",
        value=detected_role,
        state=detected_status,
        reason_code=("ROLE_MISMATCH" if role_mismatch else
                     "NO_DETERMINISTIC_ROLE_PROPOSAL" if detected_role is None else None),
        provenance={"source": "DETERMINISTIC_CLASSIFIER", "assigned_role": assigned_role},
    ))

    def evidence_state(evidence_id: UUID | None) -> str:
        span = next((item for item in evidence if item.evidence_id == evidence_id), None)
        return "VERIFIED" if span is not None and span.verification_status == "VERIFIED" else "NEEDS_REVIEW"

    def add_field(path: str, value: Any, state: str, source: SourceEvidence | None = None,
                  *, reason: str | None = None, provenance: dict[str, Any] | None = None) -> UUID | None:
        evidence_id = add_evidence(source) if source is not None else None
        if source is not None and evidence_id is not None:
            span = next((item for item in evidence if item.evidence_id == evidence_id), None)
            if span is None or span.verification_status != "VERIFIED":
                if state == "VERIFIED":
                    state = "NEEDS_REVIEW"
                    reason = span.reason_code if span is not None else "SPAN_UNVERIFIED"
        fields.append(FieldDraft(
            field_path=path,
            value=value,
            state=state,
            evidence_id=evidence_id,
            reason_code=reason,
            provenance=provenance or {},
        ))
        return evidence_id

    # Page artifacts and page hashes retain both reader views, not only the preferred text.
    page_drafts: list[PageDraft] = []
    for page_read in page_reads:
        layout = page_read.layout
        native = page_read.native
        page_drafts.append(PageDraft(
            page_number=page_read.asset.page_number,
            layout_state="READ" if layout is not None else "UNREADABLE",
            native_state="READ" if native is not None else "UNREADABLE",
            layout_method=layout.method.value if layout is not None else None,
            native_method=native.method.value if native is not None else None,
            layout_text=layout.text if layout is not None else None,
            native_text=native.text if native is not None else None,
            width_points=page_read.asset.width,
            height_points=page_read.asset.height,
            ocr_confidence=layout.mean_confidence if layout is not None else None,
            quality=page_read.asset.quality.as_dict(),
        ))

    if isinstance(parser_output, ParsedPolicy):
        for clause in parser_output.clauses:
            ev_id = add_evidence(clause.evidence)
            state = evidence_state(ev_id)
            records.append(RecordDraft(
                record_type="POLICY_CLAUSE",
                record_key=clause.clause_id,
                state=state,
                evidence_id=ev_id,
                record=clause.as_dict(),
            ))
        metadata_fields = (
            ("policy.uin", parser_output.metadata.uin),
            ("policy.product", parser_output.metadata.product),
            ("policy.effective_text", parser_output.metadata.effective_text),
        )
        for field_path, value in metadata_fields:
            if value is None:
                add_field(field_path, None, "MISSING", reason="NOT_STATED_OR_NOT_PARSED")
                continue
            source = _find_source(value, document_id_text, actual_sha, pages_layout)
            add_field(field_path, value, "PROPOSED", source,
                      provenance={"parser": "parse_policy", "version": EXTRACTION_VERSION})
        summary = clause_stats(parser_output)
    elif isinstance(parser_output, BillParseResult):
        lines_by_id = {line.line_id: line for line in parser_output.bill.lines}
        for line in parser_output.bill.lines:
            primary_id = add_evidence(line.evidence)
            line_state = evidence_state(primary_id)
            records.append(RecordDraft("BILL_LINE", line.line_id, line_state, primary_id,
                                       line.as_dict()))
            facts_for_line = [fact for fact in parser_output.amount_facts
                              if fact.subject == line.line_id]
            amount_state, amount_reason = _amount_state(
                line.amount_paise,
                facts_for_line,
                has_candidate=bool(line.amount_readings or line.printed_amount_tokens),
            )
            # Use the layout source as the field anchor only when the dual parser made a
            # usable decision; both per-reader readings are persisted as separate evidence.
            for reading in line.amount_readings:
                add_evidence(reading.evidence)
            add_field(
                f"bill.line.{line.line_id}.amount_paise",
                line.amount_paise,
                amount_state,
                line.evidence,
                reason=amount_reason,
                provenance={
                    "readers": sorted({reading.reader for reading in line.amount_readings}),
                    "agreement_required": True,
                    "parser": "parse_bill",
                },
            )
            
            # Map category
            proposed_category = _classify_bill_line(line.label)
            category_state = "VERIFIED" if proposed_category else "MISSING"
            add_field(
                f"bill.line.{line.line_id}.category",
                proposed_category,
                category_state,
                line.evidence,
                provenance={"parser": "parse_bill", "classifier": "deterministic"},
            )
        for subtotal in parser_output.bill.subtotals:
            ev_id = add_evidence(subtotal.evidence)
            records.append(RecordDraft("BILL_SUBTOTAL", subtotal.subtotal_id,
                                       evidence_state(ev_id), ev_id, subtotal.as_dict()))
        for total in parser_output.bill.totals:
            ev_id = add_evidence(total.evidence)
            records.append(RecordDraft("BILL_TOTAL", total.total_id,
                                       evidence_state(ev_id), ev_id, total.as_dict()))
                                       
        bill_total = None
        for total in parser_output.bill.totals:
            if total.kind == "grand_total" or "total" in total.kind:
                bill_total = total
                break
        if bill_total and bill_total.amount_paise is not None:
            ev_id = add_evidence(bill_total.evidence)
            add_field("bill.total_paise", bill_total.amount_paise, "VERIFIED", bill_total.evidence, provenance={"parser": "parse_bill"})
        else:
            add_field("bill.total_paise", None, "MISSING", reason="TOTAL_NOT_FOUND", provenance={"parser": "parse_bill"})
        item_lines = parser_output.bill.item_lines
        item_field_states = [next((item.state for item in fields
                                   if item.field_path == f"bill.line.{line.line_id}.amount_paise"),
                                  "MISSING") for line in item_lines]
        if not item_lines:
            add_field("bill.item_amounts", None, "MISSING",
                      reason="NO_BILL_ITEM_AMOUNT_FOUND",
                      provenance={"parser": "parse_bill"})
        elif all(state == "VERIFIED" for state in item_field_states):
            add_field("bill.item_amounts", [line.amount_paise for line in item_lines],
                      "VERIFIED", provenance={
                          "parser": "parse_bill",
                          "source_fields": [f"bill.line.{line.line_id}.amount_paise"
                                            for line in item_lines],
                      })
        else:
            if any(state in {"QUARANTINED", "CONFLICTING"} for state in item_field_states):
                aggregate_state, aggregate_reason = "QUARANTINED", "ITEM_AMOUNT_QUARANTINED"
            elif any(state == "UNREADABLE" for state in item_field_states):
                aggregate_state, aggregate_reason = "UNREADABLE", "ITEM_AMOUNT_UNREADABLE"
            else:
                aggregate_state, aggregate_reason = "MISSING", "ITEM_AMOUNT_MISSING"
            add_field("bill.item_amounts", None, aggregate_state,
                      reason=aggregate_reason,
                      provenance={"parser": "parse_bill", "required_readers": 2})
        summary = bill_stats(parser_output)
    elif isinstance(parser_output, ParsedSettlement):
        lines_by_id = {line.settlement_line_id: line for line in parser_output.lines}
        for line in parser_output.lines:
            ev_id = add_evidence(line.evidence)
            line_state = evidence_state(ev_id)
            records.append(RecordDraft("SETTLEMENT_LINE", line.settlement_line_id,
                                       line_state, ev_id, line.as_dict()))
            facts_for_line = [fact for fact in parser_output.amount_facts
                              if fact.subject == f"settlement:{line.settlement_line_id}"]
            amount_state, amount_reason = _amount_state(
                line.amount_paise, facts_for_line,
                has_candidate=any(f.readings for f in facts_for_line),
            )
            add_field(
                f"settlement.line.{line.settlement_line_id}.amount_paise",
                line.amount_paise,
                amount_state,
                line.evidence,
                reason=amount_reason,
                provenance={"readers_required": 2, "parser": "parse_settlement"},
            )
            
            proposed_head = _classify_settlement_head(line.description)
            head_state = "VERIFIED" if proposed_head else "MISSING"
            add_field(
                f"settlement.line.{line.settlement_line_id}.head",
                proposed_head,
                head_state,
                line.evidence,
                provenance={"parser": "parse_settlement", "classifier": "deterministic"},
            )
            
            
        if parser_output.claimed_amount_paise is not None:
            ev_id = add_evidence(parser_output.claimed_amount_evidence)
            add_field("settlement.claimed_amount_paise", parser_output.claimed_amount_paise, "VERIFIED", parser_output.claimed_amount_evidence, provenance={"parser": "parse_settlement"})
        else:
            add_field("settlement.claimed_amount_paise", None, "MISSING", reason="TOTAL_NOT_FOUND", provenance={"parser": "parse_settlement"})
            
        if parser_output.final_payable_paise is not None:
            ev_id = add_evidence(parser_output.final_payable_evidence)
            add_field("settlement.final_payable_paise", parser_output.final_payable_paise, "VERIFIED", parser_output.final_payable_evidence, provenance={"parser": "parse_settlement"})
        else:
            add_field("settlement.final_payable_paise", None, "MISSING", reason="TOTAL_NOT_FOUND", provenance={"parser": "parse_settlement"})
            
        add_field("settlement.partial_disallowance", True, "PROPOSED", provenance={"parser": "parse_settlement"})
        add_field("settlement.repudiated", False, "PROPOSED", provenance={"parser": "parse_settlement"})

        summary = {
            "document_id": document_id_text,
            "pages": parser_output.pages_read,
            "settlement_lines": len(parser_output.lines),
            "amount_facts": len(parser_output.amount_facts),
            "quarantined_amount_facts": parser_output.quarantine.count,
        }
    elif assigned_role == "policy_schedule":
        # The existing code has no policy-schedule parser. Use basic text extraction so obvious
        # values don't become customer work.
        summary = {"document_id": document_id_text, "pages": count,
                   "parser": "primitive_regex", "record_count": 0}
        
        text = first_page_text.lower()
        
        reader_name = "layout" if 1 in pages_layout else "native"
        page_extr = primary_text.get(1)
        page_text_sha = hashlib.sha256((page_extr.text if page_extr else first_page_text).encode("utf-8")).hexdigest()
        
        def _add_regex_field(path: str, value: Any, match: re.Match):
            source = SourceEvidence(
                document_id=document_id_text,
                page_number=1,
                text=match.group(0),
                char_start=match.start(),
                char_end=match.end(),
                reader=reader_name,
                extraction_method=page_extr.method if page_extr else EditOp.NONE,
                source_sha256=actual_sha,
                page_text_sha256=page_text_sha,
            )
            ev_id = add_evidence(source)
            add_field(path, value, "VERIFIED", source, provenance={"parser": "primitive_regex"})
            
        si_match = re.search(r"sum insured[^\d]+([\d,]+)", text)
        if si_match:
            try:
                si_val = int(si_match.group(1).replace(",", "")) * 100
                _add_regex_field("policy.sum_insured_paise", si_val, si_match)
            except ValueError:
                add_field("policy.sum_insured_paise", None, "MISSING", reason="PARSER_NOT_AVAILABLE", provenance={})
        else:
            add_field("policy.sum_insured_paise", None, "MISSING", reason="PARSER_NOT_AVAILABLE", provenance={})
            
        rr_match = re.search(r"room rent[^\d]+([\d,]+(?:\s*(?:%|percent))?)", text, re.IGNORECASE)
        if rr_match:
            try:
                raw_val = rr_match.group(1).replace(",", "").strip().lower()
                if "%" in raw_val or "percent" in raw_val:
                    pct = int(re.sub(r"[^\d]", "", raw_val))
                    if si_match:
                        # si_val is defined above inside the si_match block
                        si_val = int(si_match.group(1).replace(",", "")) * 100
                        rr_val = int(si_val * (pct / 100.0))
                        _add_regex_field("policy.room_rent_limit_paise", rr_val, rr_match)
                else:
                    rr_val = int(raw_val) * 100
                    _add_regex_field("policy.room_rent_limit_paise", rr_val, rr_match)
            except ValueError:
                pass
                
        copay_match = re.search(r"co-pay(?:ment)?[^\d]+(\d+)\s*%", text)
        if copay_match:
            try:
                _add_regex_field("policy.co_pay_percent", int(copay_match.group(1)), copay_match)
            except ValueError:
                pass
                
        fields.append(FieldDraft(
            field_path="policy_schedule.records",
            value=None,
            state="MISSING",
            reason_code="PARSER_NOT_AVAILABLE",
            provenance={"source": "NO_EXISTING_TYPED_PARSER"},
        ))
    else:
        summary = {"document_id": document_id_text, "pages": count, "parser": "NOT_AVAILABLE", "record_count": 0}

    verified_evidence = sum(1 for item in evidence if item.verification_status == "VERIFIED")
    unverified_evidence = len(evidence) - verified_evidence
    no_text_pages = sum(1 for page in page_drafts
                        if page.layout_state != "READ" and page.native_state != "READ")
    usable_amounts = sum(1 for item in fields
                         if item.field_path.endswith(".amount_paise")
                         and item.value is not None and item.state == "VERIFIED")
    usable_bill_item_amounts = 0
    if isinstance(parser_output, BillParseResult):
        usable_bill_item_amounts = sum(
            1 for line in parser_output.bill.item_lines
            if line.amount_paise is not None
        )
    quarantined_amounts = sum(1 for item in fields
                              if item.field_path.endswith(".amount_paise")
                              and item.state in {"QUARANTINED", "CONFLICTING", "UNREADABLE"})
    if role_mismatch:
        status = "NEEDS_REVIEW"
    elif no_text_pages == count or relevant_record_count == 0:
        status = "EVIDENCE_GAP"
    elif unverified_evidence or quarantined_amounts:
        status = "NEEDS_REVIEW"
    elif assigned_role == "bill" and usable_bill_item_amounts == 0:
        status = "NEEDS_REVIEW" if quarantined_amounts else "EVIDENCE_GAP"
    else:
        status = "READY_FOR_ANALYSIS"

    summary = {
        **summary,
        "extraction_version": EXTRACTION_VERSION,
        "pages_read": count - no_text_pages,
        "pages_without_readable_text": no_text_pages,
        "ocr_pages": sum(1 for item in page_reads if item.asset.used_ocr),
        "verified_evidence_spans": verified_evidence,
        "unverified_evidence_spans": unverified_evidence,
        "usable_amount_fields": usable_amounts,
        "usable_bill_item_amount_fields": usable_bill_item_amounts,
        "quarantined_or_unreadable_amount_fields": quarantined_amounts,
        "assigned_role": assigned_role,
        "detected_role": detected_role,
        "role_mismatch": role_mismatch,
        "analysis_or_verdict_created": False,
    }
    return ProcessingOutput(
        status=status,
        detected_role=detected_role,
        role_mismatch=role_mismatch,
        page_count=count,
        summary=summary,
        pages=tuple(page_drafts),
        evidence=tuple(evidence),
        fields=tuple(fields),
        records=tuple(records),
    )


def _amount_state(amount: int | None, facts: list[Any], *, has_candidate: bool) -> tuple[str, str | None]:
    if amount is not None and facts and all(
            fact.disposition is FactDisposition.USABLE for fact in facts):
        return "VERIFIED", None
    agreements = {fact.agreement.value for fact in facts}
    if "disagree" in agreements:
        return "CONFLICTING", "DUAL_READER_DISAGREEMENT"
    if "unreadable" in agreements or any(
            fact.disposition is FactDisposition.EVIDENCE_GAP for fact in facts):
        return ("UNREADABLE", "NO_UNAMBIGUOUS_AMOUNT_READING") if has_candidate else (
            "MISSING", "AMOUNT_NOT_STATED_OR_NOT_PARSED")
    if any(fact.disposition is FactDisposition.QUARANTINED for fact in facts):
        return "QUARANTINED", "SINGLE_READER_OR_AMBIGUOUS_READING"
    if has_candidate:
        return "UNREADABLE", "AMOUNT_NOT_VERIFIED"
    return "MISSING", "AMOUNT_NOT_STATED_OR_NOT_PARSED"


def _find_source(value: str, document_id: str, source_sha: str,
                 pages: dict[int, Any]) -> SourceEvidence | None:
    """Bind a parser-surfaced metadata string to its exact page substring."""
    for page_number, page in sorted(pages.items()):
        start = page.text.find(value)
        if start >= 0 and page.text.find(value, start + 1) < 0:
            return SourceEvidence(
                document_id=document_id,
                page_number=page_number,
                text=value,
                char_start=start,
                char_end=start + len(value),
                reader="layout",
                extraction_method=page.method,
                source_sha256=source_sha,
                page_text_sha256=hashlib.sha256(page.text.encode("utf-8")).hexdigest(),
            )
    return None
