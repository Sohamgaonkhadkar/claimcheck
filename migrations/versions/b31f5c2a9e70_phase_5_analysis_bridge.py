"""Persist pinned Phase 5 analysis results.

Revision ID: b31f5c2a9e70
Revises: 9c4b7a2f6e1d
Create Date: 2026-10-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b31f5c2a9e70"
down_revision = "9c4b7a2f6e1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing Phase 2 runs predate case input revisions; keep them readable as legacy rows.
    # The Phase 5 service always sets input_revision for every new run.
    op.add_column("analysis_runs", sa.Column("input_revision", sa.Integer(), nullable=True))
    op.add_column(
        "analysis_runs",
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_analysis_runs_input_revision_nonnegative",
        "analysis_runs",
        "input_revision IS NULL OR input_revision >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_analysis_runs_input_revision_nonnegative", "analysis_runs", type_="check"
    )
    op.drop_column("analysis_runs", "result_payload")
    op.drop_column("analysis_runs", "input_revision")
