"""Owner-scoped Phase 4 review queue, append-only corrections and role assignments."""
from __future__ import annotations

import hashlib
import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from claimcheck.application.errors import ApplicationError
from claimcheck.application.identity import OwnerIdentity
from claimcheck.application.input_revision import append_input_revision
from claimcheck.application.analysis_state import project_case_analysis_status
from claimcheck.evidence.spans import normalise_for_match, verify_quote
from claimcheck.ingest.money import read_line_money
from claimcheck.ingest.model import Storage
from claimcheck.persistence.models import (
    Case,
    CaseDocument,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    DocumentRoleAssignment,
    DocumentStatus,
    JobStatus,
    ProcessingJob,
    ReviewCorrection,
    StoredPageExtraction,
    utc_now,
)
from claimcheck.persistence.repositories import (
    AuditRepository,
    CaseRepository,
    DocumentRepository,
)
from claimcheck.persistence.storage import InvalidStorageKey
from claimcheck.application.review_readiness import (
    REQUIRED_ROLES,
    EffectiveValueResult,
    ReadinessReport,
    ReadinessState,
    candidate_field,
    evaluate_case_readiness,
    field_spec,
    latest_correction,
    latest_processing_run,
    latest_role_assignments,
    processing_record_for_field,
    resolve_effective_value,
    validate_field_value,
)

_ACTIVE_JOBS = ("QUEUED", "RUNNING", "RETRYABLE_FAILURE")
_ALLOWED_REASON_CODES = {
    "HUMAN_VISUAL_CHECK",
    "READER_CONFLICT_REVIEWED",
    "PARSER_GAP_REVIEWED",
    "SOURCE_AMBIGUITY",
    "OTHER_REVIEW_REASON",
}


