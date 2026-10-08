"""PostgreSQL-backed worker loop with atomic claims, leases, retries and clean shutdown."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from claimcheck.persistence.models import (
    DocumentProcessingRun,
    ProcessingJob,
    utc_now,
)
from claimcheck.persistence.repositories import (
    CaseRepository,
    DocumentRepository,
    JobClaimRepository,
    WorkerIdentity,
)
from claimcheck.worker.state import recompute_case_processing_status
from claimcheck.worker.handlers.document_delete import DocumentDeleteHandler
from claimcheck.worker.handlers.document_process import DocumentProcessHandler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    retry_delay_seconds: int = 30
    max_pdf_pages: int = 250
    max_pdf_bytes: int = 25 * 1024 * 1024
    ocr_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.worker_id or len(self.worker_id) > 128:
            raise ValueError("Worker id must be 1..128 characters.")
        if self.poll_interval_seconds <= 0 or self.lease_seconds <= 0 or \
                self.heartbeat_seconds <= 0 or self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("Worker polling and lease intervals must be positive and consistent.")
        if self.max_pdf_pages < 1 or self.max_pdf_bytes < 1:
            raise ValueError("Worker document limits must be positive.")


class _LeaseHeartbeat:
    def __init__(self, session_factory: Callable[[], Session], worker_id: str,
                 job_id: UUID, *, lease_seconds: int, heartbeat_seconds: int) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.job_id = job_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="claimcheck-lease-heartbeat",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.heartbeat_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                with self.session_factory() as session:
                    with session.begin():
                        renewed = JobClaimRepository(
                            session, WorkerIdentity(self.worker_id)
                        ).renew(self.job_id, lease_for=timedelta(seconds=self.lease_seconds))
                if not renewed:
                    return
            except Exception:
                # A failed heartbeat is not logged with driver exception detail; the next
                # ownership-checked transaction prevents a stale worker from committing.
                return


class DocumentWorker:
    """Poll and execute DOCUMENT_PROCESS / DOCUMENT_DELETE job types only."""

    def __init__(self, session_factory: Callable[[], Session], storage, *,
                 settings: WorkerSettings) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        common = {
            "session_factory": session_factory,
            "storage": storage,
            "worker_id": settings.worker_id,
        }
        self.process_handler = DocumentProcessHandler(
            **common,
            max_pdf_pages=settings.max_pdf_pages,
            max_pdf_bytes=settings.max_pdf_bytes,
            ocr_enabled=settings.ocr_enabled,
            retry_delay_seconds=settings.retry_delay_seconds,
        )
        self.delete_handler = DocumentDeleteHandler(
            **common, retry_delay_seconds=settings.retry_delay_seconds
        )

    def run_once(self) -> bool:
        with self.session_factory() as session:
            with session.begin():
                claim = JobClaimRepository(session, WorkerIdentity(self.settings.worker_id))
                expired_job_ids = claim.expire_exhausted()
                if expired_job_ids:
                    self._recover_expired_jobs(session, expired_job_ids)
                job = claim.claim_next(
                    lease_for=timedelta(seconds=self.settings.lease_seconds)
                )
                if job is None:
                    return False
                job_id, job_type = job.job_id, job.job_type
        heartbeat = _LeaseHeartbeat(
            self.session_factory,
            self.settings.worker_id,
            job_id,
            lease_seconds=self.settings.lease_seconds,
            heartbeat_seconds=self.settings.heartbeat_seconds,
        )
        heartbeat.start()
        try:
            if job_type == "DOCUMENT_PROCESS":
                self.process_handler.handle(job_id)
            elif job_type == "DOCUMENT_DELETE":
                self.delete_handler.handle(job_id)
            else:  # claim repository already enforces this; guard old/corrupt rows.
                self._fail_unsupported(job_id)
        except Exception:
            # Handler boundaries are safe, but keep one last shield around the worker loop.
            logger.error("worker job failed", extra={"job_id": str(job_id),
                                                       "error_code": "WORKER_INTERNAL_ERROR"})
            self._fail_unsupported(job_id, error_code="WORKER_INTERNAL_ERROR")
        finally:
            heartbeat.close()
        return True

    def _recover_expired_jobs(self, session: Session, expired_job_ids: list[UUID]) -> None:
        for job_id in expired_job_ids:
            hint = session.get(ProcessingJob, job_id)
            if hint is None:
                continue
            case = CaseRepository(session, hint.owner_id).get(
                hint.case_id, include_deleted=True, for_update=True
            )
            document = (DocumentRepository(session, hint.owner_id).get(
                hint.case_id, hint.document_id, include_deleted=True, for_update=True
            ) if hint.document_id is not None else None)
            now = utc_now()
            job = session.scalar(select(ProcessingJob).where(
                ProcessingJob.job_id == job_id,
                ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
                ProcessingJob.status == "RUNNING",
                ProcessingJob.lease_expires_at.is_not(None),
                ProcessingJob.lease_expires_at <= now,
                ProcessingJob.attempt_count >= ProcessingJob.max_attempts,
            ).with_for_update())
            if job is None:
                continue
            job.status = "FAILED"
            job.error_code = "LEASE_EXPIRED_MAX_ATTEMPTS"
            job.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            if job.job_type == "DOCUMENT_PROCESS":
                run_id = (job.payload_json or {}).get("processing_run_id")
                try:
                    run_id = UUID(str(run_id))
                except (TypeError, ValueError):
                    run_id = None
                if run_id is not None:
                    run = session.scalar(select(DocumentProcessingRun).where(
                        DocumentProcessingRun.owner_id == job.owner_id,
                        DocumentProcessingRun.case_id == job.case_id,
                        DocumentProcessingRun.document_id == job.document_id,
                        DocumentProcessingRun.processing_run_id == run_id,
                        DocumentProcessingRun.status == "RUNNING",
                    ).with_for_update())
                    if run is not None:
                        run.status = "FAILED"
                        run.error_code = "LEASE_EXPIRED_MAX_ATTEMPTS"
                        run.finished_at = now
                if document is not None and document.state not in {"DELETED", "DELETE_PENDING"}:
                    document.state = "FAILED"
            elif document is not None and document.state == "DELETE_PENDING":
                document.state = "FAILED"
            if case is not None:
                recompute_case_processing_status(session, case, owner_id=job.owner_id)

    def run_forever(self, shutdown: threading.Event) -> None:
        while not shutdown.is_set():
            did_work = self.run_once()
            if not did_work:
                shutdown.wait(self.settings.poll_interval_seconds)

    def _fail_unsupported(self, job_id: UUID, *, error_code: str = "UNSUPPORTED_JOB_TYPE") -> None:
        with self.session_factory() as session:
            with session.begin():
                JobClaimRepository(session, WorkerIdentity(self.settings.worker_id)).fail(
                    job_id,
                    error_code=error_code,
                    retryable=False,
                )
