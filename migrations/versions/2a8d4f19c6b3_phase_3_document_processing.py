"""phase 3 durable document processing

Revision ID: 2a8d4f19c6b3
Revises: f7e1ae8d09ba
Create Date: 2026-10-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2a8d4f19c6b3"
down_revision = "f7e1ae8d09ba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.create_check_constraint(
        "ck_cases_status", "cases",
        "status IN ('CREATED', 'AWAITING_DOCUMENTS', 'PROCESSING', 'NEEDS_REVIEW', "
        "'ANALYZED', 'EVIDENCE_GAP', 'READY_FOR_ANALYSIS', 'FAILED', 'DELETED')",
    )
    op.drop_constraint("ck_case_documents_state", "case_documents", type_="check")
    op.create_check_constraint(
        "ck_case_documents_state", "case_documents",
        "state IN ('RECEIVED', 'VALIDATING', 'QUEUED', 'PROCESSING', 'NEEDS_REVIEW', "
        "'READY', 'PARTIAL', 'READY_FOR_ANALYSIS', 'EVIDENCE_GAP', 'FAILED', "
        "'DELETE_PENDING', 'DELETED')",
    )
    op.create_unique_constraint(
        "uq_docs_owner_case_document", "case_documents",
        ["owner_id", "case_id", "document_id"],
    )

    op.create_table(
        "document_processing_runs",
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("extraction_version", sa.String(length=80), nullable=False),
        sa.Column("assigned_role", sa.String(length=40), nullable=False),
        sa.Column("detected_role", sa.String(length=40), nullable=True),
        sa.Column("role_mismatch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'NEEDS_REVIEW', 'EVIDENCE_GAP', "
            "'READY_FOR_ANALYSIS', 'FAILED', 'SUPERSEDED')",
            name="ck_processing_runs_status",
        ),
        sa.CheckConstraint("run_number >= 1", name="ck_processing_runs_number_positive"),
        sa.CheckConstraint("page_count >= 1", name="ck_processing_runs_page_count_positive"),
        sa.CheckConstraint("length(source_sha256) = 64", name="ck_processing_runs_sha256"),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id"],
            ["case_documents.owner_id", "case_documents.case_id", "case_documents.document_id"],
            name="fk_processing_runs_document_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "job_id"],
            ["processing_jobs.owner_id", "processing_jobs.case_id", "processing_jobs.job_id"],
            name="fk_processing_runs_job_owner", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("processing_run_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "run_number",
                            name="uq_processing_runs_number"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            name="uq_processing_runs_scope"),
        sa.UniqueConstraint("job_id", name="uq_processing_runs_job"),
    )
    op.create_index(
        "ix_processing_runs_owner_case_document", "document_processing_runs",
        ["owner_id", "case_id", "document_id", "run_number"], unique=False,
    )

    op.create_table(
        "document_page_extractions",
        sa.Column("page_extraction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("layout_state", sa.String(length=16), nullable=False),
        sa.Column("native_state", sa.String(length=16), nullable=False),
        sa.Column("layout_method", sa.String(length=24), nullable=True),
        sa.Column("native_method", sa.String(length=24), nullable=True),
        sa.Column("layout_text_artifact_key", sa.String(length=512), nullable=True),
        sa.Column("layout_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("native_text_artifact_key", sa.String(length=512), nullable=True),
        sa.Column("native_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("width_points", sa.Float(), nullable=True),
        sa.Column("height_points", sa.Float(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("quality_json", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("page_number >= 1", name="ck_page_extractions_page_positive"),
        sa.CheckConstraint("layout_state IN ('READ', 'UNREADABLE', 'MISSING')",
                           name="ck_page_extractions_layout_state"),
        sa.CheckConstraint("native_state IN ('READ', 'UNREADABLE', 'MISSING')",
                           name="ck_page_extractions_native_state"),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id"],
            ["document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id",
             "document_processing_runs.processing_run_id"],
            name="fk_page_extractions_run_scope", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("page_extraction_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            "page_number", name="uq_page_extractions_page"),
    )
    op.create_index(
        "ix_page_extractions_run", "document_page_extractions",
        ["owner_id", "case_id", "processing_run_id", "page_number"], unique=False,
    )

    op.create_table(
        "document_evidence_spans",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("quote_artifact_key", sa.String(length=512), nullable=False),
        sa.Column("quote_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("reader", sa.String(length=24), nullable=False),
        sa.Column("extraction_method", sa.String(length=24), nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("verification_method", sa.String(length=24), nullable=False),
        sa.Column("verify_score", sa.Float(), nullable=False),
        sa.Column("bbox_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("bbox_is_measured", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("injection_flags_json", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "verification_status IN ('VERIFIED', 'AMBIGUOUS', 'REJECTED', 'QUARANTINED')",
            name="ck_evidence_verification_status",
        ),
        sa.CheckConstraint("verify_score >= 0 AND verify_score <= 1",
                           name="ck_evidence_score_range"),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(page_text_sha256) = 64 "
            "AND length(quote_sha256) = 64",
            name="ck_evidence_hash_lengths",
        ),
        sa.CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR "
            "(char_start >= 0 AND char_end >= char_start)",
            name="ck_evidence_char_range",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id", "page_number"],
            ["document_page_extractions.owner_id", "document_page_extractions.case_id",
             "document_page_extractions.document_id",
             "document_page_extractions.processing_run_id",
             "document_page_extractions.page_number"],
            name="fk_evidence_page_scope", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            "evidence_id", name="uq_evidence_scope_id"),
    )
    op.create_index(
        "ix_evidence_run_page", "document_evidence_spans",
        ["owner_id", "case_id", "processing_run_id", "page_number"], unique=False,
    )

    op.create_table(
        "document_processing_fields",
        sa.Column("processing_field_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_path", sa.String(length=160), nullable=False),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "state IN ('VERIFIED', 'PROPOSED', 'NEEDS_REVIEW', 'QUARANTINED', "
            "'MISSING', 'UNREADABLE', 'CONFLICTING')",
            name="ck_processing_fields_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id"],
            ["document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id",
             "document_processing_runs.processing_run_id"],
            name="fk_processing_fields_run_scope", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id", "evidence_id"],
            ["document_evidence_spans.owner_id", "document_evidence_spans.case_id",
             "document_evidence_spans.document_id",
             "document_evidence_spans.processing_run_id",
             "document_evidence_spans.evidence_id"],
            name="fk_processing_fields_evidence_scope", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("processing_field_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            "field_path", name="uq_processing_fields_path"),
    )
    op.create_index(
        "ix_processing_fields_run", "document_processing_fields",
        ["owner_id", "case_id", "processing_run_id", "field_path"], unique=False,
    )

    op.create_table(
        "document_processing_records",
        sa.Column("processing_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_type", sa.String(length=40), nullable=False),
        sa.Column("record_key", sa.String(length=160), nullable=False),
        sa.Column("field_state", sa.String(length=24), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("record_json", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id"],
            ["document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id",
             "document_processing_runs.processing_run_id"],
            name="fk_processing_records_run_scope", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("processing_record_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            "record_type", "record_key", name="uq_processing_records_key"),
    )
    op.create_index(
        "ix_processing_records_run", "document_processing_records",
        ["owner_id", "case_id", "processing_run_id", "record_type"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_processing_records_run", table_name="document_processing_records")
    op.drop_table("document_processing_records")
    op.drop_index("ix_processing_fields_run", table_name="document_processing_fields")
    op.drop_table("document_processing_fields")
    op.drop_index("ix_evidence_run_page", table_name="document_evidence_spans")
    op.drop_table("document_evidence_spans")
    op.drop_index("ix_page_extractions_run", table_name="document_page_extractions")
    op.drop_table("document_page_extractions")
    op.drop_index("ix_processing_runs_owner_case_document", table_name="document_processing_runs")
    op.drop_table("document_processing_runs")
    op.drop_constraint("uq_docs_owner_case_document", "case_documents", type_="unique")
    op.drop_constraint("ck_case_documents_state", "case_documents", type_="check")
    op.create_check_constraint(
        "ck_case_documents_state", "case_documents",
        "state IN ('RECEIVED', 'VALIDATING', 'QUEUED', 'PROCESSING', 'NEEDS_REVIEW', "
        "'READY', 'PARTIAL', 'FAILED', 'DELETED')",
    )
    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.create_check_constraint(
        "ck_cases_status", "cases",
        "status IN ('CREATED', 'AWAITING_DOCUMENTS', 'PROCESSING', 'NEEDS_REVIEW', "
        "'ANALYZED', 'EVIDENCE_GAP', 'FAILED', 'DELETED')",
    )
