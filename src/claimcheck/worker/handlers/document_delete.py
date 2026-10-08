"""DOCUMENT_DELETE handler; removes source and all run-scoped extracted artifacts idempotently."""
from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from claimcheck.persistence.models import (
    Case,
    ProcessingJob,
    utc_now,
)
from claimcheck.persistence.repositories import (
    AuditRepository,
    CaseRepository,
    DocumentRepository,
    JobClaimRepository,
    ProcessingRepository,
    WorkerIdentity,
)
from claimcheck.persistence.storage import InvalidStorageKey
from claimcheck.worker.handlers.document_process import LeaseLost
from claimcheck.worker.state import recompute_case_processing_status


class DeleteFailure(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


class DocumentDeleteHandler:
    def __init__(self, session_factory: Callable[[], Session], storage, *, worker_id: str,
                 retry_delay_seconds: int = 30) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.worker_id = worker_id
        self.retry_delay_seconds = retry_delay_seconds

    def handle(self, job_id: UUID) -> None:
        try:
            context = self._start(job_id)
        except DeleteFailure as exc:
            self._fail(job_id, exc.error_code, retryable=exc.retryable)
            return
        except LeaseLost:
            return
        except Exception:
            self._fail(job_id, "DOCUMENT_DELETE_SETUP_FAILED", retryable=True)
            return
        if context is None:
            return
        try:
            for key in sorted(context["keys"]):
                try:
                    self.storage.delete(key)
                except InvalidStorageKey as exc:
                    raise DeleteFailure("INVALID_STORAGE_KEY", retryable=False) from exc
                except OSError as exc:
                    raise DeleteFailure("STORAGE_DELETE_FAILED", retryable=True) from exc
            self._finalize(job_id, context)
        except DeleteFailure as exc:
            self._fail(job_id, exc.error_code, retryable=exc.retryable)
        except LeaseLost:
            return
        except Exception:
            self._fail(job_id, "DOCUMENT_DELETE_FAILED", retryable=True)

    def _start(self, job_id: UUID) -> dict[str, Any] | None:
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_DELETE" or hint.document_id is None:
                    return None
                owner_id, case_id, document_id = hint.owner_id, hint.case_id, hint.document_id
                case = CaseRepository(session, owner_id).get(
                    case_id, include_deleted=True, for_update=True
                )
                document = DocumentRepository(session, owner_id).get(
                    case_id, document_id, include_deleted=True, for_update=True
                )
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    return None
                if document is None:
                    raise DeleteFailure("DOCUMENT_NOT_FOUND", retryable=False)
                if document.state == "DELETED":
                    claim.complete(job_id)
                    return None
                if document.state != "DELETE_PENDING":
                    raise DeleteFailure("DOCUMENT_DELETE_STATE_CONFLICT", retryable=False)
                if case is not None and case.deletion_status == "ACTIVE":
                    # Keep active in the normal state reducer; the queued deletion job reports
                    # the pending operation without marking extraction complete.
                    if case.status != "PROCESSING":
                        case.status = "PROCESSING"
                        case.version += 1
                        case.updated_at = utc_now()
                keys = ProcessingRepository(session, owner_id).artifact_keys_for_document(
                    case_id, document_id
                )
                if document.storage_key:
                    keys.add(document.storage_key)
                return {
                    "owner_id": owner_id,
                    "case_id": case_id,
                    "document_id": document_id,
                    "keys": keys,
                }

    def _finalize(self, job_id: UUID, context: dict[str, Any]) -> None:
        now = utc_now()
        with self.session_factory() as session:
            with session.begin():
                case = CaseRepository(session, context["owner_id"]).get(
                    context["case_id"], include_deleted=True, for_update=True
                )
                document = DocumentRepository(session, context["owner_id"]).get(
                    context["case_id"], context["document_id"], include_deleted=True,
                    for_update=True,
                )
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    raise LeaseLost
                if document is None:
                    raise DeleteFailure("DOCUMENT_NOT_FOUND", retryable=False)
                # Purging run rows removes text, evidence quotes, parser records and field values
                # from the database after their private artifacts have been removed.
                # Document-level privacy purge is the only permitted deletion exception to
                # append-only review history. Normal API/database operations cannot set it.
                session.execute(text(
                    "SELECT set_config('claimcheck.purge_document_artifacts', 'on', true)"
                ))
                ProcessingRepository(session, context["owner_id"]).purge_runs_for_document(
                    context["case_id"], context["document_id"]
                )
                document.state = "DELETED"
                document.storage_key = None
                document.original_filename = "deleted.pdf"
                document.sha256 = "0" * 64
                document.byte_size = 0
                document.page_count = 0
                document.idempotency_key = None
                document.deleted_at = now
                completed = claim.complete(job_id, now=now)
                if completed is None:
                    raise LeaseLost
                AuditRepository(session, context["owner_id"]).append(
                    event_type="DOCUMENT_DELETED",
                    resource_type="DOCUMENT",
                    resource_id=document.document_id,
                    case_id=context["case_id"],
                    request_id=None,
                    event_data={"content_removed": True},
                    actor_type="WORKER",
                )
                AuditRepository(session, context["owner_id"]).append(
                    event_type="JOB_COMPLETED",
                    resource_type="JOB",
                    resource_id=job_id,
                    case_id=context["case_id"],
                    request_id=None,
                    event_data={"job_type": "DOCUMENT_DELETE"},
                    actor_type="WORKER",
                )
                if case is not None:
                    recompute_case_processing_status(
                        session, case, owner_id=context["owner_id"]
                    )

    def _fail(self, job_id: UUID, error_code: str, *, retryable: bool) -> None:
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_DELETE":
                    return
                owner_id, case_id, document_id = hint.owner_id, hint.case_id, hint.document_id
                case = CaseRepository(session, owner_id).get(
                    case_id, include_deleted=True, for_update=True
                )
                document = DocumentRepository(session, owner_id).get(
                    case_id, document_id, include_deleted=True, for_update=True
                ) if document_id is not None else None
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    return
                failed = claim.fail(
                    job_id, error_code=error_code, retryable=retryable,
                    retry_delay=timedelta(seconds=self.retry_delay_seconds),
                )
                if failed is None:
                    return
                if failed.status == "FAILED" and document is not None and \
                        document.state == "DELETE_PENDING":
                    document.state = "FAILED"
                if case is not None:
                    recompute_case_processing_status(session, case, owner_id=owner_id)
                AuditRepository(session, owner_id).append(
                    event_type="JOB_FAILED",
                    resource_type="JOB",
                    resource_id=job_id,
                    case_id=case_id,
                    request_id=None,
                    result_code=error_code,
                    event_data={"retryable": failed.status == "RETRYABLE_FAILURE"},
                    actor_type="WORKER",
                )
