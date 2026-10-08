"""Phase 2 application services; all product reads/mutations are owner-scoped."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from claimcheck.application.errors import ApplicationError
from claimcheck.application.identity import OwnerIdentity
from claimcheck.application.input_revision import append_input_revision
from claimcheck.application.analysis_state import project_case_analysis_status
from claimcheck.application.review_readiness import (
    evaluate_case_readiness,
    latest_role_assignments,
)
from claimcheck.ingest.model import Storage
from claimcheck.persistence.models import (
    Case,
    CaseDocument,
    DocumentStatus,
    DocumentProcessingRun,
    JobStatus,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentRoleAssignment,
    Owner,
    ProcessingJob,
    utc_now,
)
from claimcheck.persistence.storage import InvalidStorageKey
from claimcheck.persistence.repositories import (
    AnalysisRunRepository,
    AuditRepository,
    CaseRepository,
    DocumentRepository,
    InvalidPageCursor,
    JobRepository,
    ProcessingRepository,
)
from .upload_validation import ValidatedPDF


@dataclass(frozen=True)
class UploadResult:
    document: CaseDocument
    job: ProcessingJob
    duplicate: bool
    selected_for_role: bool = False


@dataclass(frozen=True)
class DeletionResult:
    case_id: UUID
    deletion_job_id: UUID | None
    status: str


@dataclass(frozen=True)
class DocumentDeletionResult:
    document: CaseDocument
    job: ProcessingJob | None
    duplicate: bool


def _ensure_owner(session: Session, principal: OwnerIdentity) -> None:
    owner = session.get(Owner, principal.owner_id)
    if owner is None:
        session.add(Owner(
            owner_id=principal.owner_id,
            identity_provider=principal.identity_provider,
            subject=principal.subject,
        ))
        session.flush()
        return
    if (owner.identity_provider != principal.identity_provider
            or owner.subject != principal.subject):
        raise ApplicationError(
            409,
            "IDENTITY_CONFLICT",
            "Identity conflict",
            "The configured identity could not be matched to its owner record.",
        )


def _selected_for_role(session: Session, owner_id: UUID, case_id: UUID,
                       document_id: UUID, role: str) -> bool:
    assignment = latest_role_assignments(session, owner_id, case_id).get(role)
    return bool(assignment is not None and assignment.document_id == document_id)


def _selected_role_for_document(session: Session, owner_id: UUID, case_id: UUID,
                                document_id: UUID) -> str | None:
    assignments = latest_role_assignments(session, owner_id, case_id)
    return next((role for role, assignment in assignments.items()
                 if assignment.document_id == document_id), None)


def _refresh_case_readiness(session: Session, case: Case, owner_id: UUID) -> str:
    active = session.scalar(select(func.count()).select_from(ProcessingJob).where(
        ProcessingJob.owner_id == owner_id,
        ProcessingJob.case_id == case.case_id,
        ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
        ProcessingJob.status.in_(("QUEUED", "RUNNING", "RETRYABLE_FAILURE")),
    )) or 0
    report = evaluate_case_readiness(session, owner_id, case)
    projected = ("PROCESSING" if active else project_case_analysis_status(
        session, owner_id, case, report.status.value
    ))
    if case.status != projected:
        case.status = projected
        case.version += 1
        case.updated_at = utc_now()
    return projected


def _checklist(roles: set[str]) -> dict[str, str]:
    return {
        "policy_wording": "PRESENT" if "policy_wording" in roles else "MISSING",
        "policy_schedule": "PRESENT" if "policy_schedule" in roles else "MISSING",
        "bill": "PRESENT" if "bill" in roles else "MISSING",
        "settlement": "PRESENT" if "settlement" in roles else "MISSING",
    }


def _case_view(case: Case, roles: set[str] | None = None) -> dict:
    roles = roles or set()
    return {
        "case_id": case.case_id,
        "display_name": case.display_name,
        "status": case.status,
        "claim_date": case.claim_date,
        "currency": "INR",
        "document_checklist": _checklist(roles),
        "latest_analysis_run_id": case.latest_analysis_run_id,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "version": case.version,
        "input_revision": case.input_revision,
    }


def document_view(document: CaseDocument, *, selected_for_role: bool = False) -> dict:
    return {
        "document_id": document.document_id,
        "case_id": document.case_id,
        "role": document.role,
        "original_filename": document.original_filename,
        "media_type": document.media_type,
        "byte_size": document.byte_size,
        "page_count": document.page_count,
        "state": document.state,
        "uploaded_at": document.uploaded_at,
        "selected_for_role": selected_for_role,
    }


def job_view(job: ProcessingJob) -> dict:
    return {
        "job_id": job.job_id,
        "case_id": job.case_id,
        "document_id": job.document_id,
        "analysis_run_id": job.analysis_run_id,
        "job_type": job.job_type,
        "stage": job.stage,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "available_at": job.available_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error_code": job.error_code,
        "retryable": job.status == JobStatus.RETRYABLE_FAILURE.value,
        "progress": None,
    }


def processing_run_view(run: DocumentProcessingRun) -> dict:
    return {
        "processing_run_id": run.processing_run_id,
        "run_number": run.run_number,
        "status": run.status,
        "extraction_version": run.extraction_version,
        "assigned_role": run.assigned_role,
        "detected_role": run.detected_role,
        "role_mismatch": run.role_mismatch,
        "page_count": run.page_count,
        "summary": run.summary_json,
        "error_code": run.error_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


class CaseApplicationService:
    def __init__(self, session: Session, storage: Storage, principal: OwnerIdentity) -> None:
        self.session = session
        self.storage = storage
        self.principal = principal
        self.owner_id = principal.owner_id

    def create_case(self, *, display_name: str | None, claim_date,
                    request_id: str) -> dict:
        with self.session.begin():
            _ensure_owner(self.session, self.principal)
            case = Case(
                case_id=uuid4(),
                owner_id=self.owner_id,
                display_name=display_name.strip() or None if display_name is not None else None,
                claim_date=claim_date,
                status="CREATED",
                deletion_status="ACTIVE",
                version=1,
            )
            self.session.add(case)
            self.session.flush()
            AuditRepository(self.session, self.owner_id).append(
                event_type="CASE_CREATED",
                resource_type="CASE",
                resource_id=case.case_id,
                case_id=case.case_id,
                request_id=request_id,
                event_data={"status": "CREATED"},
            )
            return _case_view(case)

    def list_cases(self, *, limit: int, cursor: str | None, status: str | None) -> dict:
        with self.session.begin():
            case_repo = CaseRepository(self.session, self.owner_id)
            try:
                page = case_repo.list(limit=limit, cursor=cursor, status=status)
            except InvalidPageCursor as exc:
                raise ApplicationError(400, "INVALID_CURSOR", "Invalid cursor",
                                   "The pagination cursor is invalid.") from exc
            for case in page.items:
                _refresh_case_readiness(self.session, case, self.owner_id)
            roles = DocumentRepository(self.session, self.owner_id).roles_for_cases(
                [case.case_id for case in page.items]
            )
            return {
                "items": [_case_view(case, roles.get(case.case_id, set())) for case in page.items],
                "next_cursor": page.next_cursor,
            }

    def get_case(self, case_id: UUID) -> dict:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            if case is None:
                raise _not_found()
            _refresh_case_readiness(self.session, case, self.owner_id)
            roles = DocumentRepository(self.session, self.owner_id).roles_for_cases([case_id])
            return _case_view(case, roles.get(case_id, set()))

    def update_case(self, case_id: UUID, *, fields: dict, request_id: str) -> dict:
        if not fields:
            raise ApplicationError(422, "EMPTY_UPDATE", "No changes supplied",
                               "Provide at least one editable case field.")
        allowed = {"display_name", "claim_date"}
        if set(fields) - allowed:
            raise ApplicationError(422, "INVALID_UPDATE", "Invalid case update",
                               "Only display name and claim date can be updated.")
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            if case is None:
                raise _not_found()
            if "display_name" in fields:
                value = fields["display_name"]
                case.display_name = value.strip() or None if value is not None else None
            input_revision = None
            if "claim_date" in fields:
                next_claim_date = fields["claim_date"]
                if next_claim_date != case.claim_date:
                    case.claim_date = next_claim_date
                    input_revision = append_input_revision(
                        self.session,
                        case,
                        reason_code="CLAIM_DATE_UPDATED",
                        actor_type="OWNER",
                        resource_type="CASE",
                        resource_id=case_id,
                        request_id=request_id,
                    )
            case.version += 1
            case.updated_at = utc_now()
            AuditRepository(self.session, self.owner_id).append(
                event_type="CASE_UPDATED",
                resource_type="CASE",
                resource_id=case.case_id,
                case_id=case.case_id,
                request_id=request_id,
                event_data={"changed_fields": sorted(fields.keys()), "version": case.version,
                            "input_revision": input_revision},
            )
            if input_revision is not None:
                AuditRepository(self.session, self.owner_id).append(
                    event_type="CASE_INPUT_REVISION_CREATED",
                    resource_type="CASE_INPUT_REVISION",
                    resource_id=None,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"input_revision": input_revision,
                                "reason_code": "CLAIM_DATE_UPDATED"},
                )
            _refresh_case_readiness(self.session, case, self.owner_id)
            roles = DocumentRepository(self.session, self.owner_id).roles_for_cases([case_id])
            return _case_view(case, roles.get(case_id, set()))

    def ensure_case_access(self, case_id: UUID) -> None:
        """Check ownership before the HTTP layer parses or stores a multipart body."""
        with self.session.begin():
            if CaseRepository(self.session, self.owner_id).get(case_id) is None:
                raise _not_found()

    def record_document_rejection(self, case_id: UUID, *, reason_code: str,
                                  request_id: str) -> None:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id)
            if case is None:
                raise _not_found()
            AuditRepository(self.session, self.owner_id).append(
                event_type="DOCUMENT_REJECTED",
                resource_type="DOCUMENT_UPLOAD",
                resource_id=None,
                case_id=case_id,
                request_id=request_id,
                result_code=reason_code,
                event_data={"reason_code": reason_code},
            )

    def upload_document(self, case_id: UUID, *, role: str, pdf: ValidatedPDF,
                        idempotency_key: str | None, request_id: str) -> UploadResult:
        storage_key: str | None = None
        try:
            with self.session.begin():
                case_repo = CaseRepository(self.session, self.owner_id)
                case = case_repo.get(case_id, for_update=True)
                if case is None:
                    raise _not_found()
                documents = DocumentRepository(self.session, self.owner_id)
                jobs = JobRepository(self.session, self.owner_id)

                if idempotency_key:
                    prior = documents.by_idempotency_key(case_id, idempotency_key)
                    if prior is not None:
                        if prior.sha256 != pdf.sha256 or prior.role != role:
                            raise ApplicationError(
                                409, "IDEMPOTENCY_KEY_REUSED", "Idempotency conflict",
                                "This idempotency key was already used for different upload content.",
                            )
                        prior_job = jobs.for_document(case_id, prior.document_id)
                        if prior_job is None:
                            raise ApplicationError(409, "STATE_CONFLICT", "State conflict",
                                               "The existing upload is not available for retry.")
                        return UploadResult(
                            prior, prior_job, True,
                            _selected_for_role(self.session, self.owner_id, case_id,
                                               prior.document_id, prior.role),
                        )

                duplicate = documents.active_by_hash(case_id, pdf.sha256)
                if duplicate is not None:
                    if duplicate.role != role:
                        raise ApplicationError(
                            409, "DUPLICATE_DOCUMENT", "Duplicate document",
                            "These exact PDF bytes are already attached to this case under another role.",
                        )
                    duplicate_job = jobs.for_document(case_id, duplicate.document_id)
                    if duplicate_job is None:
                        raise ApplicationError(409, "STATE_CONFLICT", "State conflict",
                                           "The existing upload is not available for retry.")
                    return UploadResult(
                        duplicate, duplicate_job, True,
                        _selected_for_role(self.session, self.owner_id, case_id,
                                           duplicate.document_id, duplicate.role),
                    )

                document_id = uuid4()
                token = secrets.token_urlsafe(32)
                storage_key = f"cases/{case_id.hex}/objects/{token}.pdf"
                try:
                    self.storage.put(storage_key, pdf.content)
                    stored = self.storage.metadata(storage_key)
                except OSError as exc:
                    raise ApplicationError(
                        503, "STORAGE_UNAVAILABLE", "Service unavailable",
                        "Private document storage is temporarily unavailable.", retryable=True,
                    ) from exc
                if stored.sha256 != pdf.sha256 or stored.size_bytes != pdf.byte_size:
                    raise ApplicationError(
                        503, "STORAGE_INTEGRITY_FAILURE", "Service unavailable",
                        "The stored document did not pass integrity verification.",
                    )

                document = CaseDocument(
                    document_id=document_id,
                    case_id=case_id,
                    owner_id=self.owner_id,
                    role=role,
                    original_filename=pdf.original_filename,
                    media_type="application/pdf",
                    byte_size=pdf.byte_size,
                    sha256=pdf.sha256,
                    page_count=pdf.page_count,
                    state=DocumentStatus.QUEUED.value,
                    storage_key=storage_key,
                    idempotency_key=idempotency_key,
                )
                self.session.add(document)
                self.session.flush()

                job_id = uuid4()
                processing_run_id = uuid4()
                job = ProcessingJob(
                    job_id=job_id,
                    case_id=case_id,
                    owner_id=self.owner_id,
                    document_id=document_id,
                    job_type="DOCUMENT_PROCESS",
                    stage="READ_AND_PARSE",
                    status=JobStatus.QUEUED.value,
                    attempt_count=0,
                    max_attempts=3,
                    idempotency_key=idempotency_key,
                    payload_json={
                        "document_id": str(document_id),
                        "processing_run_id": str(processing_run_id),
                    },
                )
                self.session.add(job)
                self.session.flush()
                self.session.add(DocumentProcessingRun(
                    processing_run_id=processing_run_id,
                    owner_id=self.owner_id,
                    case_id=case_id,
                    document_id=document_id,
                    job_id=job_id,
                    run_number=1,
                    status="QUEUED",
                    extraction_version="claimcheck-ingest-1",
                    assigned_role=role,
                    detected_role=None,
                    role_mismatch=False,
                    source_sha256=pdf.sha256,
                    page_count=pdf.page_count,
                ))
                self.session.flush()

                input_revision = append_input_revision(
                    self.session,
                    case,
                    reason_code="DOCUMENT_UPLOADED",
                    actor_type="OWNER",
                    resource_type="DOCUMENT",
                    resource_id=document_id,
                    request_id=request_id,
                )
                # The upload form supplied an explicit role. Auto-select only the first
                # active candidate for that role; later candidates remain unselected until
                # the owner explicitly chooses one through the versioned role API.
                prior_assignment = latest_role_assignments(
                    self.session, self.owner_id, case_id
                ).get(role)
                role_candidate_count = self.session.scalar(select(func.count()).select_from(
                    CaseDocument
                ).where(
                    CaseDocument.owner_id == self.owner_id,
                    CaseDocument.case_id == case_id,
                    CaseDocument.role == role,
                    CaseDocument.state.not_in(("DELETE_PENDING", "DELETED")),
                )) or 0
                selected_for_role = prior_assignment is None and role_candidate_count == 1
                if selected_for_role:
                    assignment = DocumentRoleAssignment(
                        assignment_id=uuid4(),
                        owner_id=self.owner_id,
                        case_id=case_id,
                        role=role,
                        document_id=document_id,
                        input_revision=input_revision,
                        source_sha256=pdf.sha256,
                        assignment_source="UPLOAD_ROLE_SELECTION",
                        resolved_mismatch=False,
                    )
                    self.session.add(assignment)
                    self.session.flush()
                    AuditRepository(self.session, self.owner_id).append(
                        event_type="DOCUMENT_ROLE_ASSIGNED",
                        resource_type="DOCUMENT_ROLE_ASSIGNMENT",
                        resource_id=assignment.assignment_id,
                        case_id=case_id,
                        request_id=request_id,
                        event_data={"role": role, "document_id": str(document_id),
                                    "input_revision": input_revision,
                                    "assignment_source": "UPLOAD_ROLE_SELECTION"},
                    )
                AuditRepository(self.session, self.owner_id).append(
                    event_type="CASE_INPUT_REVISION_CREATED",
                    resource_type="CASE_INPUT_REVISION",
                    resource_id=None,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"input_revision": input_revision,
                                "reason_code": "DOCUMENT_UPLOADED"},
                )

                role_set = documents.roles_for_cases([case_id]).get(case_id, set())
                has_policy = bool(role_set & {"policy_wording", "policy_schedule"})
                if has_policy and "bill" in role_set and "settlement" in role_set:
                    case.status = "PROCESSING"
                else:
                    case.status = "AWAITING_DOCUMENTS"
                case.version += 1
                case.updated_at = utc_now()

                audit = AuditRepository(self.session, self.owner_id)
                audit.append(
                    event_type="DOCUMENT_UPLOADED",
                    resource_type="DOCUMENT",
                    resource_id=document_id,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={
                        "role": role,
                        "byte_size": pdf.byte_size,
                        "page_count": pdf.page_count,
                    },
                )
                audit.append(
                    event_type="JOB_CREATED",
                    resource_type="JOB",
                    resource_id=job.job_id,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"job_type": job.job_type},
                )
                return UploadResult(document, job, False, selected_for_role)
        except Exception:
            # The DB transaction may fail after the private file was atomically persisted.
            if storage_key is not None:
                try:
                    self.storage.delete(storage_key)
                except Exception:
                    pass
            raise

    def list_documents(self, case_id: UUID, *, limit: int, cursor: str | None,
                       role: str | None, state: str | None) -> dict:
        with self.session.begin():
            if CaseRepository(self.session, self.owner_id).get(case_id) is None:
                raise _not_found()
            try:
                page = DocumentRepository(self.session, self.owner_id).list_for_case(
                    case_id, limit=limit, cursor=cursor, role=role, state=state
                )
            except InvalidPageCursor as exc:
                raise ApplicationError(400, "INVALID_CURSOR", "Invalid cursor",
                                   "The pagination cursor is invalid.") from exc
            assignments = latest_role_assignments(self.session, self.owner_id, case_id)
            selected_ids = {row.document_id for row in assignments.values()
                            if row.document_id is not None}
            return {
                "items": [document_view(
                    document, selected_for_role=document.document_id in selected_ids
                ) for document in page.items],
                "next_cursor": page.next_cursor,
            }

    def get_document(self, case_id: UUID, document_id: UUID) -> CaseDocument:
        with self.session.begin():
            document = DocumentRepository(self.session, self.owner_id).get(case_id, document_id)
            if document is None:
                raise _not_found()
            return document

    def get_document_detail(self, case_id: UUID, document_id: UUID) -> dict:
        with self.session.begin():
            document = DocumentRepository(self.session, self.owner_id).get(case_id, document_id)
            if document is None:
                raise _not_found()
            runs = ProcessingRepository(self.session, self.owner_id).list_runs(case_id, document_id)
            selected = _selected_for_role(
                self.session, self.owner_id, case_id, document_id, document.role
            )
            selected_role = _selected_role_for_document(
                self.session, self.owner_id, case_id, document_id
            )
            return {
                **document_view(document, selected_for_role=selected or selected_role is not None),
                "processing_runs": [processing_run_view(run) for run in runs],
            }

    def reprocess_document(self, case_id: UUID, document_id: UUID, *,
                           idempotency_key: str | None, request_id: str) -> UploadResult:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            document = DocumentRepository(self.session, self.owner_id).get(
                case_id, document_id, for_update=True
            )
            if case is None or document is None:
                raise _not_found()
            if document.state in {"DELETE_PENDING", "DELETED"} or not document.storage_key:
                raise ApplicationError(409, "DOCUMENT_UNAVAILABLE", "State conflict",
                                       "This document cannot be reprocessed.")
            selected_role = _selected_role_for_document(
                self.session, self.owner_id, case_id, document_id
            )
            assigned_role = selected_role or document.role

            key = None
            if idempotency_key:
                key = "reprocess:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:56]
                prior = self.session.scalar(select(ProcessingJob).where(
                    ProcessingJob.owner_id == self.owner_id,
                    ProcessingJob.case_id == case_id,
                    ProcessingJob.idempotency_key == key,
                ).with_for_update())
                if prior is not None:
                    if prior.document_id != document_id or prior.job_type != "DOCUMENT_PROCESS":
                        raise ApplicationError(409, "IDEMPOTENCY_KEY_REUSED", "Idempotency conflict",
                                               "This key was already used for another operation.")
                    return UploadResult(document, prior, True, selected_role is not None)

            active = self.session.scalar(select(ProcessingJob).where(
                ProcessingJob.owner_id == self.owner_id,
                ProcessingJob.case_id == case_id,
                ProcessingJob.document_id == document_id,
                ProcessingJob.job_type == "DOCUMENT_PROCESS",
                ProcessingJob.status.in_(("QUEUED", "RUNNING", "RETRYABLE_FAILURE")),
            ).limit(1).with_for_update())
            if active is not None:
                raise ApplicationError(409, "DOCUMENT_PROCESSING_ACTIVE", "State conflict",
                                       "A processing run is already queued or running for this document.")

            run_number = (self.session.scalar(select(func.max(DocumentProcessingRun.run_number)).where(
                DocumentProcessingRun.owner_id == self.owner_id,
                DocumentProcessingRun.case_id == case_id,
                DocumentProcessingRun.document_id == document_id,
            )) or 0) + 1
            job_id, run_id = uuid4(), uuid4()
            job = ProcessingJob(
                job_id=job_id,
                case_id=case_id,
                owner_id=self.owner_id,
                document_id=document_id,
                job_type="DOCUMENT_PROCESS",
                stage="READ_AND_PARSE",
                status=JobStatus.QUEUED.value,
                attempt_count=0,
                max_attempts=3,
                idempotency_key=key,
                payload_json={"document_id": str(document_id),
                              "processing_run_id": str(run_id)},
            )
            self.session.add(job)
            self.session.flush()
            self.session.add(DocumentProcessingRun(
                processing_run_id=run_id,
                owner_id=self.owner_id,
                case_id=case_id,
                document_id=document_id,
                job_id=job_id,
                run_number=run_number,
                status="QUEUED",
                extraction_version="claimcheck-ingest-1",
                assigned_role=assigned_role,
                source_sha256=document.sha256,
                page_count=document.page_count,
            ))
            input_revision = append_input_revision(
                self.session,
                case,
                reason_code="DOCUMENT_REPROCESS_QUEUED",
                actor_type="OWNER",
                resource_type="DOCUMENT",
                resource_id=document_id,
                request_id=request_id,
            )
            document.state = DocumentStatus.QUEUED.value
            case.status = "PROCESSING"
            case.version += 1
            case.updated_at = utc_now()
            AuditRepository(self.session, self.owner_id).append(
                event_type="JOB_CREATED",
                resource_type="JOB",
                resource_id=job_id,
                case_id=case_id,
                request_id=request_id,
                event_data={"job_type": "DOCUMENT_PROCESS", "run_number": run_number,
                            "input_revision": input_revision},
            )
            AuditRepository(self.session, self.owner_id).append(
                event_type="CASE_INPUT_REVISION_CREATED",
                resource_type="CASE_INPUT_REVISION",
                resource_id=None,
                case_id=case_id,
                request_id=request_id,
                event_data={"input_revision": input_revision,
                            "reason_code": "DOCUMENT_REPROCESS_QUEUED"},
            )
            return UploadResult(document, job, False, selected_role is not None)

    def request_document_deletion(self, case_id: UUID, document_id: UUID, *,
                                  request_id: str) -> DocumentDeletionResult:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            document = DocumentRepository(self.session, self.owner_id).get(
                case_id, document_id, include_deleted=True, for_update=True
            )
            if case is None or document is None:
                raise _not_found()
            if document.state == "DELETED":
                job = self.session.scalar(select(ProcessingJob).where(
                    ProcessingJob.owner_id == self.owner_id,
                    ProcessingJob.case_id == case_id,
                    ProcessingJob.document_id == document_id,
                    ProcessingJob.job_type == "DOCUMENT_DELETE",
                ).order_by(ProcessingJob.created_at.desc()).limit(1))
                return DocumentDeletionResult(document, job, True)

            was_delete_pending = document.state == "DELETE_PENDING"
            deletion_key = f"document-delete:{document_id.hex}"
            job = self.session.scalar(select(ProcessingJob).where(
                ProcessingJob.owner_id == self.owner_id,
                ProcessingJob.case_id == case_id,
                ProcessingJob.idempotency_key == deletion_key,
            ).with_for_update())
            if job is None:
                job = ProcessingJob(
                    job_id=uuid4(),
                    case_id=case_id,
                    owner_id=self.owner_id,
                    document_id=document_id,
                    job_type="DOCUMENT_DELETE",
                    stage="DELETE_PRIVATE_OBJECTS",
                    status=JobStatus.QUEUED.value,
                    attempt_count=0,
                    max_attempts=10,
                    idempotency_key=deletion_key,
                    available_at=utc_now() + timedelta(seconds=30),
                    payload_json={"document_id": str(document_id)},
                )
                self.session.add(job)
                self.session.flush()
                AuditRepository(self.session, self.owner_id).append(
                    event_type="JOB_CREATED",
                    resource_type="JOB",
                    resource_id=job.job_id,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"job_type": "DOCUMENT_DELETE"},
                )
            elif job.status in {"FAILED", "CANCELLED"}:
                job.status = JobStatus.QUEUED.value
                job.stage = "DELETE_PRIVATE_OBJECTS"
                job.attempt_count = 0
                job.max_attempts = max(job.max_attempts, 10)
                job.available_at = utc_now() + timedelta(seconds=30)
                job.started_at = None
                job.finished_at = None
                job.error_code = None
                job.lease_owner = None
                job.lease_expires_at = None

            if not was_delete_pending:
                selected_role = _selected_role_for_document(
                    self.session, self.owner_id, case_id, document_id
                )
                input_revision = append_input_revision(
                    self.session,
                    case,
                    reason_code="DOCUMENT_DELETE_REQUESTED",
                    actor_type="OWNER",
                    resource_type="DOCUMENT",
                    resource_id=document_id,
                    request_id=request_id,
                )
                if selected_role is not None:
                    assignment = DocumentRoleAssignment(
                        assignment_id=uuid4(),
                        owner_id=self.owner_id,
                        case_id=case_id,
                        role=selected_role,
                        document_id=None,
                        input_revision=input_revision,
                        source_sha256=None,
                        assignment_source="SOURCE_DELETION_REQUESTED",
                        resolved_mismatch=False,
                        reason_code="SOURCE_DELETION_REQUESTED",
                    )
                    self.session.add(assignment)
                    self.session.flush()
                    AuditRepository(self.session, self.owner_id).append(
                        event_type="DOCUMENT_ROLE_ASSIGNED",
                        resource_type="DOCUMENT_ROLE_ASSIGNMENT",
                        resource_id=assignment.assignment_id,
                        case_id=case_id,
                        request_id=request_id,
                        event_data={"role": selected_role, "document_id": None,
                                    "input_revision": input_revision,
                                    "reason_code": "SOURCE_DELETION_REQUESTED"},
                    )
                AuditRepository(self.session, self.owner_id).append(
                    event_type="CASE_INPUT_REVISION_CREATED",
                    resource_type="CASE_INPUT_REVISION",
                    resource_id=None,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"input_revision": input_revision,
                                "reason_code": "DOCUMENT_DELETE_REQUESTED"},
                )
            document.state = DocumentStatus.DELETE_PENDING.value
            for process_job in JobRepository(self.session, self.owner_id).all_for_case(
                    case_id, include_finished=False, for_update=True):
                if process_job.document_id != document_id or \
                        process_job.job_type != "DOCUMENT_PROCESS":
                    continue
                process_job.status = JobStatus.CANCELLED.value
                process_job.finished_at = utc_now()
                process_job.error_code = "DOCUMENT_DELETE_REQUESTED"
                process_job.lease_owner = None
                process_job.lease_expires_at = None
                # Keep reserved artifact keys in the private job payload for cleanup.
                run_id = (process_job.payload_json or {}).get("processing_run_id")
                if run_id:
                    run = self.session.scalar(select(DocumentProcessingRun).where(
                        DocumentProcessingRun.owner_id == self.owner_id,
                        DocumentProcessingRun.case_id == case_id,
                        DocumentProcessingRun.document_id == document_id,
                        DocumentProcessingRun.processing_run_id == UUID(str(run_id)),
                    ).with_for_update())
                    if run is not None and run.status in {"QUEUED", "RUNNING"}:
                        run.status = "SUPERSEDED"
                        run.error_code = "DOCUMENT_DELETE_REQUESTED"
                        run.finished_at = utc_now()
            case.status = "PROCESSING"
            case.version += 1
            case.updated_at = utc_now()
            return DocumentDeletionResult(document, job, False)

    def get_jobs(self, case_id: UUID, *, limit: int, cursor: str | None,
                 job_type: str | None = None, status: str | None = None) -> dict:
        with self.session.begin():
            if CaseRepository(self.session, self.owner_id).get(case_id) is None:
                raise _not_found()
            try:
                page = ProcessingRepository(self.session, self.owner_id).jobs_for_case(
                    case_id, limit=limit, cursor=cursor, job_type=job_type, status=status
                )
            except InvalidPageCursor as exc:
                raise ApplicationError(400, "INVALID_CURSOR", "Invalid cursor",
                                       "The pagination cursor is invalid.") from exc
            return {"items": [job_view(job) for job in page.items],
                    "next_cursor": page.next_cursor}

    def get_processing_status(self, case_id: UUID) -> dict:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id)
            if case is None:
                raise _not_found()
            documents = DocumentRepository(self.session, self.owner_id).all_for_case(
                case_id, include_deleted=True
            )
            processing = ProcessingRepository(self.session, self.owner_id)
            jobs = JobRepository(self.session, self.owner_id).all_for_case(case_id)
            job_counts: dict[str, int] = {}
            for job in jobs:
                job_counts[job.status] = job_counts.get(job.status, 0) + 1
            document_status = []
            for document in documents:
                selected_role = _selected_role_for_document(
                    self.session, self.owner_id, case_id, document.document_id
                )
                if document.state == "DELETED":
                    document_status.append({"document_id": document.document_id,
                                            "assigned_role": document.role,
                                            "selected_for_role": False,
                                            "state": document.state,
                                            "latest_run": None})
                    continue
                latest = processing.latest_run(case_id, document.document_id)
                document_status.append({
                    "document_id": document.document_id,
                    "assigned_role": selected_role or document.role,
                    "selected_for_role": selected_role is not None,
                    "state": document.state,
                    "latest_run": processing_run_view(latest) if latest else None,
                })
            return {
                "case_id": case_id,
                "status": case.status,
                "document_count": len([item for item in documents if item.state != "DELETED"]),
                "documents": document_status,
                "job_counts": job_counts,
                "active_jobs": sum(job_counts.get(state, 0)
                                    for state in ("QUEUED", "RUNNING", "RETRYABLE_FAILURE")),
                "ready_for_analysis": case.status == "READY_FOR_ANALYSIS",
            }

    def get_document_fields(self, case_id: UUID, document_id: UUID, *,
                            processing_run_id: UUID | None = None) -> dict:
        with self.session.begin():
            document = DocumentRepository(self.session, self.owner_id).get(case_id, document_id)
            if document is None:
                raise _not_found()
            processing = ProcessingRepository(self.session, self.owner_id)
            run = (processing.get_run(case_id, document_id, processing_run_id)
                   if processing_run_id else processing.latest_run(case_id, document_id))
            if processing_run_id is not None and run is None:
                raise _not_found()
            if run is None:
                return {"document_id": document_id, "processing_run": None, "fields": []}
            fields = processing.fields(case_id, document_id, run.processing_run_id)
            return {
                "document_id": document_id,
                "processing_run": processing_run_view(run),
                "fields": [{
                    "field_path": item.field_path,
                    "value": item.value_json,
                    "state": item.state,
                    "evidence_id": item.evidence_id,
                    "reason_code": item.reason_code,
                    "provenance": item.provenance_json,
                } for item in fields],
            }

    def get_evidence(self, case_id: UUID, evidence_id: UUID) -> dict:
        with self.session.begin():
            if CaseRepository(self.session, self.owner_id).get(case_id) is None:
                raise _not_found()
            evidence = ProcessingRepository(self.session, self.owner_id).evidence_for_case(
                case_id, evidence_id
            )
            if evidence is None:
                raise _not_found()
            document = DocumentRepository(self.session, self.owner_id).get(
                case_id, evidence.document_id
            )
            if document is None:
                raise _not_found()
            artifact_key = evidence.quote_artifact_key
            expected_sha256 = evidence.quote_sha256
            payload = {
                "evidence_id": evidence.evidence_id,
                "document_id": evidence.document_id,
                "document_role": document.role,
                "processing_run_id": evidence.processing_run_id,
                "page_number": evidence.page_number,
                "source_sha256": evidence.source_sha256,
                "page_text_sha256": evidence.page_text_sha256,
                "char_start": evidence.char_start,
                "char_end": evidence.char_end,
                "reader": evidence.reader,
                "extraction_method": evidence.extraction_method,
                "verification_status": evidence.verification_status,
                "verification_method": evidence.verification_method,
                "verify_score": evidence.verify_score,
                "bbox": evidence.bbox_json,
                "bbox_is_measured": evidence.bbox_is_measured,
                "injection_flags": evidence.injection_flags_json,
                "reason_code": evidence.reason_code,
            }
        try:
            raw = self.storage.get(artifact_key)
        except (FileNotFoundError, InvalidStorageKey) as exc:
            raise ApplicationError(503, "EVIDENCE_ARTIFACT_UNAVAILABLE", "Evidence unavailable",
                                   "The evidence source could not be retrieved.", retryable=True) from exc
        except OSError as exc:
            raise ApplicationError(503, "EVIDENCE_ARTIFACT_UNAVAILABLE", "Evidence unavailable",
                                   "The evidence source could not be retrieved.", retryable=True) from exc
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ApplicationError(409, "EVIDENCE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                   "The stored evidence failed integrity verification.")
        try:
            quote_text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ApplicationError(409, "EVIDENCE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                   "The stored evidence failed integrity verification.") from exc
        return {**payload, "quoted_text": quote_text}

    def get_document_content(self, case_id: UUID, document_id: UUID) -> tuple[CaseDocument, bytes]:
        document = self.get_document(case_id, document_id)
        if document.storage_key is None:
            raise _not_found()
        try:
            content = self.storage.get(document.storage_key)
        except FileNotFoundError as exc:
            raise ApplicationError(404, "DOCUMENT_CONTENT_NOT_FOUND", "Not found",
                               "The requested document content was not found.") from exc
        except InvalidStorageKey as exc:
            raise ApplicationError(503, "STORAGE_UNAVAILABLE", "Service unavailable",
                               "Private document storage is temporarily unavailable.",
                               retryable=True) from exc
        except OSError as exc:
            raise ApplicationError(503, "STORAGE_UNAVAILABLE", "Service unavailable",
                               "Private document storage is temporarily unavailable.",
                               retryable=True) from exc
        if len(content) != document.byte_size or hashlib.sha256(content).hexdigest() != document.sha256:
            raise ApplicationError(409, "DOCUMENT_CONTENT_CORRUPT", "Document unavailable",
                                   "The stored document failed integrity verification.")
        return document, content

    def get_job(self, job_id: UUID, *, case_id: UUID | None = None) -> dict:
        with self.session.begin():
            job = JobRepository(self.session, self.owner_id).get(job_id, case_id=case_id)
            if job is None:
                raise _not_found()
            return job_view(job)

    def delete_case(self, case_id: UUID, *, request_id: str) -> DeletionResult:
        # First persist a tombstone and a durable deletion job. Reads stop here even if storage
        # later becomes unavailable; a repeated DELETE resumes the idempotent cleanup.
        with self.session.begin():
            case_repo = CaseRepository(self.session, self.owner_id)
            case = case_repo.get(case_id, include_deleted=True, for_update=True)
            if case is None:
                raise _not_found()
            job_repo = JobRepository(self.session, self.owner_id)
            deletion_job = job_repo.deletion_job(case_id)
            if case.deletion_status == "DELETED":
                return DeletionResult(case_id, deletion_job.job_id if deletion_job else None, "DELETED")
            if deletion_job is None:
                deletion_job = ProcessingJob(
                    job_id=uuid4(),
                    case_id=case_id,
                    owner_id=self.owner_id,
                    job_type="CASE_DELETION",
                    stage="DELETE_PRIVATE_OBJECTS",
                    status=JobStatus.RUNNING.value,
                    attempt_count=1,
                    max_attempts=10,
                    payload_json={},
                )
                self.session.add(deletion_job)
                self.session.flush()
                AuditRepository(self.session, self.owner_id).append(
                    event_type="JOB_CREATED",
                    resource_type="JOB",
                    resource_id=deletion_job.job_id,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"job_type": "CASE_DELETION"},
                )
            else:
                deletion_job.status = JobStatus.RUNNING.value
                deletion_job.stage = "DELETE_PRIVATE_OBJECTS"
                deletion_job.attempt_count += 1
                deletion_job.error_code = None
                deletion_job.available_at = utc_now()
                deletion_job.started_at = utc_now()
                deletion_job.finished_at = None
                deletion_job.lease_owner = None
                deletion_job.lease_expires_at = None
            was_delete_pending = case.deletion_status == "DELETE_PENDING"
            case.deletion_status = "DELETE_PENDING"
            case.updated_at = utc_now()
            case.version += 1
            if not was_delete_pending:
                input_revision = append_input_revision(
                    self.session,
                    case,
                    reason_code="DOCUMENT_DELETE_REQUESTED",
                    actor_type="OWNER",
                    resource_type="CASE",
                    resource_id=case_id,
                    request_id=request_id,
                )
                AuditRepository(self.session, self.owner_id).append(
                    event_type="CASE_INPUT_REVISION_CREATED",
                    resource_type="CASE_INPUT_REVISION",
                    resource_id=None,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"input_revision": input_revision,
                                "reason_code": "DOCUMENT_DELETE_REQUESTED"},
                )
            documents = DocumentRepository(self.session, self.owner_id).all_for_case(
                case_id, include_deleted=False
            )
            storage_keys = {document.storage_key for document in documents if document.storage_key}
            processing_repo = ProcessingRepository(self.session, self.owner_id)
            for document in documents:
                storage_keys.update(processing_repo.artifact_keys_for_document(
                    case_id, document.document_id
                ))
            deletion_job_id = deletion_job.job_id

        try:
            for key in storage_keys:
                self.storage.delete(key)
        except Exception as exc:
            with self.session.begin():
                job = JobRepository(self.session, self.owner_id).get(
                    deletion_job_id, case_id=case_id, include_deleted=True, for_update=True
                )
                if job is not None:
                    job.status = JobStatus.RETRYABLE_FAILURE.value
                    job.stage = "DELETE_PRIVATE_OBJECTS"
                    job.error_code = "STORAGE_DELETE_FAILED"
                    job.available_at = utc_now()
                    job.finished_at = utc_now()
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.updated_at = utc_now()
                    AuditRepository(self.session, self.owner_id).append(
                        event_type="JOB_FAILED",
                        resource_type="JOB",
                        resource_id=job.job_id,
                        case_id=case_id,
                        request_id=request_id,
                        result_code="STORAGE_DELETE_FAILED",
                        event_data={"retryable": True},
                    )
            raise ApplicationError(
                503, "STORAGE_DELETE_FAILED", "Deletion pending",
                "Private document cleanup could not be completed; repeat the delete request to retry.",
                retryable=True,
            ) from exc

        now = utc_now()
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(
                case_id, include_deleted=True, for_update=True
            )
            if case is None:
                raise _not_found()
            if case.deletion_status == "DELETED":
                return DeletionResult(case_id, deletion_job_id, "DELETED")
            docs = DocumentRepository(self.session, self.owner_id).all_for_case(
                case_id, include_deleted=True, for_update=True
            )
            audit = AuditRepository(self.session, self.owner_id)
            for document in docs:
                if document.state != DocumentStatus.DELETED.value:
                    document.state = DocumentStatus.DELETED.value
                    document.storage_key = None
                    document.original_filename = "deleted.pdf"
                    document.sha256 = "0" * 64
                    document.byte_size = 0
                    document.page_count = 0
                    document.idempotency_key = None
                    document.deleted_at = now
                    audit.append(
                        event_type="DOCUMENT_DELETED",
                        resource_type="DOCUMENT",
                        resource_id=document.document_id,
                        case_id=case_id,
                        request_id=request_id,
                        event_data={"content_removed": True},
                    )
            jobs = JobRepository(self.session, self.owner_id).all_for_case(
                case_id, include_finished=False, for_update=True
            )
            for job in jobs:
                if job.job_id == deletion_job_id:
                    continue
                job.status = JobStatus.CANCELLED.value
                job.finished_at = now
                job.error_code = "CASE_DELETED"
                job.payload_json = {}
                job.idempotency_key = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                audit.append(
                    event_type="JOB_CANCELLED",
                    resource_type="JOB",
                    resource_id=job.job_id,
                    case_id=case_id,
                    request_id=request_id,
                    result_code="CASE_DELETED",
                    event_data={"reason_code": "CASE_DELETED"},
                )
            deletion_job = JobRepository(self.session, self.owner_id).get(
                deletion_job_id, case_id=case_id, include_deleted=True, for_update=True
            )
            if deletion_job is not None:
                deletion_job.status = JobStatus.SUCCEEDED.value
                deletion_job.stage = "COMPLETE"
                deletion_job.finished_at = now
                deletion_job.error_code = None
                deletion_job.lease_owner = None
                deletion_job.lease_expires_at = None
                deletion_job.updated_at = now
                audit.append(
                    event_type="JOB_COMPLETED",
                    resource_type="JOB",
                    resource_id=deletion_job.job_id,
                    case_id=case_id,
                    request_id=request_id,
                    event_data={"job_type": "CASE_DELETION"},
                )
            analysis_run_count = AnalysisRunRepository(self.session, self.owner_id).delete_for_case(case_id)
            self.session.execute(text(
                "SELECT set_config('claimcheck.purge_document_artifacts', 'on', true)"
            ))
            processing_run_count = ProcessingRepository(
                self.session, self.owner_id
            ).purge_runs_for_case(case_id)
            case.status = "DELETED"
            case.deletion_status = "DELETED"
            case.display_name = None
            case.claim_date = None
            case.latest_analysis_run_id = None
            case.deleted_at = now
            case.updated_at = now
            case.version += 1
            audit.append(
                event_type="CASE_DELETED",
                resource_type="CASE",
                resource_id=case_id,
                case_id=case_id,
                request_id=request_id,
                event_data={"document_count": len(docs), "analysis_run_count": analysis_run_count,
                            "processing_run_count": processing_run_count},
            )
        return DeletionResult(case_id, deletion_job_id, "DELETED")


def _not_found() -> ApplicationError:
    return ApplicationError(404, "NOT_FOUND", "Not found", "The requested resource was not found.")
