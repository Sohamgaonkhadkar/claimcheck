"""Owner-scoped case/document/job HTTP resources for the Phase 2 persistence boundary."""
from __future__ import annotations

import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from claimcheck.api.dependencies import get_owner_principal, get_session
from claimcheck.api.errors import request_id_for
from claimcheck.application.errors import ApplicationError
from claimcheck.api.identity import OwnerPrincipal
from claimcheck.api.schemas import (
    CaseCreateRequest,
    CaseDeletionResponse,
    CaseListResponse,
    CasePatchRequest,
    CaseStatus,
    CaseSummary,
    DocumentListResponse,
    DocumentStatus,
    DocumentSummary,
    DocumentDetail,
    DocumentFieldsResponse,
    DocumentDeleteResponse,
    EvidenceSpanView,
    JobListResponse,
    JobState,
    ProcessingStatusResponse,
    DocumentUploadResponse,
    JobStatusResponse,
    ReviewReadinessResponse,
    ReviewQueueResponse,
    ReviewCorrectionRequest,
    ReviewCorrectionResponse,
    DocumentRoleAssignmentRequest,
    DocumentRoleAssignmentResponse,
    AnalysisRunResponse,
    AnalysisRunListResponse,
)
from claimcheck.application.backend import CaseApplicationService, document_view, job_view
from claimcheck.application.review import ReviewApplicationService
from claimcheck.application.analysis import AnalysisApplicationService
from claimcheck.application.upload_validation import (
    UploadRejected,
    normalize_document_role,
    validate_pdf_upload,
)

router = APIRouter(prefix="/api/v1", tags=["cases", "documents", "jobs"])


def _service(request: Request, session, owner: OwnerPrincipal) -> CaseApplicationService:
    return CaseApplicationService(
        session,
        request.app.state.storage,
        owner,
    )


def _review_service(request: Request, session, owner: OwnerPrincipal) -> ReviewApplicationService:
    return ReviewApplicationService(session, request.app.state.storage, owner)


def _analysis_service(request: Request, session,
                      owner: OwnerPrincipal) -> AnalysisApplicationService:
    return AnalysisApplicationService(session, request.app.state.storage, owner)


def _audit_rejection(service: CaseApplicationService, case_id: UUID,
                     code: str, request_id: str) -> None:
    service.record_document_rejection(case_id, reason_code=code, request_id=request_id)


def _is_uploaded_file(value: object) -> bool:
    return (not isinstance(value, str)
            and callable(getattr(value, "read", None))
            and callable(getattr(value, "close", None))
            and hasattr(value, "filename")
            and hasattr(value, "content_type"))


@router.post("/cases", response_model=CaseSummary, status_code=201, summary="Create a case")
def create_case(
    body: CaseCreateRequest,
    request: Request,
    response: Response,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).create_case(
        display_name=body.display_name,
        claim_date=body.claim_date,
        request_id=request_id,
    )
    payload["request_id"] = request_id
    response.headers["Location"] = f"/api/v1/cases/{payload['case_id']}"
    return payload


