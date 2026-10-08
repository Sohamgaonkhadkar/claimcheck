"""Deterministic Phase 4 readiness over explicit roles and evidence-bound inputs.

This application service reads persisted candidates; it never mutates extraction rows and
never invokes the analysis pipeline. A human correction is usable only when it is the latest
correction for the same processing run and its stored scope/hash/evidence still matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from claimcheck.persistence.models import (
    AnalysisRun,
    Case,
    CaseDocument,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    DocumentRoleAssignment,
    ReviewCorrection,
)
from claimcheck.schema import CanonicalCategory

REQUIRED_ROLES = ("policy_wording", "policy_schedule", "bill", "settlement")


class ReadinessState(StrEnum):
    READY_FOR_ANALYSIS = "READY_FOR_ANALYSIS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    ROLE_GAP = "ROLE_GAP"


@dataclass(frozen=True)
class ReadinessGap:
    code: str
    message: str
    role: str | None = None
    field_path: str | None = None
    document_id: UUID | None = None
    processing_run_id: UUID | None = None
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "role": self.role,
            "field_path": self.field_path,
            "document_id": self.document_id,
            "processing_run_id": self.processing_run_id,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ReadinessReport:
    case_id: UUID
    status: ReadinessState
    input_revision: int
    selected_roles: dict[str, UUID | None]
    gaps: tuple[ReadinessGap, ...]

    @property
    def ready(self) -> bool:
        return self.status is ReadinessState.READY_FOR_ANALYSIS

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "input_revision": self.input_revision,
            "ready_for_analysis": self.ready,
            "selected_roles": self.selected_roles,
            "gaps": [gap.as_dict() for gap in self.gaps],
        }


@dataclass(frozen=True)
class FieldSpec:
    roles: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class EffectiveValue:
    value: Any
    origin: str
    verification_method: str
    evidence_id: UUID | None
    page_number: int | None
    correction_id: UUID | None
    candidate_field_id: UUID | None
    candidate_record_id: UUID | None
    raw_text: str | None


@dataclass(frozen=True)
class EffectiveValueResult:
    effective: EffectiveValue | None
    reason: str
    candidate: DocumentProcessingField | None
    correction: ReviewCorrection | None


_DYNAMIC_BILL_AMOUNT = re.compile(r"^bill\.line\.([A-Za-z0-9._:-]{1,120})\.amount_paise$")
_DYNAMIC_BILL_CATEGORY = re.compile(r"^bill\.line\.([A-Za-z0-9._:-]{1,120})\.category$")
_DYNAMIC_SETTLEMENT_AMOUNT = re.compile(
    r"^settlement\.line\.([A-Za-z0-9._:-]{1,120})\.amount_paise$"
)
_DYNAMIC_SETTLEMENT_HEAD = re.compile(
    r"^settlement\.line\.([A-Za-z0-9._:-]{1,120})\.head$"
)


def field_spec(field_path: str) -> FieldSpec | None:
    """Allow-list of values the review boundary may turn into trusted domain inputs."""
    fixed = {
        "policy.uin": FieldSpec(("policy_wording",), "text"),
        "policy.product": FieldSpec(("policy_wording",), "text"),
        "policy.effective_text": FieldSpec(("policy_wording",), "text"),
        "policy.sum_insured_paise": FieldSpec(("policy_schedule",), "money"),
        "policy.room_rent_limit_paise": FieldSpec(
            ("policy_schedule", "policy_wording"), "money"
        ),
        "policy.co_pay_percent": FieldSpec(
            ("policy_schedule", "policy_wording"), "percent"
        ),
        "policy.ame_definition_present": FieldSpec(
            ("policy_schedule", "policy_wording"), "bool"
        ),
        "policy.claim_date": FieldSpec(("policy_schedule", "policy_wording"), "date"),
        "policy.inception_date": FieldSpec(("policy_schedule",), "date"),
        "policy.renewal_date": FieldSpec(("policy_schedule",), "date"),
        "bill.total_paise": FieldSpec(("bill",), "money"),
        "settlement.claimed_amount_paise": FieldSpec(("settlement",), "money"),
        "settlement.final_payable_paise": FieldSpec(("settlement",), "money"),
        "settlement.partial_disallowance": FieldSpec(("settlement",), "bool"),
        "settlement.repudiated": FieldSpec(("settlement",), "bool"),
    }
    if field_path in fixed:
        return fixed[field_path]
    if _DYNAMIC_BILL_AMOUNT.fullmatch(field_path):
        return FieldSpec(("bill",), "money")
    if _DYNAMIC_BILL_CATEGORY.fullmatch(field_path):
        return FieldSpec(("bill",), "category")
    if _DYNAMIC_SETTLEMENT_AMOUNT.fullmatch(field_path):
        return FieldSpec(("settlement",), "money")
    if _DYNAMIC_SETTLEMENT_HEAD.fullmatch(field_path):
        return FieldSpec(("settlement",), "head")
    return None


def validate_field_value(field_path: str, value: Any) -> Any:
    """Type-check review values without coercion, float money, or category guessing."""
    spec = field_spec(field_path)
    if spec is None:
        raise ValueError("unsupported field path")
    if spec.kind == "money":
        if type(value) is not int or value < 0 or value > 10**14:
            raise ValueError("money fields require nonnegative integer paise")
        return value
    if spec.kind == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise ValueError("text field is empty or too long")
        return value.strip()
    if spec.kind == "percent":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("percentage must be an integer or exact decimal string")
        rendered = str(value)
        if not re.fullmatch(r"(?:100|(?:[0-9]{1,2})(?:\.[0-9]{1,2})?)", rendered):
            raise ValueError("percentage is outside the supported exact range")
        if Fraction(rendered) > 100:
            raise ValueError("percentage is outside the supported exact range")
        return rendered
    if spec.kind == "bool":
        if type(value) is not bool:
            raise ValueError("boolean field requires true or false")
        return value
    if spec.kind == "category":
        if not isinstance(value, str):
            raise ValueError("category must be an explicit canonical category")
        try:
            category = CanonicalCategory(value)
        except ValueError as exc:
            raise ValueError("category is not in the canonical category set") from exc
        if category is CanonicalCategory.UNMAPPED:
            raise ValueError("UNMAPPED is not a trusted category")
        return category.value
    if spec.kind == "head":
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise ValueError("settlement head must be a valid string")
        return value
    if spec.kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("date must be an ISO calendar date")
        from datetime import date
        date.fromisoformat(value)
        return value
    raise ValueError("unsupported field type")


def latest_role_assignments(session: Session, owner_id: UUID,
                            case_id: UUID) -> dict[str, DocumentRoleAssignment]:
    rows = list(session.scalars(select(DocumentRoleAssignment).where(
        DocumentRoleAssignment.owner_id == owner_id,
        DocumentRoleAssignment.case_id == case_id,
    ).order_by(DocumentRoleAssignment.role, DocumentRoleAssignment.input_revision.desc(),
               DocumentRoleAssignment.assignment_id.desc())))
    latest: dict[str, DocumentRoleAssignment] = {}
    for row in rows:
        latest.setdefault(row.role, row)
    return latest


def latest_processing_run(session: Session, owner_id: UUID, case_id: UUID,
                          document_id: UUID) -> DocumentProcessingRun | None:
    return session.scalar(select(DocumentProcessingRun).where(
        DocumentProcessingRun.owner_id == owner_id,
        DocumentProcessingRun.case_id == case_id,
        DocumentProcessingRun.document_id == document_id,
    ).order_by(DocumentProcessingRun.run_number.desc()).limit(1))


def candidate_field(session: Session, owner_id: UUID, case_id: UUID, document_id: UUID,
                    run_id: UUID, field_path: str) -> DocumentProcessingField | None:
    return session.scalar(select(DocumentProcessingField).where(
        DocumentProcessingField.owner_id == owner_id,
        DocumentProcessingField.case_id == case_id,
        DocumentProcessingField.document_id == document_id,
        DocumentProcessingField.processing_run_id == run_id,
        DocumentProcessingField.field_path == field_path,
    ))


def latest_correction(session: Session, owner_id: UUID, case_id: UUID, document_id: UUID,
                      run_id: UUID, field_path: str) -> ReviewCorrection | None:
    return session.scalar(select(ReviewCorrection).where(
        ReviewCorrection.owner_id == owner_id,
        ReviewCorrection.case_id == case_id,
        ReviewCorrection.document_id == document_id,
        ReviewCorrection.processing_run_id == run_id,
        ReviewCorrection.field_path == field_path,
    ).order_by(ReviewCorrection.correction_number.desc()).limit(1))


def processing_record_for_field(session: Session, owner_id: UUID, case_id: UUID,
                                document_id: UUID, run_id: UUID,
                                field_path: str) -> DocumentProcessingRecord | None:
    """Return the one typed parser record an allow-listed dynamic path refers to."""
    record_type = None
    record_key = None
    match = re.fullmatch(
        r"bill\.line\.([A-Za-z0-9._:-]{1,120})\.(?:amount_paise|category)", field_path
    )
    if match:
        record_type, record_key = "BILL_LINE", match.group(1)
    match = re.fullmatch(
        r"settlement\.line\.([A-Za-z0-9._:-]{1,120})\.(?:amount_paise|head)",
        field_path,
    )
    if match:
        record_type, record_key = "SETTLEMENT_LINE", match.group(1)
    if record_type is not None:
        return session.scalar(select(DocumentProcessingRecord).where(
            DocumentProcessingRecord.owner_id == owner_id,
            DocumentProcessingRecord.case_id == case_id,
            DocumentProcessingRecord.document_id == document_id,
            DocumentProcessingRecord.processing_run_id == run_id,
            DocumentProcessingRecord.record_type == record_type,
            DocumentProcessingRecord.record_key == record_key,
        ))
    if field_path == "bill.total_paise":
        return session.scalar(select(DocumentProcessingRecord).where(
            DocumentProcessingRecord.owner_id == owner_id,
            DocumentProcessingRecord.case_id == case_id,
            DocumentProcessingRecord.document_id == document_id,
            DocumentProcessingRecord.processing_run_id == run_id,
            DocumentProcessingRecord.record_type == "BILL_TOTAL",
        ).order_by(DocumentProcessingRecord.record_key).limit(1))
    return None


def _evidence_row(session: Session, owner_id: UUID, case_id: UUID, document_id: UUID,
                  run_id: UUID, evidence_id: UUID | None) -> DocumentEvidenceSpan | None:
    if evidence_id is None:
        return None
    return session.scalar(select(DocumentEvidenceSpan).where(
        DocumentEvidenceSpan.owner_id == owner_id,
        DocumentEvidenceSpan.case_id == case_id,
        DocumentEvidenceSpan.document_id == document_id,
        DocumentEvidenceSpan.processing_run_id == run_id,
        DocumentEvidenceSpan.evidence_id == evidence_id,
    ))


def _candidate_evidence_valid(session: Session, owner_id: UUID, case_id: UUID,
                              document: CaseDocument, run: DocumentProcessingRun,
                              candidate: DocumentProcessingField) -> bool:
    evidence = _evidence_row(session, owner_id, case_id, document.document_id,
                             run.processing_run_id, candidate.evidence_id)
    if evidence is None:
        return False
    
    if evidence.document_id != document.document_id or \
       evidence.processing_run_id != run.processing_run_id or \
       evidence.source_sha256 != document.sha256 or document.sha256 != run.source_sha256:
        return False
    return bool(candidate.state == "VERIFIED" and evidence.verification_status == "VERIFIED")


def resolve_effective_value(session: Session, owner_id: UUID, case: Case,
                            document: CaseDocument, run: DocumentProcessingRun,
                            field_path: str) -> EffectiveValueResult:
    """Resolve only a verified candidate or the latest scope/hash-valid review event."""
    spec = field_spec(field_path)
    if spec is None or run.assigned_role not in spec.roles:
        return EffectiveValueResult(None, "UNSUPPORTED_OR_WRONG_ROLE", None, None)
    candidate = candidate_field(session, owner_id, case.case_id, document.document_id,
                                run.processing_run_id, field_path)
    record = processing_record_for_field(
        session, owner_id, case.case_id, document.document_id,
        run.processing_run_id, field_path,
    )
    correction = latest_correction(session, owner_id, case.case_id, document.document_id,
                                   run.processing_run_id, field_path)
    if (document.state in {"DELETE_PENDING", "DELETED"} or not document.storage_key
            or document.sha256 == "0" * 64):
        return EffectiveValueResult(None, "SOURCE_UNAVAILABLE", candidate, correction)
    if run.source_sha256 != document.sha256:
        return EffectiveValueResult(None, "SOURCE_HASH_MISMATCH", candidate, correction)

    if correction is not None:
        if correction.source_sha256 != document.sha256:
            return EffectiveValueResult(None, "SOURCE_HASH_MISMATCH", candidate, correction)
        expected_candidate_id = candidate.processing_field_id if candidate is not None else None
        expected_record_id = record.processing_record_id if record is not None else None
        expected_prior_state = candidate.state if candidate is not None else "MISSING"
        expected_prior_value = candidate.value_json if candidate is not None else None
        if (correction.candidate_field_id != expected_candidate_id
                or correction.prior_state != expected_prior_state
                or correction.prior_value_json != expected_prior_value):
            return EffectiveValueResult(None, "CANDIDATE_REFERENCE_MISMATCH", candidate,
                                        correction)
        if correction.candidate_record_id != expected_record_id:
            return EffectiveValueResult(None, "CANDIDATE_RECORD_REFERENCE_MISMATCH", candidate,
                                        correction)
        if correction.action == "UNRESOLVED":
            return EffectiveValueResult(None, "UNRESOLVED", candidate, correction)
        if correction.action == "CONFIRM":
            if candidate is None:
                return EffectiveValueResult(None, "CANDIDATE_REFERENCE_MISMATCH", candidate,
                                            correction)
            value = candidate.value_json
            evidence_id = correction.evidence_id
        elif correction.action == "CORRECT":
            value = correction.corrected_value_json
            evidence_id = correction.evidence_id
        else:
            return EffectiveValueResult(None, "INVALID_CORRECTION_ACTION", candidate, correction)

        try:
            value = validate_field_value(field_path, value)
        except (TypeError, ValueError):
            return EffectiveValueResult(None, "CORRECTION_VALUE_INVALID", candidate, correction)

        if correction.verification_method == "EVIDENCE_SPAN":
            allowed_evidence_ids = {
                item for item in (
                    candidate.evidence_id if candidate is not None else None,
                    record.evidence_id if record is not None else None,
                ) if item is not None
            }
            if evidence_id not in allowed_evidence_ids:
                return EffectiveValueResult(None, "CORRECTION_EVIDENCE_NOT_TIED_TO_FIELD",
                                            candidate, correction)
            evidence = _evidence_row(session, owner_id, case.case_id, document.document_id,
                                     run.processing_run_id, evidence_id)
            if (evidence is None or evidence.verification_status != "VERIFIED"
                    or evidence.source_sha256 != document.sha256
                    or evidence.page_number < 1 or evidence.page_number > document.page_count):
                return EffectiveValueResult(None, "CORRECTION_EVIDENCE_INVALID", candidate,
                                            correction)
            page_number = evidence.page_number
        elif correction.verification_method == "HUMAN_VISUAL":
            if (correction.evidence_id is not None or correction.page_number is None
                    or not 1 <= correction.page_number <= document.page_count):
                return EffectiveValueResult(None, "CORRECTION_VISUAL_PROVENANCE_INVALID",
                                            candidate, correction)
            page_number = correction.page_number
        else:
            return EffectiveValueResult(None, "CORRECTION_VERIFICATION_MISSING", candidate,
                                        correction)
        origin = "PRINTED" if correction.action == "CONFIRM" and \
            correction.verification_method == "EVIDENCE_SPAN" else "USER"
        # Source text is intentionally not copied here; the adapter revalidates and loads
        # it from the hash-bound page/evidence artifacts before constructing domain facts.
        raw_text = None
        return EffectiveValueResult(EffectiveValue(
            value=value,
            origin=origin,
            verification_method=correction.verification_method,
            evidence_id=evidence_id,
            page_number=page_number,
            correction_id=correction.correction_id,
            candidate_field_id=correction.candidate_field_id,
            candidate_record_id=correction.candidate_record_id,
            raw_text=raw_text,
        ), "TRUSTED_CORRECTION", candidate, correction)

    if candidate is None:
        return EffectiveValueResult(None, "MISSING", None, None)
    if not _candidate_evidence_valid(session, owner_id, case.case_id, document, run, candidate):
        state = candidate.state
        reason = "EVIDENCE_UNVERIFIED" if state == "VERIFIED" else state
        return EffectiveValueResult(None, reason, candidate, None)
    try:
        value = validate_field_value(field_path, candidate.value_json)
    except (TypeError, ValueError):
        return EffectiveValueResult(None, "CANDIDATE_VALUE_INVALID", candidate, None)
    evidence = _evidence_row(session, owner_id, case.case_id, document.document_id,
                             run.processing_run_id, candidate.evidence_id)
    return EffectiveValueResult(EffectiveValue(
        value=value,
        origin="PRINTED",
        verification_method="EVIDENCE_SPAN",
        evidence_id=candidate.evidence_id,
        page_number=evidence.page_number if evidence is not None else None,
        correction_id=None,
        candidate_field_id=candidate.processing_field_id,
        candidate_record_id=None,
        raw_text=None,
    ), "VERIFIED_CANDIDATE", candidate, None)


def _record_evidence_valid(session: Session, owner_id: UUID, case: Case,
                           document: CaseDocument, run: DocumentProcessingRun,
                           record: DocumentProcessingRecord) -> bool:
    evidence = _evidence_row(session, owner_id, case.case_id, document.document_id,
                             run.processing_run_id, record.evidence_id)
    return bool(
        record.field_state == "VERIFIED"
        and evidence is not None
        and evidence.verification_status == "VERIFIED"
        and evidence.source_sha256 == document.sha256 == run.source_sha256
    )


def _selected_context(session: Session, owner_id: UUID, case: Case,
                      role: str) -> tuple[CaseDocument | None, DocumentProcessingRun | None,
                                          DocumentRoleAssignment | None]:
    assignment = latest_role_assignments(session, owner_id, case.case_id).get(role)
    if assignment is None or assignment.document_id is None:
        return None, None, assignment
    document = session.scalar(select(CaseDocument).where(
        CaseDocument.owner_id == owner_id,
        CaseDocument.case_id == case.case_id,
        CaseDocument.document_id == assignment.document_id,
    ))
    run = latest_processing_run(session, owner_id, case.case_id, assignment.document_id)
    return document, run, assignment


def evaluate_case_readiness(session: Session, owner_id: UUID, case: Case) -> ReadinessReport:
    """Compute case readiness deterministically from current explicit selections.

    Missing semantic facts are evidence gaps; present but proposed/conflicting/unverified
    candidates are review gaps. Role assignments always take precedence over upload labels.
    """
    gaps: list[ReadinessGap] = []
    selected_roles: dict[str, UUID | None] = {}
    assignments = latest_role_assignments(session, owner_id, case.case_id)
    any_role_gap = False
    any_review_gap = False
    any_evidence_gap = False
    contexts: dict[str, tuple[CaseDocument, DocumentProcessingRun, DocumentRoleAssignment]] = {}

    for role in REQUIRED_ROLES:
        assignment = assignments.get(role)
        if assignment is None or assignment.document_id is None:
            selected_roles[role] = None
            candidates = list(session.scalars(select(CaseDocument).where(
                CaseDocument.owner_id == owner_id,
                CaseDocument.case_id == case.case_id,
                CaseDocument.role == role,
                CaseDocument.state.not_in(("DELETE_PENDING", "DELETED")),
            )))
            if len(candidates) > 1:
                code = "MULTIPLE_ROLE_CANDIDATES_REQUIRE_SELECTION"
                message = f"Choose one explicit source for {role}; no candidate is selected."
            else:
                code = "ROLE_SOURCE_NOT_ASSIGNED"
                message = f"No active source is explicitly assigned to {role}."
            gaps.append(ReadinessGap(code, message, role=role))
            any_role_gap = True
            continue
        selected_roles[role] = assignment.document_id
        document = session.scalar(select(CaseDocument).where(
            CaseDocument.owner_id == owner_id,
            CaseDocument.case_id == case.case_id,
            CaseDocument.document_id == assignment.document_id,
        ))
        if document is None or document.state in {"DELETE_PENDING", "DELETED"}:
            gaps.append(ReadinessGap(
                "ASSIGNED_ROLE_SOURCE_UNAVAILABLE",
                f"The selected {role} source is unavailable; select an active document.",
                role=role, document_id=assignment.document_id,
            ))
            any_role_gap = True
            continue
        candidates = list(session.scalars(select(CaseDocument).where(
            CaseDocument.owner_id == owner_id,
            CaseDocument.case_id == case.case_id,
            CaseDocument.role == role,
            CaseDocument.state.not_in(("DELETE_PENDING", "DELETED")),
        )))
        if len(candidates) > 1:
            gaps.append(ReadinessGap(
                "UNSELECTED_ROLE_CANDIDATES_REMAIN",
                f"Additional {role} files are not selected and will not enter the case.",
                role=role, document_id=assignment.document_id, blocking=False,
            ))
        if assignment.source_sha256 != document.sha256:
            gaps.append(ReadinessGap(
                "ROLE_ASSIGNMENT_SOURCE_HASH_MISMATCH",
                f"The selected {role} source hash changed; assign or process the current source.",
                role=role, document_id=document.document_id,
            ))
            any_review_gap = True
            continue
        run = latest_processing_run(session, owner_id, case.case_id, document.document_id)
        if run is None or run.status in {"QUEUED", "RUNNING"}:
            gaps.append(ReadinessGap(
                "PROCESSING_RUN_PENDING",
                f"The selected {role} source has no completed processing candidate.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id if run else None,
            ))
            any_review_gap = True
            continue
        if run.source_sha256 != document.sha256:
            gaps.append(ReadinessGap(
                "PROCESSING_SOURCE_HASH_MISMATCH",
                f"The latest {role} processing run does not match the current source hash.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id,
            ))
            any_review_gap = True
            continue
        if run.assigned_role != role:
            gaps.append(ReadinessGap(
                "PROCESSING_ROLE_MISMATCH",
                f"The latest run for the selected source was processed as another role.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id,
            ))
            any_review_gap = True
            continue
        if run.role_mismatch and not assignment.resolved_mismatch:
            gaps.append(ReadinessGap(
                "ROLE_MISMATCH_REQUIRES_EXPLICIT_RESOLUTION",
                f"The detected document role differs from {role}; explicitly resolve the mismatch.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id,
            ))
            any_review_gap = True
        elif run.role_mismatch:
            gaps.append(ReadinessGap(
                "ROLE_MISMATCH_EXPLICITLY_RESOLVED",
                f"The detected role differs from {role}; the owner explicitly retained {role}.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id, blocking=False,
            ))
        if run.status in {"FAILED", "SUPERSEDED"}:
            gaps.append(ReadinessGap(
                "PROCESSING_RUN_NOT_USABLE",
                f"The latest {role} run is not usable; process the current source again.",
                role=role, document_id=document.document_id,
                processing_run_id=run.processing_run_id,
            ))
            any_review_gap = True
            continue
        contexts[role] = (document, run, assignment)

    if len(contexts) == len(REQUIRED_ROLES):
        # This warning is deliberately retained even when human review supplies the needed
        # schedule facts. Phase 4 does not claim that a schedule parser now exists.
        schedule_doc, schedule_run, _ = contexts["policy_schedule"]
        schedule_gap = candidate_field(session, owner_id, case.case_id, schedule_doc.document_id,
                                       schedule_run.processing_run_id, "policy_schedule.records")
        if schedule_gap is not None and schedule_gap.reason_code == "PARSER_NOT_AVAILABLE":
            gaps.append(ReadinessGap(
                "POLICY_SCHEDULE_PARSER_UNAVAILABLE",
                "No policy-schedule parser is available; schedule values require explicit review.",
                role="policy_schedule", document_id=schedule_doc.document_id,
                processing_run_id=schedule_run.processing_run_id, blocking=False,
            ))

        required_paths = (
            ("policy.sum_insured_paise", "policy_schedule"),
            ("bill.total_paise", "bill"),
            ("settlement.claimed_amount_paise", "settlement"),
            ("settlement.final_payable_paise", "settlement"),
            ("settlement.partial_disallowance", "settlement"),
            ("settlement.repudiated", "settlement"),
        )
        for path, role in required_paths:
            document, run, _assignment = contexts[role]
            result = resolve_effective_value(session, owner_id, case, document, run, path)
            if result.effective is not None:
                continue
            correction = result.correction
            candidate = result.candidate
            if correction is not None and correction.action == "UNRESOLVED":
                gaps.append(ReadinessGap(
                    "REVIEW_REMAINS_UNRESOLVED",
                    f"The reviewed value for {path} is explicitly unresolved.",
                    role=role, field_path=path, document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                ))
                any_review_gap = True
            elif result.reason in {
                "PROPOSED", "NEEDS_REVIEW", "QUARANTINED", "CONFLICTING", "UNREADABLE",
                "EVIDENCE_UNVERIFIED", "CANDIDATE_VALUE_INVALID",
                "SOURCE_UNAVAILABLE", "SOURCE_HASH_MISMATCH", "CORRECTION_EVIDENCE_INVALID",
                "CORRECTION_EVIDENCE_NOT_TIED_TO_FIELD", "CORRECTION_VISUAL_PROVENANCE_INVALID",
                "CORRECTION_VERIFICATION_MISSING", "CORRECTION_VALUE_INVALID",
                "CANDIDATE_REFERENCE_MISMATCH", "CANDIDATE_RECORD_REFERENCE_MISMATCH",
                "INVALID_CORRECTION_ACTION",
            }:
                gaps.append(ReadinessGap(
                    "FIELD_REQUIRES_REVIEW",
                    f"The value for {path} is present but not yet trusted.",
                    role=role, field_path=path, document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                ))
                any_review_gap = True
            else:
                gaps.append(ReadinessGap(
                    "REQUIRED_FIELD_MISSING",
                    f"A trusted value for {path} is required before analysis readiness.",
                    role=role, field_path=path, document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                ))
                any_evidence_gap = True

        bill_doc, bill_run, _ = contexts["bill"]
        bill_records = list(session.scalars(select(DocumentProcessingRecord).where(
            DocumentProcessingRecord.owner_id == owner_id,
            DocumentProcessingRecord.case_id == case.case_id,
            DocumentProcessingRecord.document_id == bill_doc.document_id,
            DocumentProcessingRecord.processing_run_id == bill_run.processing_run_id,
            DocumentProcessingRecord.record_type == "BILL_LINE",
        ).order_by(DocumentProcessingRecord.record_key)))
        item_records = [record for record in bill_records
                        if not bool((record.record_json or {}).get("is_summary_row"))]
        if not item_records:
            gaps.append(ReadinessGap(
                "BILL_ITEM_LINES_MISSING",
                "At least one evidence-backed bill item line is required.",
                role="bill", document_id=bill_doc.document_id,
                processing_run_id=bill_run.processing_run_id,
            ))
            any_evidence_gap = True
        for record in item_records:
            key = record.record_key
            if not _record_evidence_valid(session, owner_id, case, bill_doc, bill_run, record):
                gaps.append(ReadinessGap(
                    "BILL_LINE_EVIDENCE_UNVERIFIED",
                    f"Bill line {key} lacks a verified source span.", role="bill",
                    field_path=f"bill.line.{key}", document_id=bill_doc.document_id,
                    processing_run_id=bill_run.processing_run_id,
                ))
                any_review_gap = True
            for path in (f"bill.line.{key}.amount_paise", f"bill.line.{key}.category"):
                result = resolve_effective_value(session, owner_id, case, bill_doc, bill_run, path)
                if result.effective is not None:
                    continue
                code = ("BILL_LINE_VALUE_REQUIRES_REVIEW"
                        if result.candidate is not None or path.endswith(".category")
                        else "BILL_LINE_VALUE_MISSING")
                gaps.append(ReadinessGap(
                    code,
                    f"A trusted value for {path} is required for the bill line.",
                    role="bill", field_path=path, document_id=bill_doc.document_id,
                    processing_run_id=bill_run.processing_run_id,
                ))
                if result.correction is not None and result.correction.action == "UNRESOLVED":
                    any_review_gap = True
                elif (result.candidate is not None or result.reason not in {"MISSING"}
                      or path.endswith(".category")):
                    any_review_gap = True
                else:
                    any_evidence_gap = True

        settlement_doc, settlement_run, _ = contexts["settlement"]
        settlement_records = list(session.scalars(select(DocumentProcessingRecord).where(
            DocumentProcessingRecord.owner_id == owner_id,
            DocumentProcessingRecord.case_id == case.case_id,
            DocumentProcessingRecord.document_id == settlement_doc.document_id,
            DocumentProcessingRecord.processing_run_id == settlement_run.processing_run_id,
            DocumentProcessingRecord.record_type == "SETTLEMENT_LINE",
        ).order_by(DocumentProcessingRecord.record_key)))
        for record in settlement_records:
            key = record.record_key
            if not _record_evidence_valid(session, owner_id, case, settlement_doc,
                                          settlement_run, record):
                gaps.append(ReadinessGap(
                    "SETTLEMENT_LINE_EVIDENCE_UNVERIFIED",
                    f"Settlement line {key} lacks a verified source span.", role="settlement",
                    field_path=f"settlement.line.{key}", document_id=settlement_doc.document_id,
                    processing_run_id=settlement_run.processing_run_id,
                ))
                any_review_gap = True
            for path in (f"settlement.line.{key}.amount_paise",
                         f"settlement.line.{key}.head"):
                result = resolve_effective_value(session, owner_id, case, settlement_doc,
                                                 settlement_run, path)
                if result.effective is not None:
                    continue
                gaps.append(ReadinessGap(
                    "SETTLEMENT_LINE_CLASSIFICATION_REQUIRED" if path.endswith(".head") else
                    "SETTLEMENT_LINE_VALUE_REQUIRES_REVIEW",
                    f"A trusted value for {path} is required for the settlement line.",
                    role="settlement", field_path=path, document_id=settlement_doc.document_id,
                    processing_run_id=settlement_run.processing_run_id,
                ))
                if result.correction is not None and result.correction.action == "UNRESOLVED":
                    any_review_gap = True
                elif (result.candidate is not None or result.reason not in {"MISSING"}
                      or path.endswith(".head")):
                    any_review_gap = True
                else:
                    any_evidence_gap = True

        policy_doc, policy_run, _ = contexts["policy_wording"]
        clauses = list(session.scalars(select(DocumentProcessingRecord).where(
            DocumentProcessingRecord.owner_id == owner_id,
            DocumentProcessingRecord.case_id == case.case_id,
            DocumentProcessingRecord.document_id == policy_doc.document_id,
            DocumentProcessingRecord.processing_run_id == policy_run.processing_run_id,
            DocumentProcessingRecord.record_type == "POLICY_CLAUSE",
        )))
        trusted_clause = any(_record_evidence_valid(
            session, owner_id, case, policy_doc, policy_run, record
        ) for record in clauses)
        if not trusted_clause:
            gaps.append(ReadinessGap(
                "POLICY_WORDING_CLAUSE_MISSING",
                "No verified policy-wording clause is available for the trusted case.",
                role="policy_wording", document_id=policy_doc.document_id,
                processing_run_id=policy_run.processing_run_id,
            ))
            if clauses:
                any_review_gap = True
            else:
                any_evidence_gap = True

    # Status priority is intentional: missing roles are not disguised as generic review or
    # evidence gaps; a present conflict then outranks absence of a separate required fact.
    if any_role_gap:
        status = ReadinessState.ROLE_GAP
    elif any_review_gap:
        status = ReadinessState.NEEDS_REVIEW
    elif any_evidence_gap:
        status = ReadinessState.EVIDENCE_GAP
    else:
        status = ReadinessState.READY_FOR_ANALYSIS
    return ReadinessReport(case.case_id, status, case.input_revision, selected_roles,
                           tuple(gaps))
