"""Relational product-state records. No claim verdict or money logic lives here."""
from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Float,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CaseStatus(StrEnum):
    CREATED = "CREATED"
    AWAITING_DOCUMENTS = "AWAITING_DOCUMENTS"
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ANALYZED = "ANALYZED"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    READY_FOR_ANALYSIS = "READY_FOR_ANALYSIS"
    ROLE_GAP = "ROLE_GAP"
    FAILED = "FAILED"
    DELETED = "DELETED"


class DeletionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETE_PENDING = "DELETE_PENDING"
    DELETED = "DELETED"


class DocumentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"
    PARTIAL = "PARTIAL"
    READY_FOR_ANALYSIS = "READY_FOR_ANALYSIS"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    FAILED = "FAILED"
    DELETE_PENDING = "DELETE_PENDING"
    DELETED = "DELETED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


CASE_STATUSES = tuple(status.value for status in CaseStatus)
DELETION_STATUSES = tuple(status.value for status in DeletionStatus)
DOCUMENT_STATUSES = tuple(status.value for status in DocumentStatus)
JOB_STATUSES = tuple(status.value for status in JobStatus)
DOCUMENT_ROLES = ("policy_wording", "policy_schedule", "bill", "settlement")
JOB_TYPES = ("DOCUMENT_PROCESS", "DOCUMENT_DELETE", "CASE_DELETION")
PROCESSING_RUN_STATUSES = ("QUEUED", "RUNNING", "NEEDS_REVIEW", "EVIDENCE_GAP",
                           "READY_FOR_ANALYSIS", "FAILED", "SUPERSEDED")
PROCESSING_FIELD_STATES = ("VERIFIED", "PROPOSED", "NEEDS_REVIEW", "QUARANTINED",
                           "MISSING", "UNREADABLE", "CONFLICTING")
EVIDENCE_STATES = ("VERIFIED", "AMBIGUOUS", "REJECTED", "QUARANTINED")
PAGE_STATES = ("READ", "UNREADABLE", "MISSING")
AUDIT_EVENT_TYPES = (
    "CASE_CREATED",
    "CASE_UPDATED",
    "CASE_DELETED",
    "DOCUMENT_UPLOADED",
    "DOCUMENT_REJECTED",
    "DOCUMENT_DELETED",
    "JOB_CREATED",
    "JOB_COMPLETED",
    "JOB_FAILED",
    "JOB_CANCELLED",
    "CASE_INPUT_REVISION_CREATED",
    "DOCUMENT_ROLE_ASSIGNED",
    "REVIEW_CORRECTION_APPENDED",
)