@router.get("/cases", response_model=CaseListResponse, summary="List owned cases")
def list_cases(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    status: CaseStatus | None = None,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).list_cases(
        limit=limit, cursor=cursor, status=status
    )
    for item in payload["items"]:
        item["request_id"] = request_id
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}", response_model=CaseSummary, summary="Get an owned case")
def get_case(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_case(case_id)
    payload["request_id"] = request_id
    return payload


@router.patch("/cases/{case_id}", response_model=CaseSummary, summary="Update case metadata")
def patch_case(
    case_id: UUID,
    body: CasePatchRequest,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    request_id = request_id_for(request)
    payload = _service(request, session, owner).update_case(
        case_id, fields=fields, request_id=request_id
    )
    payload["request_id"] = request_id
    return payload


@router.delete("/cases/{case_id}", response_model=CaseDeletionResponse,
               summary="Delete a case and private source files")
def delete_case(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    result = _service(request, session, owner).delete_case(case_id, request_id=request_id)
    return {
        "case_id": result.case_id,
        "deletion_job_id": result.deletion_job_id,
        "status": result.status,
        "request_id": request_id,
    }


@router.get("/cases/{case_id}/documents", response_model=DocumentListResponse,
            summary="List case documents")
def list_documents(
    case_id: UUID,
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    role: str | None = None,
    state: DocumentStatus | None = None,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    service = _service(request, session, owner)
    normalized_role = None
    if role is not None:
        try:
            normalized_role = normalize_document_role(role)
        except UploadRejected as exc:
            raise ApplicationError(exc.status, exc.code, exc.title, exc.detail) from exc
    request_id = request_id_for(request)
    payload = service.list_documents(
        case_id,
        limit=limit,
        cursor=cursor,
        role=normalized_role,
        state=state,
    )
    for item in payload["items"]:
        item["request_id"] = request_id
    payload["request_id"] = request_id
    return payload


@router.post(
    "/cases/{case_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
    summary="Validate and queue a PDF",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "role": {"type": "string", "enum": [
                                "POLICY", "POLICY_SCHEDULE", "HOSPITAL_BILL", "SETTLEMENT_LETTER",
                                "policy_wording", "policy_schedule", "bill", "settlement",
                            ]},
                        },
                        "required": ["file", "role"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    },
)
async def upload_document(
    case_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=128),
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    service = _service(request, session, owner)
    request_id = request_id_for(request)

    # Check owner/case before parsing the multipart body or touching private storage.
    service.ensure_case_access(case_id)

    if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        _audit_rejection(service, case_id, "INVALID_IDEMPOTENCY_KEY", request_id)
        raise ApplicationError(400, "INVALID_IDEMPOTENCY_KEY", "Invalid request",
                           "The idempotency key contains unsupported characters.")

    content_length = request.headers.get("content-length")
    if content_length is None:
        _audit_rejection(service, case_id, "CONTENT_LENGTH_REQUIRED", request_id)
        raise ApplicationError(411, "CONTENT_LENGTH_REQUIRED", "Content length required",
                           "Provide a bounded Content-Length for this upload.")
    try:
        request_size = int(content_length)
        if request_size < 0:
            raise ValueError
    except ValueError as exc:
        _audit_rejection(service, case_id, "INVALID_CONTENT_LENGTH", request_id)
        raise ApplicationError(400, "INVALID_CONTENT_LENGTH", "Invalid request",
                           "The upload request length is invalid.") from exc
    if request_size > request.app.state.settings.max_request_bytes:
        _audit_rejection(service, case_id, "UPLOAD_TOO_LARGE", request_id)
        raise ApplicationError(413, "UPLOAD_TOO_LARGE", "Upload too large",
                           "The request exceeds the configured upload-size limit.")

    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data;"):
        _audit_rejection(service, case_id, "INVALID_MULTIPART", request_id)
        raise ApplicationError(400, "INVALID_MULTIPART", "Invalid upload request",
                           "Upload a PDF using multipart form data.")

    try:
        form = await request.form(max_files=1, max_fields=4)
    except Exception as exc:
        _audit_rejection(service, case_id, "INVALID_MULTIPART", request_id)
        raise ApplicationError(400, "INVALID_MULTIPART", "Invalid upload request",
                           "The multipart upload could not be processed.") from exc

    form_items = list(form.multi_items())
    if len(form_items) != 2 or {key for key, _value in form_items} != {"file", "role"}:
        for _key, value in form_items:
            if _is_uploaded_file(value):
                await value.close()
        _audit_rejection(service, case_id, "INVALID_UPLOAD_FIELDS", request_id)
        raise ApplicationError(422, "INVALID_UPLOAD_FIELDS", "Invalid upload request",
                           "Provide exactly one file and one document role.")

    upload = form.get("file")
    role_value = form.get("role")
    if not _is_uploaded_file(upload) or not isinstance(role_value, str):
        if _is_uploaded_file(upload):
            await upload.close()
        _audit_rejection(service, case_id, "INVALID_UPLOAD_FIELDS", request_id)
        raise ApplicationError(422, "INVALID_UPLOAD_FIELDS", "Invalid upload request",
                           "Provide exactly one PDF file and one document role.")

    try:
        role = normalize_document_role(role_value)
    except UploadRejected as exc:
        await upload.close()
        _audit_rejection(service, case_id, exc.code, request_id)
        raise ApplicationError(exc.status, exc.code, exc.title, exc.detail) from exc

    try:
        data = await upload.read(request.app.state.settings.max_upload_bytes + 1)
        validated = validate_pdf_upload(
            filename=upload.filename,
            content_type=upload.content_type,
            data=data,
            max_bytes=request.app.state.settings.max_upload_bytes,
            max_pages=request.app.state.settings.max_pdf_pages,
        )
    except UploadRejected as exc:
        _audit_rejection(service, case_id, exc.code, request_id)
        raise ApplicationError(exc.status, exc.code, exc.title, exc.detail) from exc
    except Exception as exc:
        _audit_rejection(service, case_id, "UPLOAD_READ_FAILED", request_id)
        raise ApplicationError(400, "UPLOAD_READ_FAILED", "Invalid upload request",
                           "The uploaded file could not be read.") from exc
    finally:
        await upload.close()

    result = service.upload_document(
        case_id,
        role=role,
        pdf=validated,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )
    document_payload = _document_view_for_response(
        result.document, request_id, selected_for_role=result.selected_for_role
    )
    job_payload = _job_view_for_response(result.job, request_id)
    return {
        "document": document_payload,
        "job": job_payload,
        "request_id": request_id,
    }


def _document_view_for_response(document, request_id: str, *,
                                selected_for_role: bool = False) -> dict:
    result = document_view(document, selected_for_role=selected_for_role)
    result["request_id"] = request_id
    return result


def _job_view_for_response(job, request_id: str) -> dict:
    result = job_view(job)
    result["request_id"] = request_id
    return result


@router.get("/cases/{case_id}/documents/{document_id}", response_model=DocumentDetail,
            summary="Get document metadata and processing runs")
def get_document(
    case_id: UUID,
    document_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_document_detail(case_id, document_id)
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/readiness", response_model=ReviewReadinessResponse,
            summary="Get trust-gated review readiness")
def get_review_readiness(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _review_service(request, session, owner).get_readiness(case_id)
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/review-queue", response_model=ReviewQueueResponse,
            summary="Get owner-scoped human-review items")
def get_review_queue(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _review_service(request, session, owner).get_review_queue(case_id)
    payload["request_id"] = request_id
    return payload


@router.post("/cases/{case_id}/review-corrections", response_model=ReviewCorrectionResponse,
             status_code=201, summary="Append a provenance-checked human review event")
def append_review_correction(
    case_id: UUID,
    body: ReviewCorrectionRequest,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _review_service(request, session, owner).append_correction(
        case_id,
        document_id=body.document_id,
        processing_run_id=body.processing_run_id,
        field_path=body.field_path,
        action=body.action,
        corrected_value=body.corrected_value,
        evidence_id=body.evidence_id,
        verification_method=body.verification_method,
        page_number=body.page_number,
        reason_code=body.reason_code,
        request_id=request_id,
    )
    payload["readiness"]["request_id"] = request_id
    payload["request_id"] = request_id
    return payload


@router.post("/cases/{case_id}/document-role-assignments",
             response_model=DocumentRoleAssignmentResponse, status_code=201,
             summary="Version an explicit role/source selection")
def assign_document_role(
    case_id: UUID,
    body: DocumentRoleAssignmentRequest,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _review_service(request, session, owner).assign_role(
        case_id,
        role=body.role,
        document_id=body.document_id,
        confirm_mismatch=body.confirm_mismatch,
        reason_code=body.reason_code,
        request_id=request_id,
    )
    payload["readiness"]["request_id"] = request_id
    payload["request_id"] = request_id
    return payload


@router.post("/cases/{case_id}/analyze", response_model=AnalysisRunResponse,
             status_code=201, summary="Analyze a trusted, ready case revision")
def analyze_case(
    case_id: UUID,
    request: Request,
    response: Response,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    analysis_run = _analysis_service(request, session, owner).analyze(case_id)
    response.headers["Location"] = (
        f"/api/v1/cases/{case_id}/analysis/{analysis_run['analysis_run_id']}"
    )
    return {"analysis_run": analysis_run, "request_id": request_id}


@router.get("/cases/{case_id}/analysis", response_model=AnalysisRunListResponse,
            summary="List owner-scoped analysis runs")
def list_analysis_runs(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _analysis_service(request, session, owner).list_runs(case_id)
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/analysis/{analysis_run_id}",
            response_model=AnalysisRunResponse, summary="Get one safe analysis result")
def get_analysis_run(
    case_id: UUID,
    analysis_run_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    analysis_run = _analysis_service(request, session, owner).get_run(
        case_id, analysis_run_id
    )
    return {"analysis_run": analysis_run, "request_id": request_id}


@router.get("/cases/{case_id}/jobs", response_model=JobListResponse,
            summary="List processing jobs for an owned case")
def list_case_jobs(
    case_id: UUID,
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    job_type: str | None = Query(None, max_length=64),
    status: JobState | None = None,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_jobs(
        case_id, limit=limit, cursor=cursor, job_type=job_type, status=status
    )
    for item in payload["items"]:
        item["request_id"] = request_id
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/processing-status", response_model=ProcessingStatusResponse,
            summary="Get owner-scoped processing status for a case")
def get_processing_status(
    case_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_processing_status(case_id)
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/documents/{document_id}/fields",
            response_model=DocumentFieldsResponse, summary="Get versioned extracted fields")
def get_document_fields(
    case_id: UUID,
    document_id: UUID,
    request: Request,
    processing_run_id: UUID | None = Query(None),
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_document_fields(
        case_id, document_id, processing_run_id=processing_run_id
    )
    payload["request_id"] = request_id
    return payload


@router.get("/cases/{case_id}/evidence/{evidence_id}",
            response_model=EvidenceSpanView, summary="Get an owner-scoped verified evidence span")
def get_evidence(
    case_id: UUID,
    evidence_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_evidence(case_id, evidence_id)
    payload["request_id"] = request_id
    return payload


@router.post("/cases/{case_id}/documents/{document_id}/reprocess",
             response_model=DocumentUploadResponse, status_code=202,
             summary="Queue a new immutable document-processing run")
def reprocess_document(
    case_id: UUID,
    document_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=128),
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise ApplicationError(400, "INVALID_IDEMPOTENCY_KEY", "Invalid request",
                               "The idempotency key contains unsupported characters.")
    result = _service(request, session, owner).reprocess_document(
        case_id, document_id, idempotency_key=idempotency_key, request_id=request_id
    )
    return {
        "document": _document_view_for_response(
            result.document, request_id, selected_for_role=result.selected_for_role
        ),
        "job": _job_view_for_response(result.job, request_id),
        "request_id": request_id,
    }


@router.delete("/cases/{case_id}/documents/{document_id}",
               response_model=DocumentDeleteResponse, status_code=202,
               summary="Queue owner-scoped document and extraction deletion")
def delete_document(
    case_id: UUID,
    document_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    result = _service(request, session, owner).request_document_deletion(
        case_id, document_id, request_id=request_id
    )
    return {
        "document_id": result.document.document_id,
        "state": result.document.state,
        "job": _job_view_for_response(result.job, request_id) if result.job else None,
        "request_id": request_id,
    }


@router.get("/cases/{case_id}/documents/{document_id}/content", summary="Download an owned PDF")
def get_document_content(
    case_id: UUID,
    document_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> StreamingResponse:
    document, data = _service(request, session, owner).get_document_content(case_id, document_id)
    filename = quote(document.original_filename, safe="")
    return StreamingResponse(
        iter((data,)),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/cases/{case_id}/jobs/{job_id}", response_model=JobStatusResponse,
            summary="Get an owned processing job")
def get_job(
    case_id: UUID,
    job_id: UUID,
    request: Request,
    owner: OwnerPrincipal = Depends(get_owner_principal),
    session=Depends(get_session),
) -> dict:
    request_id = request_id_for(request)
    payload = _service(request, session, owner).get_job(job_id, case_id=case_id)
    payload["request_id"] = request_id
    return payload
