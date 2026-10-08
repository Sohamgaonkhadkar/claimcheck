"""DOCUMENT_PROCESS job handler using the existing PDF/layout/OCR/parser stack."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from claimcheck.application.document_processing import (
    EXTRACTION_VERSION,
    ProcessingFailure,
    ProcessingOutput,
    process_document_bytes,
)
from claimcheck.persistence.models import (
    Case,
    CaseDocument,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    JobStatus,
    ProcessingJob,
    StoredPageExtraction,
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
from claimcheck.worker.state import recompute_case_processing_status


class LeaseLost(RuntimeError):
    """The job is no longer owned by this worker; no result may be committed."""


class DocumentProcessHandler:
    def __init__(self, session_factory: Callable[[], Session], storage, *, worker_id: str,
                 max_pdf_pages: int = 250, max_pdf_bytes: int = 25 * 1024 * 1024,
                 ocr_enabled: bool = True, retry_delay_seconds: int = 30) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.worker_id = worker_id
        self.max_pdf_pages = max_pdf_pages
        self.max_pdf_bytes = max_pdf_bytes
        self.ocr_enabled = ocr_enabled
        self.retry_delay_seconds = retry_delay_seconds

    def handle(self, job_id: UUID) -> None:
        try:
            context = self._mark_running(job_id)
        except ProcessingFailure as exc:
            self._fail(job_id, exc.error_code, retryable=exc.retryable)
            return
        except LeaseLost:
            return
        except Exception:
            self._fail(job_id, "PROCESSING_SETUP_FAILED", retryable=True)
            return
        if context is None:
            return
        try:
            try:
                data = self.storage.get(context["storage_key"])
            except FileNotFoundError as exc:
                raise ProcessingFailure("ORIGINAL_OBJECT_MISSING") from exc
            except InvalidStorageKey as exc:
                raise ProcessingFailure("INVALID_STORAGE_KEY") from exc
            except OSError as exc:
                raise ProcessingFailure("STORAGE_READ_FAILED", retryable=True) from exc
            if len(data) > self.max_pdf_bytes:
                raise ProcessingFailure("STORED_OBJECT_TOO_LARGE")
            output = process_document_bytes(
                data,
                case_id=context["case_id"],
                document_id=context["document_id"],
                processing_run_id=context["processing_run_id"],
                assigned_role=context["assigned_role"],
                expected_sha256=context["source_sha256"],
                expected_pages=context["page_count"],
                max_pages=self.max_pdf_pages,
                ocr_enabled=self.ocr_enabled,
            )
            artifacts, page_keys, evidence_keys = self._artifact_plan(
                context, output
            )
            self._reserve_artifacts(job_id, artifacts)
            self._write_artifacts(job_id, context, artifacts)
            self._finalize(job_id, context, output, page_keys, evidence_keys)
        except ProcessingFailure as exc:
            self._fail(job_id, exc.error_code, retryable=exc.retryable)
        except LeaseLost:
            # A delete/cancel/expired lease won the race. The next owner or delete job is
            # responsible for final state; do not leak the exception or persist stale output.
            return
        except Exception as e:
            # No exception message/traceback can contain patient text, paths, or library detail.
            import traceback; traceback.print_exc()
            self._fail(job_id, "PROCESSING_FAILED", retryable=True)
    def _mark_running(self, job_id: UUID) -> dict[str, Any] | None:
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_PROCESS":
                    return None
                owner_id, case_id, document_id = hint.owner_id, hint.case_id, hint.document_id
                case = CaseRepository(session, owner_id).get(
                    case_id, include_deleted=True, for_update=True
                )
                document = (DocumentRepository(session, owner_id).get(
                    case_id, document_id, include_deleted=True, for_update=True
                ) if document_id is not None else None)
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    return None
                if document_id is None:
                    raise ProcessingFailure("JOB_DOCUMENT_MISSING")
                if case is None or case.deletion_status != "ACTIVE":
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = utc_now()
                    job.error_code = "CASE_UNAVAILABLE"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    return None
                if document is None or document.state in {"DELETE_PENDING", "DELETED"}:
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = utc_now()
                    job.error_code = "DOCUMENT_UNAVAILABLE"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    return None
                payload = dict(job.payload_json or {})
                try:
                    run_id = UUID(str(payload["processing_run_id"]))
                except (KeyError, ValueError, TypeError) as exc:
                    raise ProcessingFailure("JOB_RUN_REFERENCE_INVALID") from exc
                run = session.scalar(select(DocumentProcessingRun).where(
                    DocumentProcessingRun.owner_id == owner_id,
                    DocumentProcessingRun.case_id == case_id,
                    DocumentProcessingRun.document_id == document_id,
                    DocumentProcessingRun.processing_run_id == run_id,
                    DocumentProcessingRun.job_id == job_id,
                ).with_for_update())
                if run is None:
                    raise ProcessingFailure("PROCESSING_RUN_NOT_FOUND")
                if run.status not in {"QUEUED", "RUNNING"}:
                    # A committed terminal run and job are atomic; this only guards malformed
                    # old rows or manual DB changes.
                    raise ProcessingFailure("PROCESSING_RUN_STATE_CONFLICT")
                if not document.storage_key:
                    raise ProcessingFailure("ORIGINAL_OBJECT_MISSING")
                if document.byte_size <= 0 or document.page_count <= 0:
                    raise ProcessingFailure("DOCUMENT_METADATA_INVALID")
                document.state = "PROCESSING"
                run.status = "RUNNING"
                run.started_at = run.started_at or utc_now()
                run.error_code = None
                if case.status != "PROCESSING":
                    case.status = "PROCESSING"
                    case.version += 1
                    case.updated_at = utc_now()
                return {
                    "owner_id": owner_id,
                    "case_id": case_id,
                    "document_id": document_id,
                    "processing_run_id": run_id,
                    "storage_key": document.storage_key,
                    "source_sha256": document.sha256,
                    "page_count": document.page_count,
                    "byte_size": document.byte_size,
                    # Role assignments are versioned; never let a later mutable document
                    # projection rewrite the historical processing-run role.
                    "assigned_role": run.assigned_role,
                }

    def _artifact_plan(self, context: dict[str, Any], output: ProcessingOutput):
        artifacts: dict[str, bytes] = {}
        page_keys: dict[tuple[int, str], str] = {}
        evidence_keys: dict[UUID, tuple[str, str]] = {}

        def add(kind: str, payload: bytes) -> tuple[str, str]:
            digest = hashlib.sha256(payload).hexdigest()
            key = (f"cases/{context['case_id'].hex}/derived/{context['document_id'].hex}/"
                   f"{context['processing_run_id'].hex}/{kind}/{digest}.bin")
            artifacts[key] = payload
            return key, digest

        for page in output.pages:
            if page.layout_text is not None:
                page_keys[(page.page_number, "layout")] = add(
                    f"page-{page.page_number:04d}-layout", page.layout_text.encode("utf-8")
                )[0]
            if page.native_text is not None:
                page_keys[(page.page_number, "native")] = add(
                    f"page-{page.page_number:04d}-native", page.native_text.encode("utf-8")
                )[0]
        for span in output.evidence:
            raw = span.quote.encode("utf-8")
            key, digest = add(f"evidence-{span.evidence_id.hex}", raw)
            evidence_keys[span.evidence_id] = (key, digest)
        return artifacts, page_keys, evidence_keys

    def _reserve_artifacts(self, job_id: UUID, artifacts: dict[str, bytes]) -> None:
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_PROCESS":
                    raise LeaseLost
                case = CaseRepository(session, hint.owner_id).get(
                    hint.case_id, include_deleted=True, for_update=True
                )
                document = (DocumentRepository(session, hint.owner_id).get(
                    hint.case_id, hint.document_id, include_deleted=True, for_update=True
                ) if hint.document_id is not None else None)
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    raise LeaseLost
                if case is None or case.deletion_status != "ACTIVE" or document is None or \
                        document.state in {"DELETE_PENDING", "DELETED"}:
                    raise LeaseLost
                payload = dict(job.payload_json or {})
                keys = set(item for item in payload.get("artifact_keys", []) if isinstance(item, str))
                keys.update(artifacts)
                payload["artifact_keys"] = sorted(keys)
                job.payload_json = payload
                job.updated_at = utc_now()

    def _write_artifacts(self, job_id: UUID, context: dict[str, Any],
                         artifacts: dict[str, bytes]) -> None:
        # Holding the case/document row locks during this short file stage serializes it with
        # DELETE_PENDING. A delete cannot clean a manifest and then race a late atomic write.
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_PROCESS":
                    raise LeaseLost
                case = CaseRepository(session, context["owner_id"]).get(
                    context["case_id"], include_deleted=True, for_update=True
                )
                document = DocumentRepository(session, context["owner_id"]).get(
                    context["case_id"], context["document_id"], include_deleted=True,
                    for_update=True,
                )
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id, lock=False)
                if job is None or case is None or case.deletion_status != "ACTIVE" or \
                        document is None or document.state in {"DELETE_PENDING", "DELETED"}:
                    raise LeaseLost
                for key, payload in artifacts.items():
                    self._put_immutable(key, payload)

    def _put_immutable(self, key: str, payload: bytes) -> None:
        expected = hashlib.sha256(payload).hexdigest()
        try:
            current = self.storage.metadata(key)
        except FileNotFoundError:
            current = None
        except InvalidStorageKey as exc:
            raise ProcessingFailure("INVALID_ARTIFACT_KEY") from exc
        except OSError as exc:
            raise ProcessingFailure("STORAGE_READ_FAILED", retryable=True) from exc
        if current is not None:
            if current.sha256 != expected or current.size_bytes != len(payload):
                raise ProcessingFailure("ARTIFACT_HASH_MISMATCH")
            return
        try:
            self.storage.put(key, payload)
            written = self.storage.metadata(key)
        except InvalidStorageKey as exc:
            raise ProcessingFailure("INVALID_ARTIFACT_KEY") from exc
        except OSError as exc:
            raise ProcessingFailure("STORAGE_WRITE_FAILED", retryable=True) from exc
        if written.sha256 != expected or written.size_bytes != len(payload):
            raise ProcessingFailure("ARTIFACT_HASH_MISMATCH")

    def _finalize(self, job_id: UUID, context: dict[str, Any], output: ProcessingOutput,
                  page_keys: dict[tuple[int, str], str],
                  evidence_keys: dict[UUID, tuple[str, str]]) -> None:
        now = utc_now()
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_PROCESS":
                    raise LeaseLost
                case = CaseRepository(session, context["owner_id"]).get(
                    context["case_id"], include_deleted=True, for_update=True
                )
                document = DocumentRepository(session, context["owner_id"]).get(
                    context["case_id"], context["document_id"], include_deleted=True,
                    for_update=True,
                )
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None or case is None or case.deletion_status != "ACTIVE" or \
                        document is None or document.state in {"DELETE_PENDING", "DELETED"}:
                    raise LeaseLost
                run = session.scalar(select(DocumentProcessingRun).where(
                    DocumentProcessingRun.owner_id == context["owner_id"],
                    DocumentProcessingRun.case_id == context["case_id"],
                    DocumentProcessingRun.document_id == context["document_id"],
                    DocumentProcessingRun.processing_run_id == context["processing_run_id"],
                ).with_for_update())
                if run is None or run.status != "RUNNING":
                    raise ProcessingFailure("PROCESSING_RUN_STATE_CONFLICT")

                layout_by_page = {(page.page_number, "layout"): page for page in output.pages}
                for page in output.pages:
                    layout_key = page_keys.get((page.page_number, "layout"))
                    native_key = page_keys.get((page.page_number, "native"))
                    layout_sha = _artifact_hash(page.layout_text) if page.layout_text is not None else None
                    native_sha = _artifact_hash(page.native_text) if page.native_text is not None else None
                    session.add(StoredPageExtraction(
                        page_extraction_id=_stable_id(run.processing_run_id,
                                                      f"page:{page.page_number}"),
                        owner_id=context["owner_id"],
                        case_id=context["case_id"],
                        document_id=context["document_id"],
                        processing_run_id=run.processing_run_id,
                        page_number=page.page_number,
                        layout_state=page.layout_state,
                        native_state=page.native_state,
                        layout_method=page.layout_method,
                        native_method=page.native_method,
                        layout_text_artifact_key=layout_key,
                        layout_text_sha256=layout_sha,
                        native_text_artifact_key=native_key,
                        native_text_sha256=native_sha,
                        width_points=page.width_points,
                        height_points=page.height_points,
                        ocr_confidence=page.ocr_confidence,
                        quality_json=page.quality,
                    ))
                # Evidence spans have a composite FK to the per-run page rows. The mappers
                # intentionally have no bidirectional ORM relationship, so make this
                # dependency explicit before staging the evidence batch.
                session.flush()
                for span in output.evidence:
                    artifact = evidence_keys.get(span.evidence_id)
                    if artifact is None:
                        raise ProcessingFailure("EVIDENCE_ARTIFACT_MISSING")
                    key, quote_sha = artifact
                    session.add(DocumentEvidenceSpan(
                        evidence_id=span.evidence_id,
                        owner_id=context["owner_id"],
                        case_id=context["case_id"],
                        document_id=context["document_id"],
                        processing_run_id=run.processing_run_id,
                        page_number=span.page_number,
                        quote_artifact_key=key,
                        quote_sha256=quote_sha,
                        source_sha256=span.source_sha256,
                        page_text_sha256=span.page_text_sha256,
                        char_start=span.char_start,
                        char_end=span.char_end,
                        reader=span.reader,
                        extraction_method=span.extraction_method,
                        verification_status=span.verification_status,
                        verification_method=span.verification_method,
                        verify_score=span.verify_score,
                        bbox_json=span.bbox,
                        bbox_is_measured=span.bbox_is_measured,
                        injection_flags_json=span.injection_flags,
                        reason_code=span.reason_code,
                    ))
                # Processing fields also reference evidence through composite scope FKs.
                session.flush()
                for item in output.fields:
                    session.add(DocumentProcessingField(
                        processing_field_id=_stable_id(run.processing_run_id,
                                                       f"field:{item.field_path}"),
                        owner_id=context["owner_id"],
                        case_id=context["case_id"],
                        document_id=context["document_id"],
                        processing_run_id=run.processing_run_id,
                        field_path=item.field_path,
                        value_json=item.value,
                        state=item.state,
                        evidence_id=item.evidence_id,
                        reason_code=item.reason_code,
                        provenance_json=item.provenance,
                    ))
                for item in output.records:
                    session.add(DocumentProcessingRecord(
                        processing_record_id=_stable_id(
                            run.processing_run_id,
                            f"record:{item.record_type}:{item.record_key}",
                        ),
                        owner_id=context["owner_id"],
                        case_id=context["case_id"],
                        document_id=context["document_id"],
                        processing_run_id=run.processing_run_id,
                        record_type=item.record_type,
                        record_key=item.record_key,
                        field_state=item.state,
                        evidence_id=item.evidence_id,
                        record_json=item.record,
                    ))
                run.status = output.status
                run.detected_role = output.detected_role
                run.role_mismatch = output.role_mismatch
                run.summary_json = output.summary
                run.error_code = None
                run.finished_at = now
                document.state = output.status
                document.page_count = output.page_count
                document.sha256 = context["source_sha256"]
                completed = claim.complete(job_id, now=now)
                if completed is None:
                    raise LeaseLost
                recompute_case_processing_status(session, case, owner_id=context["owner_id"])
                AuditRepository(session, context["owner_id"]).append(
                    event_type="JOB_COMPLETED",
                    resource_type="JOB",
                    resource_id=job_id,
                    case_id=context["case_id"],
                    request_id=None,
                    event_data={"job_type": "DOCUMENT_PROCESS", "run_status": output.status},
                    actor_type="WORKER",
                )

    def _fail(self, job_id: UUID, error_code: str, *, retryable: bool) -> None:
        with self.session_factory() as session:
            with session.begin():
                hint = session.get(ProcessingJob, job_id)
                if hint is None or hint.job_type != "DOCUMENT_PROCESS":
                    return
                owner_id, case_id, document_id = hint.owner_id, hint.case_id, hint.document_id
                case = CaseRepository(session, owner_id).get(
                    case_id, include_deleted=True, for_update=True
                )
                document = (DocumentRepository(session, owner_id).get(
                    case_id, document_id, include_deleted=True, for_update=True
                ) if document_id is not None else None)
                claim = JobClaimRepository(session, WorkerIdentity(self.worker_id))
                job = claim.get_owned_running(job_id)
                if job is None:
                    return
                run_id = None
                try:
                    run_id = UUID(str((job.payload_json or {}).get("processing_run_id")))
                except (ValueError, TypeError):
                    pass
                terminal_job = claim.fail(
                    job_id, error_code=error_code, retryable=retryable,
                    retry_delay=timedelta(seconds=self.retry_delay_seconds),
                )
                if terminal_job is None:
                    return
                if run_id is not None:
                    run = session.scalar(select(DocumentProcessingRun).where(
                        DocumentProcessingRun.owner_id == owner_id,
                        DocumentProcessingRun.case_id == case_id,
                        DocumentProcessingRun.document_id == document_id,
                        DocumentProcessingRun.processing_run_id == run_id,
                    ).with_for_update())
                else:
                    run = None
                if terminal_job.status == "RETRYABLE_FAILURE":
                    if run is not None:
                        run.status = "QUEUED"
                        run.error_code = error_code
                        run.finished_at = None
                    if document is not None and document.state not in {"DELETE_PENDING", "DELETED"}:
                        document.state = "QUEUED"
                else:
                    if run is not None:
                        run.status = "FAILED"
                        run.error_code = error_code
                        run.finished_at = utc_now()
                    if document is not None and document.state not in {"DELETE_PENDING", "DELETED"}:
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
                    event_data={"retryable": terminal_job.status == "RETRYABLE_FAILURE"},
                    actor_type="WORKER",
                )


def _artifact_hash(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _stable_id(run_id: UUID, name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"claimcheck:{run_id}:{name}")
