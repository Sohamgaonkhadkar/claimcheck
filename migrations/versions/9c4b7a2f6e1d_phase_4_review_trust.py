"""Phase 4 append-only review, explicit roles and case input revisions

Revision ID: 9c4b7a2f6e1d
Revises: 2a8d4f19c6b3
Create Date: 2026-10-06
"""
from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "9c4b7a2f6e1d"
down_revision = "2a8d4f19c6b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("input_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        "ck_cases_input_revision_nonnegative", "cases", "input_revision >= 0"
    )
    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.create_check_constraint(
        "ck_cases_status", "cases",
        "status IN ('CREATED', 'AWAITING_DOCUMENTS', 'PROCESSING', 'NEEDS_REVIEW', "
        "'ANALYZED', 'EVIDENCE_GAP', 'READY_FOR_ANALYSIS', 'ROLE_GAP', 'FAILED', 'DELETED')",
    )
    op.drop_constraint("ck_audit_events_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_events_type", "audit_events",
        "event_type IN ('CASE_CREATED', 'CASE_UPDATED', 'CASE_DELETED', 'DOCUMENT_UPLOADED', "
        "'DOCUMENT_REJECTED', 'DOCUMENT_DELETED', 'JOB_CREATED', 'JOB_COMPLETED', "
        "'JOB_FAILED', 'JOB_CANCELLED', 'CASE_INPUT_REVISION_CREATED', "
        "'DOCUMENT_ROLE_ASSIGNED', 'REVIEW_CORRECTION_APPENDED')",
    )

    op.create_table(
        "case_input_revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=48), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("revision_number >= 1", name="ck_input_revision_positive"),
        sa.CheckConstraint(
            "reason_code IN ('DOCUMENT_UPLOADED', 'DOCUMENT_ROLE_ASSIGNED', "
            "'REVIEW_CORRECTION_APPENDED', 'DOCUMENT_REPROCESS_QUEUED', "
            "'DOCUMENT_DELETE_REQUESTED', 'CLAIM_DATE_UPDATED')",
            name="ck_input_revision_reason",
        ),
        sa.CheckConstraint("actor_type IN ('OWNER', 'SYSTEM', 'WORKER')",
                           name="ck_input_revision_actor"),
        sa.ForeignKeyConstraint(
            ["case_id", "owner_id"], ["cases.case_id", "cases.owner_id"],
            name="fk_input_revisions_case_owner", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("owner_id", "case_id", "revision_number",
                            name="uq_input_revisions_number"),
    )
    op.create_index(
        "ix_input_revisions_case", "case_input_revisions",
        ["owner_id", "case_id", "revision_number"], unique=False,
    )

    op.create_table(
        "document_role_assignments",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("assignment_source", sa.String(length=40), nullable=False),
        sa.Column("resolved_mismatch", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "role IN ('policy_wording', 'policy_schedule', 'bill', 'settlement')",
            name="ck_role_assignment_role",
        ),
        sa.CheckConstraint(
            "assignment_source IN ('UPLOAD_ROLE_SELECTION', 'REVIEWER_SELECTION', "
            "'MIGRATION_SINGLE_CANDIDATE', 'SOURCE_DELETION_REQUESTED')",
            name="ck_role_assignment_source",
        ),
        sa.CheckConstraint(
            "(document_id IS NULL AND source_sha256 IS NULL) OR "
            "(document_id IS NOT NULL AND length(source_sha256) = 64)",
            name="ck_role_assignment_source_hash",
        ),
        sa.CheckConstraint("input_revision >= 1", name="ck_role_assignment_revision_positive"),
        sa.ForeignKeyConstraint(
            ["case_id", "owner_id"], ["cases.case_id", "cases.owner_id"],
            name="fk_role_assignments_case_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "input_revision"],
            ["case_input_revisions.owner_id", "case_input_revisions.case_id",
             "case_input_revisions.revision_number"],
            name="fk_role_assignments_input_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id"],
            ["case_documents.owner_id", "case_documents.case_id",
             "case_documents.document_id"],
            name="fk_role_assignments_document_owner", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("owner_id", "case_id", "role", "input_revision",
                            name="uq_role_assignment_revision"),
    )
    op.create_index(
        "ix_role_assignments_effective", "document_role_assignments",
        ["owner_id", "case_id", "role", "input_revision"], unique=False,
    )
    op.create_index(
        "ix_role_assignments_document", "document_role_assignments",
        ["owner_id", "case_id", "document_id", "input_revision"], unique=False,
    )

    op.create_table(
        "review_corrections",
        sa.Column("correction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processing_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_path", sa.String(length=160), nullable=False),
        sa.Column("correction_number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("prior_state", sa.String(length=24), nullable=False),
        sa.Column("prior_value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("candidate_field_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("corrected_value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verification_method", sa.String(length=24), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("correction_number >= 1",
                           name="ck_review_correction_number_positive"),
        sa.CheckConstraint("input_revision >= 1",
                           name="ck_review_correction_input_revision_positive"),
        sa.CheckConstraint("length(source_sha256) = 64",
                           name="ck_review_correction_sha256"),
        sa.CheckConstraint(
            "action IN ('CONFIRM', 'CORRECT', 'UNRESOLVED')",
            name="ck_review_correction_action",
        ),
        sa.CheckConstraint(
            "prior_state IN ('VERIFIED', 'PROPOSED', 'NEEDS_REVIEW', 'QUARANTINED', "
            "'MISSING', 'UNREADABLE', 'CONFLICTING')",
            name="ck_review_correction_prior_state",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["case_id", "owner_id"], ["cases.case_id", "cases.owner_id"],
            name="fk_review_corrections_case_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id"],
            ["case_documents.owner_id", "case_documents.case_id",
             "case_documents.document_id"],
            name="fk_review_corrections_document_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id"],
            ["document_processing_runs.owner_id", "document_processing_runs.case_id",
             "document_processing_runs.document_id",
             "document_processing_runs.processing_run_id"],
            name="fk_review_corrections_run_scope", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "document_id", "processing_run_id", "evidence_id"],
            ["document_evidence_spans.owner_id", "document_evidence_spans.case_id",
             "document_evidence_spans.document_id",
             "document_evidence_spans.processing_run_id",
             "document_evidence_spans.evidence_id"],
            name="fk_review_corrections_evidence_scope", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "case_id", "input_revision"],
            ["case_input_revisions.owner_id", "case_input_revisions.case_id",
             "case_input_revisions.revision_number"],
            name="fk_review_corrections_input_revision", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("correction_id"),
        sa.UniqueConstraint("owner_id", "case_id", "document_id", "processing_run_id",
                            "field_path", "correction_number",
                            name="uq_review_correction_version"),
    )
    op.create_index(
        "ix_review_corrections_latest", "review_corrections",
        ["owner_id", "case_id", "document_id", "processing_run_id", "field_path",
         "correction_number"], unique=False,
    )

    # Backfill only roles with a single active document. Duplicate candidates stay unselected;
    # no migration-time "latest file wins" choice is made.
    connection = op.get_bind()
    singleton_rows = connection.execute(sa.text("""
        SELECT d.owner_id, d.case_id, d.role, d.document_id, d.sha256
        FROM case_documents AS d
        JOIN (
            SELECT owner_id, case_id, role, count(*) AS candidate_count
            FROM case_documents
            WHERE state NOT IN ('DELETE_PENDING', 'DELETED')
            GROUP BY owner_id, case_id, role
        ) AS candidates
          ON candidates.owner_id = d.owner_id
         AND candidates.case_id = d.case_id
         AND candidates.role = d.role
        WHERE candidates.candidate_count = 1
          AND d.state NOT IN ('DELETE_PENDING', 'DELETED')
    """)).mappings().all()
    by_case: dict[tuple[object, object], list[dict]] = {}
    for row in singleton_rows:
        key = (row["owner_id"], row["case_id"])
        by_case.setdefault(key, []).append(dict(row))
    now = sa.func.now()
    for (owner_id, case_id), rows in by_case.items():
        connection.execute(sa.text("""
            INSERT INTO case_input_revisions
                (revision_id, owner_id, case_id, revision_number, reason_code, actor_type,
                 resource_type, resource_id)
            VALUES (:revision_id, :owner_id, :case_id, 1, 'DOCUMENT_ROLE_ASSIGNED',
                    'SYSTEM', 'CASE', :case_id)
        """), {"revision_id": uuid4(), "owner_id": owner_id, "case_id": case_id})
        for row in rows:
            connection.execute(sa.text("""
                INSERT INTO document_role_assignments
                    (assignment_id, owner_id, case_id, role, document_id, input_revision,
                     source_sha256, assignment_source, resolved_mismatch)
                VALUES (:assignment_id, :owner_id, :case_id, :role, :document_id, 1,
                        :source_sha256, 'MIGRATION_SINGLE_CANDIDATE', false)
            """), {
                "assignment_id": uuid4(), "owner_id": owner_id, "case_id": case_id,
                "role": row["role"], "document_id": row["document_id"],
                "source_sha256": row["sha256"],
            })
        connection.execute(sa.text(
            "UPDATE cases SET input_revision = 1 WHERE owner_id = :owner_id AND case_id = :case_id"
        ), {"owner_id": owner_id, "case_id": case_id})

    op.execute("""
        CREATE FUNCTION claimcheck_reject_phase4_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'review_corrections' AND TG_OP = 'DELETE'
               AND current_setting('claimcheck.purge_document_artifacts', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$;
    """)
    for table in ("case_input_revisions", "document_role_assignments", "review_corrections"):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION claimcheck_reject_phase4_event_mutation();
        """)


def downgrade() -> None:
    for table in ("review_corrections", "document_role_assignments", "case_input_revisions"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS claimcheck_reject_phase4_event_mutation()")
    op.drop_index("ix_review_corrections_latest", table_name="review_corrections")
    op.drop_table("review_corrections")
    op.drop_index("ix_role_assignments_document", table_name="document_role_assignments")
    op.drop_index("ix_role_assignments_effective", table_name="document_role_assignments")
    op.drop_table("document_role_assignments")
    op.drop_index("ix_input_revisions_case", table_name="case_input_revisions")
    op.drop_table("case_input_revisions")

    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.execute("UPDATE cases SET status = 'AWAITING_DOCUMENTS' WHERE status = 'ROLE_GAP'")
    op.create_check_constraint(
        "ck_cases_status", "cases",
        "status IN ('CREATED', 'AWAITING_DOCUMENTS', 'PROCESSING', 'NEEDS_REVIEW', "
        "'ANALYZED', 'EVIDENCE_GAP', 'READY_FOR_ANALYSIS', 'FAILED', 'DELETED')",
    )
    op.drop_constraint("ck_cases_input_revision_nonnegative", "cases", type_="check")
    op.drop_column("cases", "input_revision")
    op.drop_constraint("ck_audit_events_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_events_type", "audit_events",
        "event_type IN ('CASE_CREATED', 'CASE_UPDATED', 'CASE_DELETED', 'DOCUMENT_UPLOADED', "
        "'DOCUMENT_REJECTED', 'DOCUMENT_DELETED', 'JOB_CREATED', 'JOB_COMPLETED', "
        "'JOB_FAILED', 'JOB_CANCELLED')",
    )
