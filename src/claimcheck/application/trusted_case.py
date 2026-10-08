"""Sole Phase 4 product-persistence → trusted domain → StructuredCase gateway.

This adapter revalidates source and evidence artifacts, resolves only trusted effective
values, and refuses incomplete cases. It constructs a domain object only; it never imports
or calls ``pipeline.run_case`` and contains no analysis/calculation logic.
"""
from __future__ import annotations

import hashlib
from datetime import date
from fractions import Fraction
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from claimcheck.evidence.spans import PageIndex
from claimcheck.application.errors import ApplicationError
from claimcheck.application.identity import OwnerIdentity
from claimcheck.application.review import ReviewApplicationService
from claimcheck.application.review_readiness import (
    evaluate_case_readiness,
    latest_processing_run,
    latest_role_assignments,
    resolve_effective_value,
    EffectiveValueResult,
)
from claimcheck.ingest.model import Storage
from claimcheck.persistence.models import (
    Case,
    CaseDocument,
    DocumentEvidenceSpan,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    StoredPageExtraction,
)
from claimcheck.persistence.repositories import CaseRepository
from claimcheck.persistence.storage import InvalidStorageKey
from claimcheck.schema import (
    BillFacts,
    BillLine,
    CanonicalCategory,
    DocRole,
    Document,
    Evidence,
    ExtractionMeta,
    Fact,
    FactStatus,
    FactStore,
    MappingSource,
    Page,
    PolicyClause,
    PolicyFacts,
    Origin,
    SettlementDeduction,
    SettlementFacts,
    VerifyMethod,
)
from claimcheck.pipeline import StructuredCase

_ROLE_ENUM = {
    "policy_wording": DocRole.POLICY_WORDING,
    "policy_schedule": DocRole.POLICY_SCHEDULE,
    "bill": DocRole.BILL,
    "settlement": DocRole.SETTLEMENT,
}


