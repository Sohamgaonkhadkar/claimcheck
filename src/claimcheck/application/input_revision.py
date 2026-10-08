"""Append an analysis-relevant case input revision independently of Case.version."""
from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from claimcheck.persistence.models import Case, CaseInputRevision, utc_now


def append_input_revision(session: Session, case: Case, *, reason_code: str,
                          actor_type: str, resource_type: str | None = None,
                          resource_id: UUID | None = None,
                          request_id: str | None = None) -> int:
    """Advance the dedicated input revision and persist its append-only event row."""
    revision = case.input_revision + 1
    case.input_revision = revision
    case.updated_at = utc_now()
    session.add(CaseInputRevision(
        revision_id=uuid4(),
        owner_id=case.owner_id,
        case_id=case.case_id,
        revision_number=revision,
        reason_code=reason_code,
        actor_type=actor_type,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
    ))
    session.flush()
    return revision
