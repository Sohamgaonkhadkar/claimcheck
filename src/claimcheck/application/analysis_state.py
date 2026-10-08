"""Case status projection for analysis runs against persisted input revisions."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from claimcheck.persistence.models import AnalysisRun, Case


def project_case_analysis_status(session: Session, owner_id: UUID, case: Case,
                                 readiness_status: str) -> str:
    """Keep terminal analysis state only while it still describes the current input revision."""
    active = session.scalar(select(AnalysisRun).where(
        AnalysisRun.owner_id == owner_id,
        AnalysisRun.case_id == case.case_id,
        AnalysisRun.status == "RUNNING",
        AnalysisRun.input_revision == case.input_revision,
    ).order_by(AnalysisRun.run_number.desc()).limit(1))
    if active is not None:
        return "PROCESSING"

    if case.latest_analysis_run_id is not None:
        latest = session.scalar(select(AnalysisRun).where(
            AnalysisRun.owner_id == owner_id,
            AnalysisRun.case_id == case.case_id,
            AnalysisRun.analysis_run_id == case.latest_analysis_run_id,
        ))
        if latest is not None and latest.input_revision == case.input_revision:
            if latest.status == "SUCCEEDED":
                return "ANALYZED"
            if latest.status == "FAILED":
                return "FAILED"
    return readiness_status
