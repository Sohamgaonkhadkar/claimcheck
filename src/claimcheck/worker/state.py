"""Case-state projection over explicit roles and Phase 4 trust/readiness gates."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from claimcheck.application.review_readiness import evaluate_case_readiness
from claimcheck.persistence.models import Case, ProcessingJob, utc_now

_ACTIVE_JOB_STATES = {"QUEUED", "RUNNING", "RETRYABLE_FAILURE"}


def recompute_case_processing_status(session: Session, case: Case, *, owner_id: UUID) -> str:
    """Project processing work first, otherwise the deterministic review readiness state.

    This state reducer never calls the calculator or marks a case ANALYZED. In particular,
    upload labels and document-run READY states cannot bypass explicit role selection or
    evidence-backed human review.
    """
    if case.deletion_status != "ACTIVE":
        return case.status
    active_count = session.scalar(select(ProcessingJob.job_id).where(
        ProcessingJob.owner_id == owner_id,
        ProcessingJob.case_id == case.case_id,
        ProcessingJob.status.in_(_ACTIVE_JOB_STATES),
        ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
    ).limit(1))
    if active_count is not None:
        next_status = "PROCESSING"
    else:
        readiness = evaluate_case_readiness(session, owner_id, case)
        next_status = readiness.status.value
    if case.status != next_status:
        case.status = next_status
        case.version += 1
        case.updated_at = utc_now()
    return next_status