class Owner(Base):
    """Local identity projection; real OIDC issuer/subject arrives before external pilots."""

    __tablename__ = "owners"
    __table_args__ = (
        UniqueConstraint("identity_provider", "subject", name="uq_owners_provider_subject"),
    )

    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    identity_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("case_id", "owner_id", name="uq_cases_case_owner"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in CASE_STATUSES) + ")",
            name="ck_cases_status",
        ),
        CheckConstraint(
            "deletion_status IN (" + ", ".join(f"'{value}'" for value in DELETION_STATUSES) + ")",
            name="ck_cases_deletion_status",
        ),
        CheckConstraint("version >= 1", name="ck_cases_version_positive"),
        CheckConstraint("input_revision >= 0", name="ck_cases_input_revision_nonnegative"),
        Index("ix_cases_owner_created", "owner_id", "created_at", "case_id"),
    )

    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("owners.owner_id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CaseStatus.CREATED.value, server_default=text("'CREATED'")
    )
    deletion_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DeletionStatus.ACTIVE.value,
        server_default=text("'ACTIVE'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    # Review/role/source revisions are independent of generic metadata/status versioning.
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                server_default=text("0"))
    latest_analysis_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseDocument(Base):
    __tablename__ = "case_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_case_documents_case_owner", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "idempotency_key", name="uq_docs_idempotency"),
        UniqueConstraint("owner_id", "case_id", "document_id", name="uq_docs_owner_case_document"),
        CheckConstraint(
            "role IN (" + ", ".join(f"'{value}'" for value in DOCUMENT_ROLES) + ")",
            name="ck_case_documents_role",
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in DOCUMENT_STATUSES) + ")",
            name="ck_case_documents_state",
        ),
        CheckConstraint("byte_size > 0 OR state = 'DELETED'", name="ck_case_documents_byte_size_positive"),
        CheckConstraint("page_count > 0 OR state = 'DELETED'", name="ck_case_documents_page_count_positive"),
        CheckConstraint("length(sha256) = 64", name="ck_case_documents_sha256_length"),
        Index("ix_documents_owner_case_uploaded", "owner_id", "case_id", "uploaded_at", "document_id"),
        Index("ix_documents_owner_case_hash", "owner_id", "case_id", "sha256"),
    )

    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DocumentStatus.QUEUED.value,
        server_default=text("'QUEUED'")
    )
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_jobs_case_owner", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "job_id", name="uq_jobs_owner_case_job"),
        UniqueConstraint("owner_id", "case_id", "idempotency_key", name="uq_jobs_idempotency"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in JOB_STATUSES) + ")",
            name="ck_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempts_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        Index("ix_jobs_queue", "status", "available_at", "created_at"),
        Index("ix_jobs_owner_case_created", "owner_id", "case_id", "created_at"),
    )

    job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("case_documents.document_id", ondelete="SET NULL"), nullable=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.QUEUED.value, server_default=text("'QUEUED'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=func.now()
    )


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_analysis_runs_case_owner", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "analysis_run_id", name="uq_runs_owner_case_run"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_analysis_runs_status",
        ),
        CheckConstraint("run_number >= 1", name="ck_analysis_runs_number_positive"),
        CheckConstraint(
            "input_revision IS NULL OR input_revision >= 0",
            name="ck_analysis_runs_input_revision_nonnegative",
        ),
        UniqueConstraint("owner_id", "case_id", "run_number", name="uq_analysis_runs_number"),
        Index("ix_runs_owner_case_created", "owner_id", "case_id", "created_at"),
    )

    analysis_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    input_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rulepack_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    corpus_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """Append-only event row; deliberately has no FK to cases so tombstones survive deletion."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN (" + ", ".join(f"'{value}'" for value in AUDIT_EVENT_TYPES) + ")",
            name="ck_audit_events_type",
        ),
        Index("ix_audit_owner_created", "owner_id", "occurred_at", "event_id"),
        Index("ix_audit_case_created", "case_id", "occurred_at"),
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("owners.owner_id", ondelete="RESTRICT"), nullable=False
    )
    case_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    result_code: Mapped[str] = mapped_column(String(64), nullable=False, default="OK")
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_data: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class DocumentProcessingRun(Base):
    """Immutable result/version for one DOCUMENT_PROCESS job."""

    __tablename__ = "document_processing_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id"),
            ("case_documents.owner_id", "case_documents.case_id", "case_documents.document_id"),
            name="fk_processing_runs_document_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "job_id"),
            ("processing_jobs.owner_id", "processing_jobs.case_id", "processing_jobs.job_id"),
            name="fk_processing_runs_job_owner", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "run_number",
                         name="uq_processing_runs_number"),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         name="uq_processing_runs_scope"),
        UniqueConstraint("job_id", name="uq_processing_runs_job"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in PROCESSING_RUN_STATUSES) + ")",
            name="ck_processing_runs_status",
        ),
        CheckConstraint("run_number >= 1", name="ck_processing_runs_number_positive"),
        Index("ix_processing_runs_owner_case_document", "owner_id", "case_id", "document_id",
              "run_number"),
    )

    processing_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    extraction_version: Mapped[str] = mapped_column(String(80), nullable=False)
    assigned_role: Mapped[str] = mapped_column(String(40), nullable=False)
    detected_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    role_mismatch: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StoredPageExtraction(Base):
    """One page's reader outputs; text lives in private content-addressed storage."""

    __tablename__ = "document_page_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id"),
            ("document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id", "document_processing_runs.processing_run_id"),
            name="fk_page_extractions_run_scope", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         "page_number", name="uq_page_extractions_page"),
        CheckConstraint("page_number >= 1", name="ck_page_extractions_page_positive"),
        CheckConstraint(
            "layout_state IN (" + ", ".join(f"'{value}'" for value in PAGE_STATES) + ")",
            name="ck_page_extractions_layout_state",
        ),
        CheckConstraint(
            "native_state IN (" + ", ".join(f"'{value}'" for value in PAGE_STATES) + ")",
            name="ck_page_extractions_native_state",
        ),
        Index("ix_page_extractions_run", "owner_id", "case_id", "processing_run_id",
              "page_number"),
    )

    page_extraction_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    layout_state: Mapped[str] = mapped_column(String(16), nullable=False)
    native_state: Mapped[str] = mapped_column(String(16), nullable=False)
    layout_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    native_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    layout_text_artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    layout_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    native_text_artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    native_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class DocumentEvidenceSpan(Base):
    """A verified/untrusted span with hash-bound text held outside the database."""

    __tablename__ = "document_evidence_spans"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id", "page_number"),
            ("document_page_extractions.owner_id", "document_page_extractions.case_id",
             "document_page_extractions.document_id",
             "document_page_extractions.processing_run_id",
             "document_page_extractions.page_number"),
            name="fk_evidence_page_scope", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         "evidence_id", name="uq_evidence_scope_id"),
        CheckConstraint(
            "verification_status IN (" + ", ".join(f"'{value}'" for value in EVIDENCE_STATES) + ")",
            name="ck_evidence_verification_status",
        ),
        CheckConstraint("verify_score >= 0 AND verify_score <= 1", name="ck_evidence_score_range"),
        Index("ix_evidence_run_page", "owner_id", "case_id", "processing_run_id", "page_number"),
    )

    evidence_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_artifact_key: Mapped[str] = mapped_column(String(512), nullable=False)
    quote_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reader: Mapped[str] = mapped_column(String(24), nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_method: Mapped[str] = mapped_column(String(24), nullable=False)
    verify_score: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    bbox_is_measured: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    injection_flags_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class DocumentProcessingField(Base):
    """Typed path/value state. Missing, unreadable and conflicting stay distinct."""

    __tablename__ = "document_processing_fields"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id"),
            ("document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id", "document_processing_runs.processing_run_id"),
            name="fk_processing_fields_run_scope", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id", "evidence_id"),
            ("document_evidence_spans.owner_id", "document_evidence_spans.case_id",
             "document_evidence_spans.document_id", "document_evidence_spans.processing_run_id",
             "document_evidence_spans.evidence_id"),
            name="fk_processing_fields_evidence_scope", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         "field_path", name="uq_processing_fields_path"),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{value}'" for value in PROCESSING_FIELD_STATES) + ")",
            name="ck_processing_fields_state",
        ),
        Index("ix_processing_fields_run", "owner_id", "case_id", "processing_run_id", "field_path"),
    )

    processing_field_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    field_path: Mapped[str] = mapped_column(String(160), nullable=False)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class DocumentProcessingRecord(Base):
    """A safe serialization of an existing typed parser record for audit/replay."""

    __tablename__ = "document_processing_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id"),
            ("document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id", "document_processing_runs.processing_run_id"),
            name="fk_processing_records_run_scope", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         "record_type", "record_key", name="uq_processing_records_key"),
        Index("ix_processing_records_run", "owner_id", "case_id", "processing_run_id",
              "record_type"),
    )

    processing_record_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    record_type: Mapped[str] = mapped_column(String(40), nullable=False)
    record_key: Mapped[str] = mapped_column(String(160), nullable=False)
    field_state: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    record_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class CaseInputRevision(Base):
    """Append-only revision marker for analysis-relevant case inputs."""

    __tablename__ = "case_input_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_input_revisions_case_owner", ondelete="CASCADE",
        ),
        UniqueConstraint("owner_id", "case_id", "revision_number",
                         name="uq_input_revisions_number"),
        CheckConstraint("revision_number >= 1", name="ck_input_revision_positive"),
        CheckConstraint(
            "reason_code IN ('DOCUMENT_UPLOADED', 'DOCUMENT_ROLE_ASSIGNED', "
            "'REVIEW_CORRECTION_APPENDED', 'DOCUMENT_REPROCESS_QUEUED', "
            "'DOCUMENT_DELETE_REQUESTED', 'CLAIM_DATE_UPDATED')",
            name="ck_input_revision_reason",
        ),
        CheckConstraint("actor_type IN ('OWNER', 'SYSTEM', 'WORKER')",
                        name="ck_input_revision_actor"),
        Index("ix_input_revisions_case", "owner_id", "case_id", "revision_number"),
    )

    revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                              default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="OWNER")
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class DocumentRoleAssignment(Base):
    """A versioned, explicit case-level selection of one source document for one role."""

    __tablename__ = "document_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_role_assignments_case_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "input_revision"),
            ("case_input_revisions.owner_id", "case_input_revisions.case_id",
             "case_input_revisions.revision_number"),
            name="fk_role_assignments_input_revision", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id"),
            ("case_documents.owner_id", "case_documents.case_id",
             "case_documents.document_id"),
            name="fk_role_assignments_document_owner", ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "case_id", "role", "input_revision",
                         name="uq_role_assignment_revision"),
        CheckConstraint(
            "role IN ('policy_wording', 'policy_schedule', 'bill', 'settlement')",
            name="ck_role_assignment_role",
        ),
        CheckConstraint(
            "assignment_source IN ('UPLOAD_ROLE_SELECTION', 'REVIEWER_SELECTION', "
            "'MIGRATION_SINGLE_CANDIDATE', 'SOURCE_DELETION_REQUESTED')",
            name="ck_role_assignment_source",
        ),
        CheckConstraint(
            "(document_id IS NULL AND source_sha256 IS NULL) OR "
            "(document_id IS NOT NULL AND length(source_sha256) = 64)",
            name="ck_role_assignment_source_hash",
        ),
        CheckConstraint("input_revision >= 1", name="ck_role_assignment_revision_positive"),
        Index("ix_role_assignments_effective", "owner_id", "case_id", "role",
              "input_revision"),
        Index("ix_role_assignments_document", "owner_id", "case_id", "document_id",
              "input_revision"),
    )

    assignment_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                                default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assignment_source: Mapped[str] = mapped_column(String(40), nullable=False)
    resolved_mismatch: Mapped[bool] = mapped_column(nullable=False, default=False,
                                                    server_default=text("false"))
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class ReviewCorrection(Base):
    """Append-only decision against one immutable processing-run candidate."""

    __tablename__ = "review_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ("case_id", "owner_id"), ("cases.case_id", "cases.owner_id"),
            name="fk_review_corrections_case_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id"),
            ("case_documents.owner_id", "case_documents.case_id",
             "case_documents.document_id"),
            name="fk_review_corrections_document_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id"),
            ("document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id",
             "document_processing_runs.processing_run_id"),
            name="fk_review_corrections_run_scope", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "document_id", "processing_run_id", "evidence_id"),
            ("document_evidence_spans.owner_id", "document_evidence_spans.case_id",
             "document_evidence_spans.document_id",
             "document_evidence_spans.processing_run_id",
             "document_evidence_spans.evidence_id"),
            name="fk_review_corrections_evidence_scope", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("owner_id", "case_id", "input_revision"),
            ("case_input_revisions.owner_id", "case_input_revisions.case_id",
             "case_input_revisions.revision_number"),
            name="fk_review_corrections_input_revision", ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                         "field_path", "correction_number", name="uq_review_correction_version"),
        CheckConstraint("correction_number >= 1", name="ck_review_correction_number_positive"),
        CheckConstraint("input_revision >= 1", name="ck_review_correction_input_revision_positive"),
        CheckConstraint("length(source_sha256) = 64", name="ck_review_correction_sha256"),
        CheckConstraint(
            "action IN ('CONFIRM', 'CORRECT', 'UNRESOLVED')",
            name="ck_review_correction_action",
        ),
        CheckConstraint(
            "prior_state IN ('VERIFIED', 'PROPOSED', 'NEEDS_REVIEW', 'QUARANTINED', "
            "'MISSING', 'UNREADABLE', 'CONFLICTING')",
            name="ck_review_correction_prior_state",
        ),
        CheckConstraint(
            "(action = 'UNRESOLVED' AND verification_method = 'UNRESOLVED' "
            "AND evidence_id IS NULL AND page_number IS NULL "
            "AND corrected_value_json IS NULL) OR "
            "(action = 'CONFIRM' AND corrected_value_json IS NULL AND "
            "((verification_method = 'EVIDENCE_SPAN' AND evidence_id IS NOT NULL "
            "AND page_number IS NULL) OR "
            "(verification_method = 'HUMAN_VISUAL' AND evidence_id IS NULL "
            "AND page_number >= 1))) OR "
            "(action = 'CORRECT' AND corrected_value_json IS NOT NULL AND "
            "((verification_method = 'EVIDENCE_SPAN' AND evidence_id IS NOT NULL "
            "AND page_number IS NULL) OR "
            "(verification_method = 'HUMAN_VISUAL' AND evidence_id IS NULL "
            "AND page_number >= 1)))",
            name="ck_review_correction_evidence_mode",
        ),
        Index("ix_review_corrections_latest", "owner_id", "case_id", "document_id",
              "processing_run_id", "field_path", "correction_number"),
    )

    correction_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                                default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    field_path: Mapped[str] = mapped_column(String(160), nullable=False)
    correction_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    prior_state: Mapped[str] = mapped_column(String(24), nullable=False, default="MISSING")
    prior_value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    candidate_field_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    candidate_record_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    corrected_value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    verification_method: Mapped[str] = mapped_column(String(24), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
