"""Safe RFC 9457-style Problem Details responses for the HTTP boundary."""
from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from claimcheck.application.errors import ApplicationError
from .schemas import ProblemDetails


def request_id_for(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if value is None:
        value = str(uuid4())
        request.state.request_id = value
    return str(value)


def _problem(request: Request, *, status: int, title: str, code: str,
             detail: str, retryable: bool = False) -> JSONResponse:
    content = ProblemDetails(
        title=title,
        status=status,
        code=code,
        detail=detail,
        request_id=request_id_for(request),
        retryable=retryable,
    ).model_dump()
    return JSONResponse(
        status_code=status,
        content=content,
        media_type="application/problem+json",
        headers={"X-Request-ID": content["request_id"]},
    )


async def application_exception_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    return _problem(
        request,
        status=exc.status,
        title=exc.title,
        code=exc.code,
        detail=exc.detail,
        retryable=exc.retryable,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    status = int(getattr(exc, "status_code", 400))
    title, code, detail, retryable = {
        400: ("Invalid request", "INVALID_REQUEST", "The request could not be processed.", False),
        401: ("Authentication required", "UNAUTHENTICATED",
              "A supported identity provider is required.", False),
        404: ("Not found", "NOT_FOUND", "The requested resource was not found.", False),
        405: ("Method not allowed", "METHOD_NOT_ALLOWED",
              "The requested method is not available.", False),
        409: ("State conflict", "STATE_CONFLICT", "The requested change conflicts with current state.", False),
        411: ("Content length required", "CONTENT_LENGTH_REQUIRED",
              "Provide a bounded Content-Length for this upload.", False),
        413: ("Upload too large", "UPLOAD_TOO_LARGE",
              "The request exceeds the configured upload limit.", False),
        415: ("Unsupported file", "UNSUPPORTED_FILE_TYPE", "Upload a PDF document.", False),
        422: ("Request could not be processed", "VALIDATION_ERROR",
              "The request does not match this endpoint's contract.", False),
        503: ("Service unavailable", "DEPENDENCY_UNAVAILABLE",
              "A required service is not available.", True),
    }.get(status, ("Request failed", "REQUEST_FAILED",
                  "The request could not be completed.", False))
    return _problem(request, status=status, title=title, code=code, detail=detail,
                    retryable=retryable)


async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError) -> JSONResponse:
    # Do not echo invalid input values or Pydantic's raw error context.
    return _problem(
        request,
        status=422,
        title="Request validation failed",
        code="VALIDATION_ERROR",
        detail="The request does not match this endpoint's contract.",
    )


async def database_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    # DB/driver messages may include URLs, schema names or query values; none reach the client.
    return _problem(
        request,
        status=503,
        title="Service unavailable",
        code="DATABASE_UNAVAILABLE",
        detail="Persistent storage is temporarily unavailable.",
        retryable=True,
    )


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Intentionally omit exception text and traceback from both the HTTP response and logs.
    return _problem(
        request,
        status=500,
        title="Internal server error",
        code="INTERNAL_ERROR",
        detail="The request could not be completed. Use the request ID when reporting this issue.",
        retryable=False,
    )