class ReviewApplicationService:
    """Persistence/application adapter; never reads or writes the trusted core pipeline."""

    def __init__(self, session: Session, storage: Storage,
                 principal: OwnerIdentity) -> None:
        self.session = session
        self.storage = storage
        self.principal = principal
        self.owner_id = principal.owner_id

    def _case(self, case_id: UUID, *, for_update: bool = False) -> Case:
        case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=for_update)
        if case is None:
            raise _not_found()
        return case

    def _source_bytes(self, document: CaseDocument) -> bytes:
        if (document.state in {"DELETE_PENDING", "DELETED"} or not document.storage_key
                or document.sha256 == "0" * 64):
            raise ApplicationError(409, "DOCUMENT_UNAVAILABLE", "State conflict",
                                   "The selected source document is unavailable.")
        try:
            raw = self.storage.get(document.storage_key)
        except (FileNotFoundError, InvalidStorageKey, OSError) as exc:
            raise ApplicationError(503, "SOURCE_UNAVAILABLE", "Source unavailable",
                                   "The private source could not be verified.",
                                   retryable=True) from exc
        if len(raw) != document.byte_size or hashlib.sha256(raw).hexdigest() != document.sha256:
            raise ApplicationError(409, "SOURCE_HASH_MISMATCH", "Source unavailable",
                                   "The current source bytes do not match the persisted source hash.")
        return raw

    def _case_readiness(self, case: Case) -> ReadinessReport:
        report = evaluate_case_readiness(self.session, self.owner_id, case)
        active = self.session.scalar(select(func.count()).select_from(ProcessingJob).where(
            ProcessingJob.owner_id == self.owner_id,
            ProcessingJob.case_id == case.case_id,
            ProcessingJob.job_type.in_(("DOCUMENT_PROCESS", "DOCUMENT_DELETE")),
            ProcessingJob.status.in_(_ACTIVE_JOBS),
        )) or 0
        projected = ("PROCESSING" if active else project_case_analysis_status(
            self.session, self.owner_id, case, report.status.value
        ))
        if case.status != projected:
            case.status = projected
            case.version += 1
            case.updated_at = utc_now()
        return report

    def get_readiness(self, case_id: UUID) -> dict:
        with self.session.begin():
            case = self._case(case_id, for_update=True)
            return self._case_readiness(case).as_dict()

    def get_review_queue(self, case_id: UUID) -> dict:
        with self.session.begin():
            case = self._case(case_id, for_update=True)
            report = self._case_readiness(case)
            assignments = latest_role_assignments(self.session, self.owner_id, case_id)
            items: list[dict] = []
            for gap in report.gaps:
                if not gap.blocking or gap.code in {
                    "POLICY_SCHEDULE_PARSER_UNAVAILABLE",
                    "UNSELECTED_ROLE_CANDIDATES_REMAIN",
                    "ROLE_MISMATCH_EXPLICITLY_RESOLVED",
                }:
                    continue
                if gap.field_path:
                    assignment = assignments.get(gap.role or "")
                    document_id = assignment.document_id if assignment else gap.document_id
                    run = (latest_processing_run(self.session, self.owner_id, case_id, document_id)
                           if document_id else None)
                    candidate = (candidate_field(self.session, self.owner_id, case_id,
                                                 document_id, run.processing_run_id,
                                                 gap.field_path)
                                 if document_id and run else None)
                    correction = (latest_correction(self.session, self.owner_id, case_id,
                                                    document_id, run.processing_run_id,
                                                    gap.field_path)
                                 if document_id and run else None)
                    items.append({
                        "item_id": f"{gap.role}:{gap.field_path}",
                        "kind": "FIELD_REVIEW",
                        "code": gap.code,
                        "role": gap.role,
                        "document_id": document_id,
                        "processing_run_id": run.processing_run_id if run else None,
                        "field_path": gap.field_path,
                        "candidate_value": candidate.value_json if candidate else None,
                        "candidate_state": candidate.state if candidate else "MISSING",
                        "candidate_evidence_id": candidate.evidence_id if candidate else None,
                        "latest_correction_action": correction.action if correction else None,
                        "reason": candidate.reason_code if candidate else None,
                        "message": gap.message,
                    })
                else:
                    items.append({
                        "item_id": gap.code + (":" + gap.role if gap.role else ""),
                        "kind": "ROLE_OR_EVIDENCE_REVIEW",
                        "code": gap.code,
                        "role": gap.role,
                        "document_id": gap.document_id,
                        "processing_run_id": gap.processing_run_id,
                        "field_path": None,
                        "candidate_value": None,
                        "candidate_state": None,
                        "candidate_evidence_id": None,
                        "latest_correction_action": None,
                        "reason": None,
                        "message": gap.message,
                    })
            items.sort(key=lambda item: (
                item.get("role") or "", item.get("field_path") or "", item["code"]
            ))
            return {
                "case_id": case_id,
                "status": report.status.value,
                "input_revision": case.input_revision,
                "items": items,
                "warnings": [gap.as_dict() for gap in report.gaps if not gap.blocking],
            }

    def append_correction(
        self,
        case_id: UUID,
        *,
        document_id: UUID,
        processing_run_id: UUID,
        field_path: str,
        action: str,
        corrected_value,
        evidence_id: UUID | None,
        verification_method: str,
        page_number: int | None,
        reason_code: str | None,
        request_id: str,
    ) -> dict:
        action = action.upper()
        verification_method = verification_method.upper()
        if action not in {"CONFIRM", "CORRECT", "UNRESOLVED"}:
            raise ApplicationError(422, "INVALID_REVIEW_ACTION", "Invalid review action",
                                   "Choose CONFIRM, CORRECT, or UNRESOLVED.")
        spec = field_spec(field_path)
        if spec is None:
            raise ApplicationError(422, "UNSUPPORTED_REVIEW_FIELD", "Unsupported field",
                                   "This field is outside the explicit Phase 4 review allow-list.")
        if reason_code is not None and reason_code not in _ALLOWED_REASON_CODES:
            raise ApplicationError(422, "INVALID_REVIEW_REASON", "Invalid review reason",
                                   "Choose an allow-listed reason code.")

        with self.session.begin():
            case = self._case(case_id, for_update=True)
            document = DocumentRepository(self.session, self.owner_id).get(
                case_id, document_id, for_update=True
            )
            if document is None:
                raise _not_found()
            assignments = latest_role_assignments(self.session, self.owner_id, case_id)
            matching_roles = [role for role in spec.roles
                              if role in assignments
                              and assignments[role].document_id == document_id]
            if not matching_roles:
                raise ApplicationError(409, "ROLE_SOURCE_NOT_SELECTED", "State conflict",
                                       "The document is not the explicitly selected source for this field.")
            if document.state in {"DELETE_PENDING", "DELETED"}:
                raise ApplicationError(409, "DOCUMENT_UNAVAILABLE", "State conflict",
                                       "Review is unavailable for a deleted or deleting document.")
            run = self.session.scalar(select(DocumentProcessingRun).where(
                DocumentProcessingRun.owner_id == self.owner_id,
                DocumentProcessingRun.case_id == case_id,
                DocumentProcessingRun.document_id == document_id,
                DocumentProcessingRun.processing_run_id == processing_run_id,
            ).with_for_update())
            latest_run = latest_processing_run(self.session, self.owner_id, case_id, document_id)
            if run is None:
                raise _not_found()
            if latest_run is None or latest_run.processing_run_id != processing_run_id:
                raise ApplicationError(409, "STALE_PROCESSING_CANDIDATE", "State conflict",
                                       "Corrections must target the latest processing run.")
            if run.assigned_role not in spec.roles:
                raise ApplicationError(409, "FIELD_ROLE_MISMATCH", "State conflict",
                                       "The selected run does not belong to this field's source role.")
            if run.status in {"QUEUED", "RUNNING", "FAILED", "SUPERSEDED"}:
                raise ApplicationError(409, "PROCESSING_CANDIDATE_UNAVAILABLE", "State conflict",
                                       "The processing candidate is not available for review.")
            raw_source = self._source_bytes(document)
            source_sha256 = hashlib.sha256(raw_source).hexdigest()
            if run.source_sha256 != source_sha256:
                raise ApplicationError(409, "SOURCE_HASH_MISMATCH", "Source unavailable",
                                       "The processing run does not match the current source bytes.")

            candidate = candidate_field(self.session, self.owner_id, case_id, document_id,
                                        processing_run_id, field_path)
            record = self._record_for_field(case_id, document_id, processing_run_id, field_path)
            before_value = candidate.value_json if candidate is not None else None
            before_state = candidate.state if candidate is not None else "MISSING"
            evidence = None
            quote_text = None
            if action == "UNRESOLVED":
                if (corrected_value is not None or evidence_id is not None
                        or page_number is not None):
                    raise ApplicationError(422, "INVALID_UNRESOLVED_REVIEW", "Invalid review entry",
                                           "An unresolved entry cannot contain a value or evidence claim.")
                verification_method = "UNRESOLVED"
                stored_evidence_id = None
                stored_page_number = None
                stored_value = None
            else:
                if action == "CONFIRM":
                    if candidate is None or candidate.value_json is None or corrected_value is not None:
                        raise ApplicationError(422, "CANDIDATE_CANNOT_BE_CONFIRMED",
                                               "Candidate unavailable",
                                               "CONFIRM requires an existing candidate value and no replacement value.")
                    try:
                        normalized_value = validate_field_value(field_path, candidate.value_json)
                    except ValueError as exc:
                        raise ApplicationError(422, "CANDIDATE_VALUE_INVALID",
                                               "Candidate unavailable",
                                               "The candidate is not a supported typed value.") from exc
                else:
                    if corrected_value is None:
                        raise ApplicationError(422, "CORRECTION_VALUE_REQUIRED",
                                               "Correction value required",
                                               "CORRECT requires an explicit typed value.")
                    try:
                        normalized_value = validate_field_value(field_path, corrected_value)
                    except (TypeError, ValueError) as exc:
                        raise ApplicationError(422, "CORRECTION_VALUE_INVALID",
                                               "Invalid correction value",
                                               "The value does not match the field's exact type and range.") from exc

                if verification_method == "EVIDENCE_SPAN":
                    if evidence_id is None or page_number is not None:
                        raise ApplicationError(422, "EVIDENCE_REFERENCE_REQUIRED",
                                               "Evidence required",
                                               "EVIDENCE_SPAN requires one scoped evidence ID and no claimed page number.")
                    allowed_ids = self._expected_evidence_ids(
                        candidate, record, field_path
                    )
                    if evidence_id not in allowed_ids:
                        raise ApplicationError(422, "EVIDENCE_NOT_TIED_TO_FIELD",
                                               "Evidence unavailable",
                                               "The evidence span is not linked to this candidate field or record.")
                    evidence, quote_text = self._verified_evidence(
                        document, run, evidence_id
                    )
                    if not self._quote_supports(field_path, normalized_value, quote_text):
                        raise ApplicationError(422, "EVIDENCE_DOES_NOT_SUPPORT_VALUE",
                                               "Evidence does not support value",
                                               "Use a different verified span or record an explicit human visual check.")
                    stored_evidence_id = evidence_id
                    stored_page_number = None
                elif verification_method == "HUMAN_VISUAL":
                    if evidence_id is not None or page_number is None:
                        raise ApplicationError(422, "VISUAL_PAGE_REQUIRED",
                                               "Visual confirmation required",
                                               "HUMAN_VISUAL requires a page number and no text-span evidence ID.")
                    if not 1 <= page_number <= document.page_count:
                        raise ApplicationError(422, "INVALID_SOURCE_PAGE", "Invalid source page",
                                               "The visual-review page is outside the current document.")
                    stored_evidence_id = None
                    stored_page_number = page_number
                else:
                    raise ApplicationError(422, "INVALID_VERIFICATION_METHOD",
                                           "Invalid verification method",
                                           "Use EVIDENCE_SPAN or HUMAN_VISUAL.")
                if spec.kind in {"category", "head", "bool"} and \
                        verification_method != "HUMAN_VISUAL":
                    raise ApplicationError(422, "HUMAN_REVIEW_REQUIRED",
                                           "Human review required",
                                           "This classification requires an explicit human visual review event.")
                if action == "CONFIRM" and verification_method == "EVIDENCE_SPAN":
                    if candidate is None or candidate.evidence_id != stored_evidence_id:
                        raise ApplicationError(422, "EVIDENCE_NOT_TIED_TO_CANDIDATE",
                                               "Evidence unavailable",
                                               "A candidate can only be confirmed against its own verified span.")
                stored_value = normalized_value if action == "CORRECT" else None

            prior_correction = latest_correction(
                self.session, self.owner_id, case_id, document_id,
                processing_run_id, field_path,
            )
            correction_number = (prior_correction.correction_number if prior_correction else 0) + 1
            revision = append_input_revision(
                self.session,
                case,
                reason_code="REVIEW_CORRECTION_APPENDED",
                actor_type="OWNER",
                resource_type="DOCUMENT",
                resource_id=document_id,
                request_id=request_id,
            )
            correction = ReviewCorrection(
                correction_id=uuid4(),
                owner_id=self.owner_id,
                case_id=case_id,
                document_id=document_id,
                processing_run_id=processing_run_id,
                field_path=field_path,
                correction_number=correction_number,
                action=action,
                prior_state=before_state,
                prior_value_json=before_value,
                candidate_field_id=candidate.processing_field_id if candidate else None,
                candidate_record_id=record.processing_record_id if record else None,
                corrected_value_json=stored_value,
                evidence_id=stored_evidence_id,
                verification_method=verification_method,
                page_number=stored_page_number,
                source_sha256=source_sha256,
                input_revision=revision,
                reason_code=reason_code,
            )
            self.session.add(correction)
            self.session.flush()
            AuditRepository(self.session, self.owner_id).append(
                event_type="REVIEW_CORRECTION_APPENDED",
                resource_type="REVIEW_CORRECTION",
                resource_id=correction.correction_id,
                case_id=case_id,
                request_id=request_id,
                event_data={
                    "field_path": field_path,
                    "action": action,
                    "correction_number": correction_number,
                    "verification_method": verification_method,
                    "input_revision": revision,
                },
            )
            case.version += 1
            report = self._case_readiness(case)
            return {
                "correction": self._correction_view(correction),
                "readiness": report.as_dict(),
            }

    def assign_role(self, case_id: UUID, *, role: str, document_id: UUID | None,
                    confirm_mismatch: bool, reason_code: str | None,
                    request_id: str) -> dict:
        if role not in REQUIRED_ROLES:
            raise ApplicationError(422, "INVALID_DOCUMENT_ROLE", "Invalid document role",
                                   "Choose one of the four required canonical document roles.")
        if reason_code is not None and reason_code not in _ALLOWED_REASON_CODES:
            raise ApplicationError(422, "INVALID_ROLE_REASON", "Invalid role reason",
                                   "Choose an allow-listed reason code.")
        with self.session.begin():
            case = self._case(case_id, for_update=True)
            current = latest_role_assignments(self.session, self.owner_id, case_id)
            previous = current.get(role)
            document = None
            if document_id is not None:
                document = DocumentRepository(self.session, self.owner_id).get(
                    case_id, document_id, for_update=True
                )
                if document is None:
                    raise _not_found()
                self._source_bytes(document)
                for other_role, other_assignment in current.items():
                    if other_role != role and other_assignment.document_id == document_id:
                        raise ApplicationError(409, "DOCUMENT_ALREADY_ASSIGNED",
                                               "Role assignment conflict",
                                               "A source selected for another role must first be explicitly unassigned.")
                if document.state in {"DELETE_PENDING", "DELETED"}:
                    raise ApplicationError(409, "DOCUMENT_UNAVAILABLE", "State conflict",
                                           "A deleting or deleted document cannot be assigned.")

            same_selection = previous is not None and previous.document_id == document_id
            same_resolution = previous is not None and \
                previous.resolved_mismatch == bool(confirm_mismatch)
            if same_selection and same_resolution:
                return {
                    "assignment": self._assignment_view(previous),
                    "job": None,
                    "readiness": self._case_readiness(case).as_dict(),
                }

            if document is not None:
                active = self.session.scalar(select(ProcessingJob).where(
                    ProcessingJob.owner_id == self.owner_id,
                    ProcessingJob.case_id == case_id,
                    ProcessingJob.document_id == document_id,
                    ProcessingJob.job_type == "DOCUMENT_PROCESS",
                    ProcessingJob.status.in_(_ACTIVE_JOBS),
                ).with_for_update())
                if active is not None:
                    active_run_id = (active.payload_json or {}).get("processing_run_id")
                    active_run = None
                    if active_run_id:
                        try:
                            active_run = self.session.get(
                                DocumentProcessingRun, UUID(str(active_run_id))
                            )
                        except ValueError:
                            active_run = None
                    if active_run is None or active_run.assigned_role != role:
                        raise ApplicationError(409, "DOCUMENT_PROCESSING_ACTIVE",
                                               "State conflict",
                                               "Wait for the current processing run before changing this source role.")

            revision = append_input_revision(
                self.session,
                case,
                reason_code="DOCUMENT_ROLE_ASSIGNED",
                actor_type="OWNER",
                resource_type="DOCUMENT" if document_id else "CASE",
                resource_id=document_id or case_id,
                request_id=request_id,
            )
            assignment = DocumentRoleAssignment(
                assignment_id=uuid4(),
                owner_id=self.owner_id,
                case_id=case_id,
                role=role,
                document_id=document_id,
                input_revision=revision,
                source_sha256=document.sha256 if document is not None else None,
                assignment_source="REVIEWER_SELECTION",
                resolved_mismatch=bool(confirm_mismatch) if document is not None else False,
                reason_code=reason_code,
            )
            self.session.add(assignment)
            self.session.flush()

            queued_job = None
            if document is not None:
                document.role = role  # mutable projection only; the assignment/run history is authoritative.
                latest = latest_processing_run(self.session, self.owner_id, case_id, document_id)
                needs_run = latest is None or latest.assigned_role != role or \
                    latest.source_sha256 != document.sha256 or \
                    latest.status in {"FAILED", "SUPERSEDED"}
                if needs_run and active is None:
                    queued_job = self._queue_role_run(case, document, role, revision, request_id)

            AuditRepository(self.session, self.owner_id).append(
                event_type="DOCUMENT_ROLE_ASSIGNED",
                resource_type="DOCUMENT_ROLE_ASSIGNMENT",
                resource_id=assignment.assignment_id,
                case_id=case_id,
                request_id=request_id,
                event_data={
                    "role": role,
                    "document_id": str(document_id) if document_id else None,
                    "input_revision": revision,
                    "resolved_mismatch": bool(confirm_mismatch),
                },
            )
            case.version += 1
            if queued_job is not None:
                case.status = "PROCESSING"
            report = self._case_readiness(case)
            return {
                "assignment": self._assignment_view(assignment),
                "job": self._job_view(queued_job) if queued_job else None,
                "readiness": report.as_dict(),
            }

    def _queue_role_run(self, case: Case, document: CaseDocument, role: str,
                        revision: int, request_id: str) -> ProcessingJob:
        run_number = (self.session.scalar(select(func.max(DocumentProcessingRun.run_number)).where(
            DocumentProcessingRun.owner_id == self.owner_id,
            DocumentProcessingRun.case_id == case.case_id,
            DocumentProcessingRun.document_id == document.document_id,
        )) or 0) + 1
        run_id, job_id = uuid4(), uuid4()
        job = ProcessingJob(
            job_id=job_id,
            case_id=case.case_id,
            owner_id=self.owner_id,
            document_id=document.document_id,
            job_type="DOCUMENT_PROCESS",
            stage="READ_AND_PARSE",
            status=JobStatus.QUEUED.value,
            attempt_count=0,
            max_attempts=3,
            idempotency_key=f"role-run:{document.document_id.hex}:{role}:{revision}",
            payload_json={"document_id": str(document.document_id),
                          "processing_run_id": str(run_id), "input_revision": revision},
        )
        self.session.add(job)
        self.session.flush()
        self.session.add(DocumentProcessingRun(
            processing_run_id=run_id,
            owner_id=self.owner_id,
            case_id=case.case_id,
            document_id=document.document_id,
            job_id=job_id,
            run_number=run_number,
            status="QUEUED",
            extraction_version="claimcheck-ingest-1",
            assigned_role=role,
            source_sha256=document.sha256,
            page_count=document.page_count,
        ))
        document.state = DocumentStatus.QUEUED.value
        AuditRepository(self.session, self.owner_id).append(
            event_type="JOB_CREATED",
            resource_type="JOB",
            resource_id=job_id,
            case_id=case.case_id,
            request_id=request_id,
            event_data={"job_type": "DOCUMENT_PROCESS", "run_number": run_number,
                        "input_revision": revision},
        )
        return job

    def _record_for_field(self, case_id: UUID, document_id: UUID, run_id: UUID,
                          field_path: str) -> DocumentProcessingRecord | None:
        return processing_record_for_field(
            self.session, self.owner_id, case_id, document_id, run_id, field_path
        )

    def _expected_evidence_ids(self, candidate: DocumentProcessingField | None,
                               record: DocumentProcessingRecord | None,
                               field_path: str) -> set[UUID]:
        expected: set[UUID] = set()
        if candidate is not None and candidate.evidence_id is not None:
            expected.add(candidate.evidence_id)
        if record is not None and record.evidence_id is not None:
            expected.add(record.evidence_id)
        return expected

    def _verified_evidence(self, document: CaseDocument, run: DocumentProcessingRun,
                           evidence_id: UUID) -> tuple[DocumentEvidenceSpan, str]:
        evidence = self.session.scalar(select(DocumentEvidenceSpan).where(
            DocumentEvidenceSpan.owner_id == self.owner_id,
            DocumentEvidenceSpan.case_id == document.case_id,
            DocumentEvidenceSpan.document_id == document.document_id,
            DocumentEvidenceSpan.processing_run_id == run.processing_run_id,
            DocumentEvidenceSpan.evidence_id == evidence_id,
        ))
        if (evidence is None or evidence.verification_status != "VERIFIED"
                or evidence.source_sha256 != document.sha256
                or evidence.source_sha256 != run.source_sha256):
            raise ApplicationError(422, "EVIDENCE_SCOPE_OR_VERIFICATION_INVALID",
                                   "Evidence unavailable",
                                   "The evidence span is not verified for the current source and run.")
        page = self.session.scalar(select(StoredPageExtraction).where(
            StoredPageExtraction.owner_id == self.owner_id,
            StoredPageExtraction.case_id == document.case_id,
            StoredPageExtraction.document_id == document.document_id,
            StoredPageExtraction.processing_run_id == run.processing_run_id,
            StoredPageExtraction.page_number == evidence.page_number,
        ))
        if page is None:
            raise ApplicationError(422, "EVIDENCE_PAGE_UNAVAILABLE", "Evidence unavailable",
                                   "The evidence page is not available in the selected processing run.")
        if evidence.reader == "layout":
            artifact_key, expected_page_sha, page_state = (
                page.layout_text_artifact_key, page.layout_text_sha256, page.layout_state
            )
        elif evidence.reader == "native":
            artifact_key, expected_page_sha, page_state = (
                page.native_text_artifact_key, page.native_text_sha256, page.native_state
            )
        else:
            artifact_key, expected_page_sha, page_state = None, None, "MISSING"
        if not artifact_key or not expected_page_sha or page_state != "READ":
            raise ApplicationError(422, "EVIDENCE_PAGE_UNAVAILABLE", "Evidence unavailable",
                                   "The verified page text is not available for revalidation.")
        try:
            quote_bytes = self.storage.get(evidence.quote_artifact_key)
            page_bytes = self.storage.get(artifact_key)
        except (FileNotFoundError, InvalidStorageKey, OSError) as exc:
            raise ApplicationError(503, "EVIDENCE_ARTIFACT_UNAVAILABLE", "Evidence unavailable",
                                   "The verified evidence artifact could not be retrieved.",
                                   retryable=True) from exc
        if (hashlib.sha256(quote_bytes).hexdigest() != evidence.quote_sha256
                or hashlib.sha256(page_bytes).hexdigest() != expected_page_sha
                or expected_page_sha != evidence.page_text_sha256):
            raise ApplicationError(409, "EVIDENCE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                   "The evidence artifacts failed integrity verification.")
        try:
            quote = quote_bytes.decode("utf-8", errors="strict")
            page_text = page_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ApplicationError(409, "EVIDENCE_ARTIFACT_CORRUPT", "Evidence unavailable",
                                   "The evidence artifacts failed integrity verification.") from exc
        outcome = verify_quote(quote, page_text)
        if (not outcome.accepted or outcome.char_start != evidence.char_start
                or outcome.char_end != evidence.char_end
                or outcome.method.value.upper() != evidence.verification_method.upper()):
            raise ApplicationError(409, "EVIDENCE_SPAN_REVALIDATION_FAILED",
                                   "Evidence unavailable",
                                   "The stored verified span no longer matches its page text.")
        return evidence, quote

    @staticmethod
    def _quote_supports(field_path: str, value, quote: str) -> bool:
        spec = field_spec(field_path)
        if spec is None:
            return False
        if spec.kind == "money":
            reading = read_line_money(quote)
            return reading.amount_paise is not None and reading.amount_paise == value
        if spec.kind == "percent":
            return bool(re.search(rf"(?<!\d){re.escape(str(value))}\s*%", quote))
        if spec.kind == "text":
            return normalise_for_match(str(value)) in normalise_for_match(quote)
        return False

    @staticmethod
    def _assignment_view(row: DocumentRoleAssignment) -> dict:
        return {
            "assignment_id": row.assignment_id,
            "role": row.role,
            "document_id": row.document_id,
            "input_revision": row.input_revision,
            "source_sha256": row.source_sha256,
            "assignment_source": row.assignment_source,
            "resolved_mismatch": row.resolved_mismatch,
            "reason_code": row.reason_code,
            "created_at": row.created_at,
        }

    @staticmethod
    def _correction_view(row: ReviewCorrection) -> dict:
        return {
            "correction_id": row.correction_id,
            "document_id": row.document_id,
            "processing_run_id": row.processing_run_id,
            "field_path": row.field_path,
            "correction_number": row.correction_number,
            "action": row.action,
            "prior_state": row.prior_state,
            "prior_value": row.prior_value_json,
            "corrected_value": row.corrected_value_json,
            "evidence_id": row.evidence_id,
            "verification_method": row.verification_method,
            "page_number": row.page_number,
            "source_sha256": row.source_sha256,
            "input_revision": row.input_revision,
            "reason_code": row.reason_code,
            "created_at": row.created_at,
        }

    @staticmethod
    def _job_view(job: ProcessingJob | None) -> dict | None:
        if job is None:
            return None
        return {
            "job_id": job.job_id,
            "document_id": job.document_id,
            "job_type": job.job_type,
            "status": job.status,
            "stage": job.stage,
        }


def _not_found() -> ApplicationError:
    return ApplicationError(404, "NOT_FOUND", "Not found", "The requested resource was not found.")