class TrustedCaseAdapter:
    """Construct an analysis-input revision without starting analysis."""

    def __init__(self, session: Session, storage: Storage,
                 principal: OwnerIdentity) -> None:
        self.session = session
        self.storage = storage
        self.principal = principal
        self.owner_id = principal.owner_id
        self.provenance = ReviewApplicationService(session, storage, principal)

    def build(self, case_id: UUID) -> StructuredCase:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            if case is None:
                raise _not_found()
            report = evaluate_case_readiness(self.session, self.owner_id, case)
            if not report.ready:
                raise ApplicationError(
                    409,
                    "TRUSTED_INPUT_NOT_READY",
                    "Trusted input is incomplete",
                    "The case has blocking role, review, or evidence gaps; no StructuredCase was created.",
                )
            assignments = latest_role_assignments(self.session, self.owner_id, case_id)
            documents: dict[str, CaseDocument] = {}
            runs: dict[str, DocumentProcessingRun] = {}
            for role in _ROLE_ENUM:
                assignment = assignments.get(role)
                if assignment is None or assignment.document_id is None:
                    raise _not_ready("A required document role is not explicitly assigned.")
                document = self.session.scalar(select(CaseDocument).where(
                    CaseDocument.owner_id == self.owner_id,
                    CaseDocument.case_id == case_id,
                    CaseDocument.document_id == assignment.document_id,
                ))
                if document is None:
                    raise _not_ready("A selected document is unavailable.")
                run = latest_processing_run(
                    self.session, self.owner_id, case_id, document.document_id
                )
                if run is None or run.assigned_role != role:
                    raise _not_ready("A selected document has no current role-matched processing run.")
                raw = self.provenance._source_bytes(document)
                if run.source_sha256 != document.sha256:
                    raise _not_ready("A processing run does not match the current source bytes.")
                documents[role] = document
                runs[role] = run

            domain_documents = {
                role: Document(
                    document_id=str(doc.document_id),
                    case_id=str(case_id),
                    role=_ROLE_ENUM[role],
                    media_type=doc.media_type,
                    sha256=doc.sha256,
                    byte_size=doc.byte_size,
                    # This is an in-memory opaque reference, not a storage path/key.
                    object_key=f"private-document:{doc.document_id.hex}",
                    page_count=doc.page_count,
                )
                for role, doc in documents.items()
            }
            pages: list[Page] = []
            evidence_map: dict[str, Evidence] = {}
            for role, doc in documents.items():
                run = runs[role]
                page_rows = list(self.session.scalars(select(StoredPageExtraction).where(
                    StoredPageExtraction.owner_id == self.owner_id,
                    StoredPageExtraction.case_id == case_id,
                    StoredPageExtraction.document_id == doc.document_id,
                    StoredPageExtraction.processing_run_id == run.processing_run_id,
                ).order_by(StoredPageExtraction.page_number)))
                pages.extend(self._load_pages(doc, run, page_rows))
                evidence_rows = list(self.session.scalars(select(DocumentEvidenceSpan).where(
                    DocumentEvidenceSpan.owner_id == self.owner_id,
                    DocumentEvidenceSpan.case_id == case_id,
                    DocumentEvidenceSpan.document_id == doc.document_id,
                    DocumentEvidenceSpan.processing_run_id == run.processing_run_id,
                    DocumentEvidenceSpan.verification_status == "VERIFIED",
                ).order_by(DocumentEvidenceSpan.page_number,
                           DocumentEvidenceSpan.char_start,
                           DocumentEvidenceSpan.evidence_id)))
                for row in evidence_rows:
                    try:
                        verified_row, quote = self.provenance._verified_evidence(
                            doc, run, row.evidence_id
                        )
                    except ApplicationError:
                        # A broken/unverifiable optional span is omitted. Any span needed by a
                        # required fact/record/clause is rechecked below and blocks construction.
                        continue
                    evidence_map[str(row.evidence_id)] = self._domain_evidence(
                        verified_row, quote, run
                    )

            role_to_document_id = {
                role: str(doc.document_id) for role, doc in documents.items()
            }
            facts: list[Fact] = []
            review_provenance_notes: list[str] = []

            def resolved(path: str, role: str):
                result = resolve_effective_value(
                    self.session, self.owner_id, case, documents[role], runs[role], path
                )
                self._remember_visual_review(
                    review_provenance_notes, path, result.effective
                )
                return result

            def trusted(path: str, role: str) -> tuple[Any, Evidence | None, Any]:
                result = resolved(path, role)
                effective = result.effective
                if effective is None:
                    raise _not_ready(f"A required value for {path} is not trusted.")
                supporting = None
                if effective.evidence_id is not None:
                    supporting = evidence_map.get(str(effective.evidence_id))
                    if supporting is None:
                        # Revalidate so corruption produces a safe, explicit failure rather than
                        # silently dropping provenance at the adapter boundary.
                        try:
                            evidence_row, quote = self.provenance._verified_evidence(
                                documents[role], runs[role], effective.evidence_id
                            )
                        except ApplicationError as exc:
                            raise _not_ready(f"Evidence for {path} is unavailable.") from exc
                        supporting = self._domain_evidence(evidence_row, quote, runs[role])
                        evidence_map[str(effective.evidence_id)] = supporting
                if (result.correction is not None
                        and effective.verification_method == "EVIDENCE_SPAN"
                        and (supporting is None or not self.provenance._quote_supports(
                            path, effective.value, supporting.quoted_text
                        ))):
                    raise _not_ready(f"Evidence for {path} does not support the reviewed value.")
                return effective, supporting, result

            def make_fact(path: str, role: str, *, fact_id: str, fact_type: str,
                          unit: str = "inr_paise") -> Fact:
                effective, supporting, _result = trusted(path, role)
                if unit == "percent":
                    value = Fraction(str(effective.value))
                else:
                    value = effective.value
                origin = Origin.USER if effective.origin == "USER" else Origin.PRINTED
                fact = Fact(
                    fact_id=fact_id,
                    type=fact_type,
                    value=value,
                    unit=unit,
                    origin=origin,
                    status=FactStatus.READ,
                    confidence=1.0,
                    evidence_id=(str(effective.evidence_id)
                                 if effective.evidence_id is not None else None),
                    raw_text=(supporting.quoted_text if supporting is not None else None),
                    note=self._fact_review_note(effective),
                )
                facts.append(fact)
                return fact

            sum_insured = make_fact(
                "policy.sum_insured_paise", "policy_schedule",
                fact_id="policy.sum_insured", fact_type="loadbearing.money",
            )
            room_limit_result = resolved(
                "policy.room_rent_limit_paise", "policy_schedule"
            )
            room_limit = None
            if room_limit_result.effective is not None:
                room_limit = make_fact(
                    "policy.room_rent_limit_paise", "policy_schedule",
                    fact_id="policy.room_rent_limit_per_day", fact_type="loadbearing.money",
                )
            copay_result = resolved(
                "policy.co_pay_percent", "policy_schedule"
            )
            copay = None
            if copay_result.effective is not None:
                copay = make_fact(
                    "policy.co_pay_percent", "policy_schedule",
                    fact_id="policy.co_pay_percent", fact_type="loadbearing.ratio",
                    unit="percent",
                )
            ame_result = resolved("policy.ame_definition_present", "policy_schedule")
            ame_present = ame_result.effective.value if ame_result.effective is not None else None

            policy_metadata: dict[str, Any] = {}
            for path, key in (("policy.uin", "uin"), ("policy.product", "product_name")):
                result = resolved(path, "policy_wording")
                if result.effective is not None:
                    policy_metadata[key] = result.effective.value

            clause_rows = list(self.session.scalars(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == self.owner_id,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == documents["policy_wording"].document_id,
                DocumentProcessingRecord.processing_run_id == runs["policy_wording"].processing_run_id,
                DocumentProcessingRecord.record_type == "POLICY_CLAUSE",
            ).order_by(DocumentProcessingRecord.record_key)))
            clauses: dict[str, PolicyClause] = {}
            for row in clause_rows:
                evidence_id = row.evidence_id
                evidence = evidence_map.get(str(evidence_id)) if evidence_id else None
                if row.field_state != "VERIFIED" or evidence is None:
                    continue
                record = row.record_json or {}
                clause_id = str(record.get("clause_id") or row.record_key)
                heading = " > ".join(str(item) for item in record.get("heading_path", []))
                clauses[clause_id] = PolicyClause(
                    clause_id=clause_id,
                    heading_path=heading,
                    text=evidence.quoted_text,
                    evidence_id=str(evidence.evidence_id),
                    clause_type="general",
                )
            if not clauses:
                raise _not_ready("No verified policy wording clause is available.")
            policy = PolicyFacts(
                policy_id=f"POL-{case_id.hex}",
                insurer="",
                product_name=policy_metadata.get("product_name", ""),
                policy_era="",
                documents={
                    "policy_wording": role_to_document_id["policy_wording"],
                    "policy_schedule": role_to_document_id["policy_schedule"],
                },
                uin=policy_metadata.get("uin"),
                inception_date=self._optional_date(
                    case, documents, runs, "policy.inception_date", "policy_schedule",
                    review_provenance_notes,
                ),
                renewal_date=self._optional_date(
                    case, documents, runs, "policy.renewal_date", "policy_schedule",
                    review_provenance_notes,
                ),
                claim_date=case.claim_date,
                sum_insured=sum_insured,
                room_rent_limit=room_limit,
                co_pay_percent=copay,
                ame_definition_present=ame_present,
                clauses=clauses,
            )

            bill_total = make_fact(
                "bill.total_paise", "bill", fact_id="bill.total", fact_type="loadbearing.money"
            )
            bill_records = list(self.session.scalars(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == self.owner_id,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == documents["bill"].document_id,
                DocumentProcessingRecord.processing_run_id == runs["bill"].processing_run_id,
                DocumentProcessingRecord.record_type == "BILL_LINE",
            ).order_by(DocumentProcessingRecord.record_key)))
            bill_lines: list[BillLine] = []
            for row in bill_records:
                record = row.record_json or {}
                if bool(record.get("is_summary_row")):
                    continue
                evidence_id = row.evidence_id
                evidence = evidence_map.get(str(evidence_id)) if evidence_id else None
                if row.field_state != "VERIFIED" or evidence is None:
                    raise _not_ready(f"Bill line {row.record_key} has no trusted source span.")
                amount_fact = make_fact(
                    f"bill.line.{row.record_key}.amount_paise", "bill",
                    fact_id=f"bill.line.{row.record_key}.amount",
                    fact_type="loadbearing.money",
                )
                category_result, _category_evidence, _category_resolution = trusted(
                    f"bill.line.{row.record_key}.category", "bill"
                )
                try:
                    category = CanonicalCategory(category_result.value)
                except ValueError as exc:
                    raise _not_ready(f"Bill line {row.record_key} has no canonical category.") from exc
                if category is CanonicalCategory.UNMAPPED:
                    raise _not_ready(f"Bill line {row.record_key} is not mapped to a trusted category.")
                bill_lines.append(BillLine(
                    line_id=row.record_key,
                    raw_description=evidence.quoted_text,
                    amount=amount_fact.as_money(),
                    category=category,
                    mapping_source=MappingSource.USER_OVERRIDE,
                    mapping_confidence=1.0,
                    evidence_id=str(evidence.evidence_id),
                ))
            if not bill_lines:
                raise _not_ready("No evidence-backed bill item line is available.")
            bill = BillFacts(
                bill_id=f"BILL-{documents['bill'].document_id.hex}",
                lines=tuple(bill_lines),
                bill_total=bill_total.as_money(),
                document_id=str(documents['bill'].document_id),
            )

            claimed = make_fact(
                "settlement.claimed_amount_paise", "settlement",
                fact_id="settlement.claimed", fact_type="loadbearing.money",
            )
            final_payable = make_fact(
                "settlement.final_payable_paise", "settlement",
                fact_id="settlement.net_payable", fact_type="loadbearing.money",
            )
            repudiated_result, _repudiated_evidence, _ = trusted(
                "settlement.repudiated", "settlement"
            )
            partial_result, _partial_evidence, _ = trusted(
                "settlement.partial_disallowance", "settlement"
            )
            repudiated = bool(repudiated_result.value)
            partial_disallowance = bool(partial_result.value)
            if repudiated and partial_disallowance:
                raise _not_ready("Settlement classifications are mutually inconsistent.")

            settlement_records = list(self.session.scalars(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == self.owner_id,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == documents["settlement"].document_id,
                DocumentProcessingRecord.processing_run_id == runs["settlement"].processing_run_id,
                DocumentProcessingRecord.record_type == "SETTLEMENT_LINE",
            ).order_by(DocumentProcessingRecord.record_key)))
            deductions: list[SettlementDeduction] = []
            for row in settlement_records:
                evidence_id = row.evidence_id
                evidence = evidence_map.get(str(evidence_id)) if evidence_id else None
                if row.field_state != "VERIFIED" or evidence is None:
                    raise _not_ready(f"Settlement line {row.record_key} has no trusted source span.")
                amount_effective, _amount_supporting, amount_result = trusted(
                    f"settlement.line.{row.record_key}.amount_paise", "settlement"
                )
                head_effective, _head_supporting, _head_result = trusted(
                    f"settlement.line.{row.record_key}.head", "settlement"
                )
                if amount_effective is None or head_effective is None:
                    raise _not_ready(f"Settlement line {row.record_key} is not fully reviewed.")
                head = str(head_effective.value)
                if head in {"claimed_amount", "final_payable", "approved_amount", "payable"}:
                    continue
                amount_fact = self._fact_for_effective(
                    f"settlement.line.{row.record_key}.amount_paise", amount_result,
                    f"settlement.line.{row.record_key}.amount", evidence_map,
                )
                facts.append(amount_fact)
                deductions.append(SettlementDeduction(
                    head=head,
                    amount=amount_fact.as_money(),
                    raw_head_text=evidence.quoted_text,
                    evidence_id=str(evidence.evidence_id),
                ))
            settlement = SettlementFacts(
                settlement_id=f"STL-{documents['settlement'].document_id.hex}",
                claimed_amount=claimed.as_money(),
                deductions=tuple(deductions),
                final_payable=final_payable.as_money(),
                document_id=str(documents["settlement"].document_id),
                repudiated=repudiated,
                partial_disallowance=partial_disallowance,
            )

            return StructuredCase(
                case_id=str(case_id),
                corpus_snapshot_id=f"case-input-{case_id.hex}-{case.input_revision}",
                origin="user_upload",
                policy=policy,
                bill=bill,
                settlement=settlement,
                facts=FactStore(facts),
                pages=PageIndex(pages),
                documents=role_to_document_id,
                evidence=evidence_map,
                as_of=None,
                input_revision=case.input_revision,
                history_notes=(
                    f"persisted owner-reviewed input revision {case.input_revision}",
                    "human-visual corrections are user-origin facts with page/source provenance; "
                    "no quote or text-match evidence is synthesized",
                    "policy_schedule parser remains unavailable; schedule facts require review",
                    *review_provenance_notes,
                ),
            )

    def _optional_date(self, case: Case, documents: dict[str, CaseDocument],
                       runs: dict[str, DocumentProcessingRun], path: str,
                       role: str, review_notes: list[str]) -> date | None:
        result = resolve_effective_value(
            self.session, self.owner_id, case, documents[role], runs[role], path
        )
        self._remember_visual_review(review_notes, path, result.effective)
        if result.effective is None:
            return None
        return date.fromisoformat(result.effective.value)

    @staticmethod
    def _fact_review_note(effective) -> str | None:
        if effective.correction_id is None:
            return None
        if effective.verification_method == "HUMAN_VISUAL":
            return (f"HUMAN_VISUAL correction {effective.correction_id} "
                    f"on source page {effective.page_number}")
        return f"human review correction {effective.correction_id}"

    @staticmethod
    def _remember_visual_review(notes: list[str], field_path: str, effective) -> None:
        if (effective is None or effective.correction_id is None
                or effective.verification_method != "HUMAN_VISUAL"):
            return
        note = (f"{field_path}: HUMAN_VISUAL correction {effective.correction_id} "
                f"on source page {effective.page_number}")
        if note not in notes:
            notes.append(note)

    def _load_pages(self, document: CaseDocument, run: DocumentProcessingRun,
                    page_rows: list[StoredPageExtraction]) -> list[Page]:
        output: list[Page] = []
        for row in page_rows:
            if row.layout_state == "READ" and row.layout_text_artifact_key:
                key, digest, method = (row.layout_text_artifact_key,
                                       row.layout_text_sha256, row.layout_method)
            elif row.native_state == "READ" and row.native_text_artifact_key:
                key, digest, method = (row.native_text_artifact_key,
                                       row.native_text_sha256, row.native_method)
            else:
                continue
            if not key or not digest:
                continue
            try:
                raw = self.storage.get(key)
            except (FileNotFoundError, InvalidStorageKey, OSError) as exc:
                raise _artifact_unavailable() from exc
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ApplicationError(409, "PAGE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                       "A current page artifact failed integrity verification.")
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ApplicationError(409, "PAGE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                       "A current page artifact failed integrity verification.") from exc
            quality = row.quality_json or {}
            flags = tuple(str(flag) for flag in quality.get("flags", []))
            if "NO_TEXT" in flags:
                score = 0.0
            elif any(flag.startswith("LOW_OCR_CONFIDENCE_LINES:") for flag in flags):
                score = 0.5
            else:
                score = 1.0
            output.append(Page(
                page_id=f"{document.document_id}:{row.page_number}",
                document_id=str(document.document_id),
                page_number=row.page_number,
                text=text,
                width_px=None,
                height_px=None,
                quality_score=score,
                quality_codes=flags,
                text_layer_used=method != "ocr",
            ))
        return output

    @staticmethod
    def _domain_evidence(row: DocumentEvidenceSpan, quote: str,
                         run: DocumentProcessingRun) -> Evidence:
        try:
            verify_method = VerifyMethod(row.verification_method.lower())
        except ValueError:
            verify_method = VerifyMethod.REJECTED
        bbox = None
        if isinstance(row.bbox_json, list) and len(row.bbox_json) == 4:
            x0, y0, x1, y1 = (float(value) for value in row.bbox_json)
            bbox = ((x0, y0), (x1, y1))
        return Evidence(
            evidence_id=str(row.evidence_id),
            document_id=str(row.document_id),
            document_role=_ROLE_ENUM[run.assigned_role],
            page_number=row.page_number,
            quoted_text=quote,
            page_text_hash=row.page_text_sha256,
            char_start=row.char_start,
            char_end=row.char_end,
            bbox=bbox,
            verified=verify_method,
            verify_score=row.verify_score,
            extraction=ExtractionMeta(
                method=row.extraction_method,
                engine="claimcheck-existing-readers",
                engine_version=run.extraction_version,
                confidence=1.0,
                dual_read=False,
            ),
            quality_score=None,
            note=row.reason_code,
            injection_flags=tuple(row.injection_flags_json or ()),
        )

    def _fact_for_effective(self, field_path: str, result: EffectiveValueResult,
                            fact_id: str, evidence_map: dict[str, Evidence]) -> Fact:
        effective = result.effective
        if effective is None:
            raise _not_ready(f"A required value for {field_path} is not trusted.")
        supporting = evidence_map.get(str(effective.evidence_id)) if effective.evidence_id else None
        return Fact(
            fact_id=fact_id,
            type="loadbearing.money",
            value=effective.value,
            unit="inr_paise",
            origin=Origin.USER if effective.origin == "USER" else Origin.PRINTED,
            status=FactStatus.READ,
            confidence=1.0,
            evidence_id=str(effective.evidence_id) if effective.evidence_id else None,
            raw_text=supporting.quoted_text if supporting else None,
            note=self._fact_review_note(effective),
        )


def _not_ready(detail: str) -> ApplicationError:
    return ApplicationError(409, "TRUSTED_INPUT_NOT_READY", "Trusted input is incomplete", detail)


def _artifact_unavailable() -> ApplicationError:
    return ApplicationError(503, "PAGE_ARTIFACT_UNAVAILABLE", "Evidence unavailable",
                            "A current page artifact could not be retrieved.", retryable=True)


def _not_found() -> ApplicationError:
    return ApplicationError(404, "NOT_FOUND", "Not found", "The requested resource was not found.")
