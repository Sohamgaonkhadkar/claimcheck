"""Typed HTTP DTOs for the Phase 1 demo API.

These models validate the transport contract; the existing domain/report objects remain the source
of all amounts and findings.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


FindingType = Literal["FINANCIAL", "PROCEDURAL", "EVIDENCE_GAP", "INFO"]
FindingState = Literal["CONSISTENT", "POTENTIALLY_INCONSISTENT", "UNDETERMINED"]
ReferenceKind = Literal["span", "document", "source_reference"]


class HealthResponse(ApiModel):
    status: Literal["ok"]


class ReadyResponse(ApiModel):
    status: Literal["ready"]


class CitationReference(ApiModel):
    reference_type: StrictStr
    reference_id: StrictStr
    title: StrictStr | None
    verified: StrictBool


class EvidenceReference(ApiModel):
    reference_id: StrictStr
    kind: ReferenceKind
    document_role: StrictStr
    page_number: StrictInt | None
    verification_status: StrictStr
    verification_method: StrictStr | None


class FindingView(ApiModel):
    finding_id: StrictStr
    type: FindingType
    state: FindingState
    head: StrictStr
    why: StrictStr
    amount_paise: StrictInt | None
    amount_display: StrictStr | None
    amount_gross_paise: StrictInt | None
    amount_gross_display: StrictStr | None
    amount_basis: StrictStr | None
    evidence_reference_ids: list[StrictStr]
    calculation_step_ids: list[StrictStr]
    citations: list[CitationReference]
    missing: list[StrictStr]
    limitations: list[StrictStr]
    gates_failed: list[StrictStr]


class OverallResult(ApiModel):
    state: FindingState
    head_states: dict[StrictStr, FindingState]
    supported_difference_display: StrictStr


class ReadingView(ApiModel):
    reading_id: StrictStr
    payable_paise: StrictInt
    payable_display: StrictStr
    difference_paise: StrictInt


class ReconciliationView(ApiModel):
    lawful_payable_paise: StrictInt
    lawful_payable_display: StrictStr
    paid_paise: StrictInt
    paid_display: StrictStr
    difference_paise: StrictInt
    difference_display: StrictStr
    supported_difference_paise: StrictInt
    supported_difference_display: StrictStr
    unexplained_paise: StrictInt
    unexplained_display: StrictStr
    restore_recompute_paise: StrictInt
    restore_recompute_display: StrictStr
    identity_ok: StrictBool
    summary: StrictStr
    readings: list[ReadingView]


class CalculationStepView(ApiModel):
    step_id: StrictStr
    step_type: StrictStr
    label: StrictStr
    base_label: StrictStr
    base_paise: StrictInt | None
    base_display: StrictStr | None
    output_paise: StrictInt
    output_display: StrictStr
    effect: StrictStr


class ProvenanceView(ApiModel):
    as_of: StrictStr | None
    rulepack_version: StrictStr
    arithmetic: StrictStr


class GoldenDemoResponse(ApiModel):
    case_id: StrictStr
    run_id: StrictStr
    synthetic_demo: StrictBool
    synthetic_notice: StrictStr
    overall_result: OverallResult
    financial_findings: list[FindingView]
    procedural_findings: list[FindingView]
    evidence_gaps: list[FindingView]
    informational_findings: list[FindingView]
    evidence_references: list[EvidenceReference]
    reconciliation: ReconciliationView
    calculation_steps: list[CalculationStepView]
    limitations: list[StrictStr]
    provenance: ProvenanceView


AnalysisRunStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


class AnalysisVerdictView(ApiModel):
    state: FindingState
    head_states: dict[StrictStr, FindingState]


class AnalysisReconciliationView(ApiModel):
    lawful_payable_paise: StrictInt
    paid_paise: StrictInt
    difference_paise: StrictInt
    supported_difference_paise: StrictInt
    unexplained_paise: StrictInt
    identity_ok: StrictBool


class AnalysisStepView(ApiModel):
    step_id: StrictStr
    step_type: StrictStr
    output_paise: StrictInt


class AnalysisCalculationView(ApiModel):
    gross_bill_paise: StrictInt
    lawful_payable_paise: StrictInt
    patient_share_paise: StrictInt
    reductions_paise: StrictInt
    steps: list[AnalysisStepView]


class AnalysisFindingView(ApiModel):
    finding_id: StrictStr
    type: FindingType
    state: FindingState
    head: StrictStr
    amount_paise: StrictInt | None
    calculation_step_ids: list[StrictStr]


class AnalysisResultView(ApiModel):
    verdict: AnalysisVerdictView
    reconciliation: AnalysisReconciliationView
    calculation: AnalysisCalculationView
    findings: list[AnalysisFindingView]
    withheld_findings_count: StrictInt
    rulepack_version: StrictStr
    corpus_snapshot_id: StrictStr | None


class AnalysisRunView(ApiModel):
    analysis_run_id: UUID
    case_id: UUID
    run_number: StrictInt
    input_revision: StrictInt | None
    status: AnalysisRunStatus
    rulepack_version: StrictStr | None
    corpus_snapshot_id: StrictStr | None
    error_code: StrictStr | None
    created_at: datetime
    completed_at: datetime | None
    result: AnalysisResultView | None


class AnalysisRunResponse(ApiModel):
    analysis_run: AnalysisRunView
    request_id: StrictStr


class AnalysisRunListResponse(ApiModel):
    case_id: UUID
    current_input_revision: StrictInt
    items: list[AnalysisRunView]
    request_id: StrictStr


class ProblemDetails(ApiModel):
    type: Literal["about:blank"] = "about:blank"
    title: StrictStr
    status: StrictInt
    code: StrictStr
    detail: StrictStr
    request_id: StrictStr
    retryable: StrictBool = False
    errors: list[dict[str, StrictStr]] = Field(default_factory=list)


CaseStatus = Literal[
    "CREATED", "AWAITING_DOCUMENTS", "PROCESSING", "NEEDS_REVIEW",
    "ANALYZED", "EVIDENCE_GAP", "READY_FOR_ANALYSIS", "ROLE_GAP", "FAILED", "DELETED",
]
DocumentRole = Literal["policy_wording", "policy_schedule", "bill", "settlement"]
DocumentStatus = Literal[
    "RECEIVED", "VALIDATING", "QUEUED", "PROCESSING", "NEEDS_REVIEW", "READY",
    "PARTIAL", "READY_FOR_ANALYSIS", "EVIDENCE_GAP", "FAILED", "DELETE_PENDING", "DELETED",
]
JobState = Literal["QUEUED", "RUNNING", "SUCCEEDED", "RETRYABLE_FAILURE", "FAILED", "CANCELLED"]
ChecklistState = Literal["MISSING", "PRESENT"]


class CaseCreateRequest(ApiModel):
    display_name: StrictStr | None = Field(default=None, max_length=160)
    claim_date: date | None = None


class CasePatchRequest(ApiModel):
    display_name: StrictStr | None = Field(default=None, max_length=160)
    claim_date: date | None = None


class CaseSummary(ApiModel):
    case_id: UUID
    display_name: StrictStr | None
    status: CaseStatus
    claim_date: date | None
    currency: Literal["INR"] = "INR"
    document_checklist: dict[StrictStr, ChecklistState]
    latest_analysis_run_id: UUID | None
    created_at: datetime
    updated_at: datetime
    version: StrictInt
    input_revision: StrictInt
    request_id: StrictStr


class CaseListResponse(ApiModel):
    items: list[CaseSummary]
    next_cursor: StrictStr | None
    request_id: StrictStr


class CaseDeletionResponse(ApiModel):
    case_id: UUID
    deletion_job_id: UUID | None
    status: Literal["DELETED"]
    request_id: StrictStr


class DocumentSummary(ApiModel):
    document_id: UUID
    case_id: UUID
    role: DocumentRole
    original_filename: StrictStr
    media_type: Literal["application/pdf"]
    byte_size: StrictInt
    page_count: StrictInt
    state: DocumentStatus
    uploaded_at: datetime
    selected_for_role: StrictBool
    request_id: StrictStr


class DocumentListResponse(ApiModel):
    items: list[DocumentSummary]
    next_cursor: StrictStr | None
    request_id: StrictStr


class JobStatusResponse(ApiModel):
    job_id: UUID
    case_id: UUID
    document_id: UUID | None
    analysis_run_id: UUID | None
    job_type: StrictStr
    stage: StrictStr
    status: JobState
    attempt_count: StrictInt
    max_attempts: StrictInt
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: StrictStr | None
    retryable: StrictBool
    progress: dict[StrictStr, StrictInt] | None
    request_id: StrictStr


class DocumentUploadResponse(ApiModel):
    document: DocumentSummary
    job: JobStatusResponse
    request_id: StrictStr


ProcessingRunState = Literal[
    "QUEUED", "RUNNING", "NEEDS_REVIEW", "EVIDENCE_GAP",
    "READY_FOR_ANALYSIS", "FAILED", "SUPERSEDED",
]
ProcessingFieldState = Literal[
    "VERIFIED", "PROPOSED", "NEEDS_REVIEW", "QUARANTINED",
    "MISSING", "UNREADABLE", "CONFLICTING",
]


class ProcessingRunView(ApiModel):
    processing_run_id: UUID
    run_number: StrictInt
    status: ProcessingRunState
    extraction_version: StrictStr
    assigned_role: DocumentRole
    detected_role: DocumentRole | None
    role_mismatch: StrictBool
    page_count: StrictInt
    summary: dict[StrictStr, Any]
    error_code: StrictStr | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DocumentDetail(DocumentSummary):
    processing_runs: list[ProcessingRunView]


class DocumentFieldView(ApiModel):
    field_path: StrictStr
    value: Any
    state: ProcessingFieldState
    evidence_id: UUID | None
    reason_code: StrictStr | None
    provenance: dict[StrictStr, Any]


class DocumentFieldsResponse(ApiModel):
    document_id: UUID
    processing_run: ProcessingRunView | None
    fields: list[DocumentFieldView]
    request_id: StrictStr


class EvidenceSpanView(ApiModel):
    evidence_id: UUID
    document_id: UUID
    document_role: DocumentRole
    processing_run_id: UUID
    page_number: StrictInt
    source_sha256: StrictStr
    page_text_sha256: StrictStr
    char_start: StrictInt | None
    char_end: StrictInt | None
    reader: StrictStr
    extraction_method: StrictStr
    verification_status: Literal["VERIFIED", "AMBIGUOUS", "REJECTED", "QUARANTINED"]
    verification_method: StrictStr
    verify_score: float
    bbox: list[float] | None
    bbox_is_measured: StrictBool
    injection_flags: list[StrictStr]
    reason_code: StrictStr | None
    quoted_text: StrictStr
    request_id: StrictStr


class JobListResponse(ApiModel):
    items: list[JobStatusResponse]
    next_cursor: StrictStr | None
    request_id: StrictStr


class ProcessingDocumentStatus(ApiModel):
    document_id: UUID
    assigned_role: DocumentRole
    selected_for_role: StrictBool
    state: DocumentStatus
    latest_run: ProcessingRunView | None


class ProcessingStatusResponse(ApiModel):
    case_id: UUID
    status: CaseStatus
    document_count: StrictInt
    documents: list[ProcessingDocumentStatus]
    job_counts: dict[StrictStr, StrictInt]
    active_jobs: StrictInt
    ready_for_analysis: StrictBool
    request_id: StrictStr


class DocumentDeleteResponse(ApiModel):
    document_id: UUID
    state: DocumentStatus
    job: JobStatusResponse | None
    request_id: StrictStr


class ReadinessResponse(ApiModel):
    status: Literal["ready"]


ReadinessState = Literal[
    "READY_FOR_ANALYSIS", "NEEDS_REVIEW", "EVIDENCE_GAP", "ROLE_GAP",
]


class ReviewGapView(ApiModel):
    code: StrictStr
    message: StrictStr
    role: DocumentRole | None
    field_path: StrictStr | None
    document_id: UUID | None
    processing_run_id: UUID | None
    blocking: StrictBool


class ReviewReadinessResponse(ApiModel):
    case_id: UUID
    status: ReadinessState
    input_revision: StrictInt
    ready_for_analysis: StrictBool
    selected_roles: dict[StrictStr, UUID | None]
    gaps: list[ReviewGapView]
    request_id: StrictStr


class ReviewQueueItemView(ApiModel):
    item_id: StrictStr
    kind: StrictStr
    code: StrictStr
    role: DocumentRole | None
    document_id: UUID | None
    processing_run_id: UUID | None
    field_path: StrictStr | None
    candidate_value: Any
    candidate_state: StrictStr | None
    candidate_evidence_id: UUID | None
    latest_correction_action: StrictStr | None
    reason: StrictStr | None
    message: StrictStr


class ReviewQueueResponse(ApiModel):
    case_id: UUID
    status: ReadinessState
    input_revision: StrictInt
    items: list[ReviewQueueItemView]
    warnings: list[ReviewGapView]
    request_id: StrictStr


class ReviewCorrectionRequest(ApiModel):
    document_id: UUID
    processing_run_id: UUID
    field_path: StrictStr = Field(min_length=1, max_length=160)
    action: Literal["CONFIRM", "CORRECT", "UNRESOLVED"]
    corrected_value: Any | None = None
    evidence_id: UUID | None = None
    verification_method: Literal["EVIDENCE_SPAN", "HUMAN_VISUAL", "UNRESOLVED"]
    page_number: StrictInt | None = Field(default=None, ge=1)
    reason_code: StrictStr | None = Field(default=None, max_length=64)


class ReviewCorrectionView(ApiModel):
    correction_id: UUID
    document_id: UUID
    processing_run_id: UUID
    field_path: StrictStr
    correction_number: StrictInt
    action: Literal["CONFIRM", "CORRECT", "UNRESOLVED"]
    prior_state: StrictStr
    prior_value: Any | None
    corrected_value: Any | None
    evidence_id: UUID | None
    verification_method: StrictStr
    page_number: StrictInt | None
    source_sha256: StrictStr
    input_revision: StrictInt
    reason_code: StrictStr | None
    created_at: datetime


class ReviewCorrectionResponse(ApiModel):
    correction: ReviewCorrectionView
    readiness: ReviewReadinessResponse
    request_id: StrictStr


class DocumentRoleAssignmentRequest(ApiModel):
    role: DocumentRole
    document_id: UUID | None
    confirm_mismatch: StrictBool = False
    reason_code: StrictStr | None = Field(default=None, max_length=64)


class DocumentRoleAssignmentView(ApiModel):
    assignment_id: UUID
    role: DocumentRole
    document_id: UUID | None
    input_revision: StrictInt
    source_sha256: StrictStr | None
    assignment_source: StrictStr
    resolved_mismatch: StrictBool
    reason_code: StrictStr | None
    created_at: datetime


class ReviewJobView(ApiModel):
    job_id: UUID
    document_id: UUID | None
    job_type: StrictStr
    status: JobState
    stage: StrictStr


class DocumentRoleAssignmentResponse(ApiModel):
    assignment: DocumentRoleAssignmentView
    job: ReviewJobView | None
    readiness: ReviewReadinessResponse
    request_id: StrictStr
