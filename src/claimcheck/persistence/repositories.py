"""Owner-filtered repositories plus an explicit system-worker job claim port."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from .models import (
    AnalysisRun, AuditEvent, Case, CaseDocument, DocumentEvidenceSpan,
    DocumentProcessingField, DocumentProcessingRun, DocumentRoleAssignment,
    ProcessingJob, StoredPageExtraction,
)


class InvalidPageCursor(ValueError):
    pass


def encode_cursor(created_at: datetime, resource_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.astimezone(UTC).isoformat(), "id": str(resource_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 512:
        raise InvalidPageCursor("Invalid page cursor.")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        resource_id = UUID(payload["id"])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at.astimezone(UTC), resource_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPageCursor("Invalid page cursor.") from exc


@dataclass(frozen=True)
class Page:
    items: list[Any]
    next_cursor: str | None


class CaseRepository:
    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def get(self, case_id: UUID, *, include_deleted: bool = False,
            for_update: bool = False) -> Case | None:
        statement = select(Case).where(Case.case_id == case_id, Case.owner_id == self.owner_id)
        if not include_deleted:
            statement = statement.where(Case.deletion_status == "ACTIVE")
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list(self, *, limit: int, cursor: str | None = None,
             status: str | None = None) -> Page:
        statement = select(Case).where(
            Case.owner_id == self.owner_id,
            Case.deletion_status == "ACTIVE",
        )
        if status is not None:
            statement = statement.where(Case.status == status)
        decoded = decode_cursor(cursor)
        if decoded:
            created_at, case_id = decoded
            statement = statement.where(
                or_(Case.created_at < created_at,
                    and_(Case.created_at == created_at, Case.case_id < case_id))
            )
        rows = list(self.session.scalars(
            statement.order_by(Case.created_at.desc(), Case.case_id.desc()).limit(limit + 1)
        ))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last.created_at, last.case_id)
            rows = rows[:limit]
        return Page(items=rows, next_cursor=next_cursor)


class DocumentRepository:
    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def get(self, case_id: UUID, document_id: UUID, *, include_deleted: bool = False,
            for_update: bool = False) -> CaseDocument | None:
        statement = (
            select(CaseDocument)
            .join(Case, Case.case_id == CaseDocument.case_id)
            .where(
                CaseDocument.document_id == document_id,
                CaseDocument.case_id == case_id,
                CaseDocument.owner_id == self.owner_id,
                Case.owner_id == self.owner_id,
            )
        )
        if not include_deleted:
            statement = statement.where(
                Case.deletion_status == "ACTIVE", CaseDocument.state != "DELETED"
            )
        if for_update:
            statement = statement.with_for_update(of=CaseDocument)
        return self.session.scalar(statement)

    def list_for_case(self, case_id: UUID, *, limit: int, cursor: str | None = None,
                      role: str | None = None, state: str | None = None,
                      include_deleted: bool = False) -> Page:
        statement = (
            select(CaseDocument)
            .join(Case, Case.case_id == CaseDocument.case_id)
            .where(
                CaseDocument.owner_id == self.owner_id,
                Case.owner_id == self.owner_id,
                CaseDocument.case_id == case_id,
            )
        )
        if not include_deleted:
            statement = statement.where(
                Case.deletion_status == "ACTIVE", CaseDocument.state != "DELETED"
            )
        if role:
            statement = statement.where(CaseDocument.role == role)
        if state:
            statement = statement.where(CaseDocument.state == state)
        decoded = decode_cursor(cursor)
        if decoded:
            uploaded_at, document_id = decoded
            statement = statement.where(
                or_(CaseDocument.uploaded_at < uploaded_at,
                    and_(CaseDocument.uploaded_at == uploaded_at,
                         CaseDocument.document_id < document_id))
            )
        rows = list(self.session.scalars(
            statement.order_by(CaseDocument.uploaded_at.desc(), CaseDocument.document_id.desc())
            .limit(limit + 1)
        ))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last.uploaded_at, last.document_id)
            rows = rows[:limit]
        return Page(items=rows, next_cursor=next_cursor)

    def all_for_case(self, case_id: UUID, *, include_deleted: bool = False,
                     for_update: bool = False) -> list[CaseDocument]:
        statement = select(CaseDocument).where(
            CaseDocument.owner_id == self.owner_id, CaseDocument.case_id == case_id
        )
        if not include_deleted:
            statement = statement.where(CaseDocument.state != "DELETED")
        if for_update:
            statement = statement.with_for_update()
        return list(self.session.scalars(statement.order_by(CaseDocument.uploaded_at)))

    def active_by_hash(self, case_id: UUID, sha256: str) -> CaseDocument | None:
        return self.session.scalar(
            select(CaseDocument).where(
                CaseDocument.owner_id == self.owner_id,
                CaseDocument.case_id == case_id,
                CaseDocument.sha256 == sha256,
                CaseDocument.state != "DELETED",
            ).with_for_update()
        )

    def by_idempotency_key(self, case_id: UUID, key: str) -> CaseDocument | None:
        return self.session.scalar(
            select(CaseDocument).where(
                CaseDocument.owner_id == self.owner_id,
                CaseDocument.case_id == case_id,
                CaseDocument.idempotency_key == key,
            ).with_for_update()
        )

    def roles_for_cases(self, case_ids: list[UUID]) -> dict[UUID, set[str]]:
        """Return only currently selected, active role sources (never upload labels)."""
        if not case_ids:
            return {}
        events = list(self.session.scalars(
            select(DocumentRoleAssignment)
            .join(Case, Case.case_id == DocumentRoleAssignment.case_id)
            .where(
                DocumentRoleAssignment.owner_id == self.owner_id,
                Case.owner_id == self.owner_id,
                Case.case_id.in_(case_ids),
                Case.deletion_status == "ACTIVE",
            )
            .order_by(DocumentRoleAssignment.case_id, DocumentRoleAssignment.role,
                      DocumentRoleAssignment.input_revision.desc(),
                      DocumentRoleAssignment.assignment_id.desc())
        ))
        latest: dict[tuple[UUID, str], DocumentRoleAssignment] = {}
        for event in events:
            latest.setdefault((event.case_id, event.role), event)
        roles: dict[UUID, set[str]] = {}
        for (case_id, role), event in latest.items():
            if event.document_id is None:
                continue
            active = self.session.scalar(select(CaseDocument.document_id).where(
                CaseDocument.owner_id == self.owner_id,
                CaseDocument.case_id == case_id,
                CaseDocument.document_id == event.document_id,
                CaseDocument.state.not_in(("DELETE_PENDING", "DELETED")),
            ))
            if active is not None:
                roles.setdefault(case_id, set()).add(role)
        return roles


class JobRepository:
    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def get(self, job_id: UUID, *, case_id: UUID | None = None,
            include_deleted: bool = False, for_update: bool = False) -> ProcessingJob | None:
        statement = (
            select(ProcessingJob)
            .join(Case, and_(Case.case_id == ProcessingJob.case_id,
                            Case.owner_id == ProcessingJob.owner_id))
            .where(
                ProcessingJob.job_id == job_id,
                ProcessingJob.owner_id == self.owner_id,
                Case.owner_id == self.owner_id,
            )
        )
        if case_id is not None:
            statement = statement.where(ProcessingJob.case_id == case_id)
        if not include_deleted:
            statement = statement.where(Case.deletion_status == "ACTIVE")
        if for_update:
            statement = statement.with_for_update(of=ProcessingJob)
        return self.session.scalar(statement)

    def for_document(self, case_id: UUID, document_id: UUID) -> ProcessingJob | None:
        return self.session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.owner_id == self.owner_id,
                ProcessingJob.case_id == case_id,
                ProcessingJob.document_id == document_id,
            ).order_by(ProcessingJob.created_at.desc())
        )

    def all_for_case(self, case_id: UUID, *, include_finished: bool = True,
                     for_update: bool = False) -> list[ProcessingJob]:
        statement = select(ProcessingJob).where(
            ProcessingJob.owner_id == self.owner_id, ProcessingJob.case_id == case_id
        )
        if not include_finished:
            statement = statement.where(ProcessingJob.status.in_(
                ("QUEUED", "RUNNING", "RETRYABLE_FAILURE")
            ))
        if for_update:
            statement = statement.with_for_update()
        return list(self.session.scalars(statement.order_by(ProcessingJob.created_at)))

    def deletion_job(self, case_id: UUID) -> ProcessingJob | None:
        return self.session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.owner_id == self.owner_id,
                ProcessingJob.case_id == case_id,
                ProcessingJob.job_type == "CASE_DELETION",
            ).order_by(ProcessingJob.created_at.desc()).limit(1).with_for_update()
        )


class AnalysisRunRepository:
    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def all_for_case(self, case_id: UUID, *, for_update: bool = False) -> list[AnalysisRun]:
        statement = select(AnalysisRun).where(
            AnalysisRun.owner_id == self.owner_id,
            AnalysisRun.case_id == case_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return list(self.session.scalars(statement.order_by(AnalysisRun.created_at)))

    def delete_for_case(self, case_id: UUID) -> int:
        result = self.session.execute(delete(AnalysisRun).where(
            AnalysisRun.owner_id == self.owner_id,
            AnalysisRun.case_id == case_id,
        ))
        return int(result.rowcount or 0)


class ProcessingRepository:
    """Owner-filtered readers for durable document processing state."""

    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def latest_run(self, case_id: UUID, document_id: UUID) -> DocumentProcessingRun | None:
        return self.session.scalar(
            select(DocumentProcessingRun).where(
                DocumentProcessingRun.owner_id == self.owner_id,
                DocumentProcessingRun.case_id == case_id,
                DocumentProcessingRun.document_id == document_id,
            ).order_by(DocumentProcessingRun.run_number.desc()).limit(1)
        )

    def get_run(self, case_id: UUID, document_id: UUID,
                processing_run_id: UUID) -> DocumentProcessingRun | None:
        return self.session.scalar(select(DocumentProcessingRun).where(
            DocumentProcessingRun.owner_id == self.owner_id,
            DocumentProcessingRun.case_id == case_id,
            DocumentProcessingRun.document_id == document_id,
            DocumentProcessingRun.processing_run_id == processing_run_id,
        ))

    def list_runs(self, case_id: UUID, document_id: UUID) -> list[DocumentProcessingRun]:
        return list(self.session.scalars(select(DocumentProcessingRun).where(
            DocumentProcessingRun.owner_id == self.owner_id,
            DocumentProcessingRun.case_id == case_id,
            DocumentProcessingRun.document_id == document_id,
        ).order_by(DocumentProcessingRun.run_number.desc())))

    def fields(self, case_id: UUID, document_id: UUID,
               processing_run_id: UUID) -> list[DocumentProcessingField]:
        return list(self.session.scalars(select(DocumentProcessingField).where(
            DocumentProcessingField.owner_id == self.owner_id,
            DocumentProcessingField.case_id == case_id,
            DocumentProcessingField.document_id == document_id,
            DocumentProcessingField.processing_run_id == processing_run_id,
        ).order_by(DocumentProcessingField.field_path)))

    def evidence(self, case_id: UUID, document_id: UUID,
                 evidence_id: UUID) -> DocumentEvidenceSpan | None:
        return self.session.scalar(select(DocumentEvidenceSpan).where(
            DocumentEvidenceSpan.owner_id == self.owner_id,
            DocumentEvidenceSpan.case_id == case_id,
            DocumentEvidenceSpan.document_id == document_id,
            DocumentEvidenceSpan.evidence_id == evidence_id,
        ))

    def evidence_for_case(self, case_id: UUID,
                          evidence_id: UUID) -> DocumentEvidenceSpan | None:
        return self.session.scalar(select(DocumentEvidenceSpan).where(
            DocumentEvidenceSpan.owner_id == self.owner_id,
            DocumentEvidenceSpan.case_id == case_id,
            DocumentEvidenceSpan.evidence_id == evidence_id,
        ))

    def jobs_for_case(self, case_id: UUID, *, limit: int, cursor: str | None = None,
                      job_type: str | None = None, status: str | None = None) -> Page:
        statement = select(ProcessingJob).join(
            Case, and_(Case.case_id == ProcessingJob.case_id,
                       Case.owner_id == ProcessingJob.owner_id)
        ).where(
            ProcessingJob.owner_id == self.owner_id,
            ProcessingJob.case_id == case_id,
            Case.owner_id == self.owner_id,
            Case.deletion_status == "ACTIVE",
        )
        if job_type:
            statement = statement.where(ProcessingJob.job_type == job_type)
        if status:
            statement = statement.where(ProcessingJob.status == status)
        decoded = decode_cursor(cursor)
        if decoded:
            created_at, job_id = decoded
            statement = statement.where(
                or_(ProcessingJob.created_at < created_at,
                    and_(ProcessingJob.created_at == created_at,
                         ProcessingJob.job_id < job_id))
            )
        rows = list(self.session.scalars(
            statement.order_by(ProcessingJob.created_at.desc(), ProcessingJob.job_id.desc())
            .limit(limit + 1)
        ))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last.created_at, last.job_id)
            rows = rows[:limit]
        return Page(items=rows, next_cursor=next_cursor)

    def artifact_keys_for_document(self, case_id: UUID, document_id: UUID) -> set[str]:
        keys: set[str] = set()
        for row in self.session.scalars(select(StoredPageExtraction).where(
                StoredPageExtraction.owner_id == self.owner_id,
                StoredPageExtraction.case_id == case_id,
                StoredPageExtraction.document_id == document_id)):
            keys.update(key for key in (row.layout_text_artifact_key,
                                        row.native_text_artifact_key) if key)
        keys.update(self.session.scalars(select(DocumentEvidenceSpan.quote_artifact_key).where(
            DocumentEvidenceSpan.owner_id == self.owner_id,
            DocumentEvidenceSpan.case_id == case_id,
            DocumentEvidenceSpan.document_id == document_id,
        )))
        jobs = self.session.scalars(select(ProcessingJob).where(
            ProcessingJob.owner_id == self.owner_id,
            ProcessingJob.case_id == case_id,
            ProcessingJob.document_id == document_id,
            ProcessingJob.job_type == "DOCUMENT_PROCESS",
        ))
        for job in jobs:
            payload = job.payload_json or {}
            for key in payload.get("artifact_keys", []):
                if isinstance(key, str):
                    keys.add(key)
        return keys

    def purge_runs_for_document(self, case_id: UUID, document_id: UUID) -> int:
        result = self.session.execute(delete(DocumentProcessingRun).where(
            DocumentProcessingRun.owner_id == self.owner_id,
            DocumentProcessingRun.case_id == case_id,
            DocumentProcessingRun.document_id == document_id,
        ))
        return int(result.rowcount or 0)

    def purge_runs_for_case(self, case_id: UUID) -> int:
        result = self.session.execute(delete(DocumentProcessingRun).where(
            DocumentProcessingRun.owner_id == self.owner_id,
            DocumentProcessingRun.case_id == case_id,
        ))
        return int(result.rowcount or 0)


class AuditRepository:
    def __init__(self, session: Session, owner_id: UUID) -> None:
        self.session = session
        self.owner_id = owner_id

    def append(self, *, event_type: str, resource_type: str, resource_id: UUID | None,
               request_id: str | None, case_id: UUID | None = None,
               result_code: str = "OK", event_data: dict | None = None,
               actor_type: str = "USER") -> AuditEvent:
        event = AuditEvent(
            owner_id=self.owner_id,
            case_id=case_id,
            actor_type=actor_type,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            result_code=result_code,
            request_id=request_id,
            event_data=event_data or {},
        )
        self.session.add(event)
        return event


@dataclass(frozen=True)
class WorkerIdentity:
    """Trusted internal identity used only for the future worker claim operation."""

    worker_id: str


class JobClaimRepository:
    """Atomic PostgreSQL `SKIP LOCKED` claim primitive; it does not execute a worker."""

    def __init__(self, session: Session, worker: WorkerIdentity) -> None:
        self.session = session
        self.worker = worker

    def claim_next(self, *, now: datetime | None = None,
                   lease_for: timedelta = timedelta(minutes=2)) -> ProcessingJob | None:
        now = now or datetime.now(UTC)
        statement = (
            select(ProcessingJob)
            .join(Case, and_(Case.case_id == ProcessingJob.case_id,
                            Case.owner_id == ProcessingJob.owner_id))
            .where(
                Case.deletion_status == "ACTIVE",
                ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
                or_(
                    ProcessingJob.status.in_(("QUEUED", "RETRYABLE_FAILURE")),
                    and_(ProcessingJob.status == "RUNNING",
                         ProcessingJob.lease_expires_at.is_not(None),
                         ProcessingJob.lease_expires_at <= now),
                ),
                ProcessingJob.available_at <= now,
                ProcessingJob.attempt_count < ProcessingJob.max_attempts,
            )
            .order_by(ProcessingJob.available_at, ProcessingJob.created_at, ProcessingJob.job_id)
            .limit(1)
            .with_for_update(skip_locked=True, of=ProcessingJob)
        )
        job = self.session.scalar(statement)
        if job is None:
            return None
        job.status = "RUNNING"
        job.attempt_count += 1
        job.started_at = now
        job.finished_at = None
        job.error_code = None
        job.lease_owner = self.worker.worker_id
        job.lease_expires_at = now + lease_for
        job.updated_at = now
        return job

    def get_owned_running(self, job_id: UUID, *, now: datetime | None = None,
                          lock: bool = True) -> ProcessingJob | None:
        now = now or datetime.now(UTC)
        statement = select(ProcessingJob).where(
            ProcessingJob.job_id == job_id,
            ProcessingJob.status == "RUNNING",
            ProcessingJob.lease_owner == self.worker.worker_id,
            ProcessingJob.lease_expires_at.is_not(None),
            ProcessingJob.lease_expires_at > now,
            ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def renew(self, job_id: UUID, *, now: datetime | None = None,
              lease_for: timedelta = timedelta(minutes=2)) -> bool:
        now = now or datetime.now(UTC)
        job = self.get_owned_running(job_id, now=now)
        if job is None:
            return False
        job.lease_expires_at = now + lease_for
        job.updated_at = now
        return True

    def complete(self, job_id: UUID, *, now: datetime | None = None) -> ProcessingJob | None:
        now = now or datetime.now(UTC)
        job = self.get_owned_running(job_id, now=now)
        if job is None:
            return None
        job.status = "SUCCEEDED"
        job.stage = "COMPLETE"
        job.finished_at = now
        job.error_code = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        return job

    def fail(self, job_id: UUID, *, error_code: str, retryable: bool,
             now: datetime | None = None,
             retry_delay: timedelta = timedelta(seconds=30)) -> ProcessingJob | None:
        now = now or datetime.now(UTC)
        job = self.get_owned_running(job_id, now=now)
        if job is None:
            return None
        will_retry = retryable and job.attempt_count < job.max_attempts
        job.status = "RETRYABLE_FAILURE" if will_retry else "FAILED"
        job.error_code = error_code
        job.finished_at = now
        job.available_at = now + retry_delay if will_retry else now
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        return job

    def expire_exhausted(self, *, now: datetime | None = None) -> list[UUID]:
        """Return crashed last-attempt IDs for lock-ordered worker recovery.

        The caller must lock case/document rows before locking and finalizing each job; this
        read-only selector avoids reversing the application deletion lock order.
        """
        now = now or datetime.now(UTC)
        statement = select(ProcessingJob.job_id).where(
            ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
            ProcessingJob.status == "RUNNING",
            ProcessingJob.lease_expires_at.is_not(None),
            ProcessingJob.lease_expires_at <= now,
            ProcessingJob.attempt_count >= ProcessingJob.max_attempts,
        ).order_by(ProcessingJob.available_at, ProcessingJob.created_at)
        return list(self.session.scalars(statement))
