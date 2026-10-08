"""Bounded PDF intake validation. This module does not extract text or process pages."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader


@dataclass(frozen=True)
class ValidatedPDF:
    content: bytes
    original_filename: str
    byte_size: int
    sha256: str
    page_count: int
    media_type: str = "application/pdf"


class UploadRejected(Exception):
    """A safe, caller-displayable upload rejection with no parser exception details."""

    def __init__(self, status: int, code: str, title: str, detail: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = False


_ROLE_ALIASES = {
    "policy": "policy_wording",
    "policy_wording": "policy_wording",
    "policy_schedule": "policy_schedule",
    "hospital_bill": "bill",
    "bill": "bill",
    "settlement_letter": "settlement",
    "settlement": "settlement",
}


def normalize_document_role(value: str) -> str:
    if not isinstance(value, str):
        raise UploadRejected(422, "INVALID_DOCUMENT_ROLE", "Invalid document role",
                             "Choose a supported document role.")
    role = value.strip().lower()
    normalized = _ROLE_ALIASES.get(role)
    if normalized is None:
        raise UploadRejected(422, "INVALID_DOCUMENT_ROLE", "Invalid document role",
                             "Choose a supported document role.")
    return normalized


def _safe_filename(filename: str | None) -> str:
    # The original name is display metadata only; both POSIX and Windows separators are stripped.
    raw = (filename or "upload.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    raw = unicodedata.normalize("NFC", raw)
    safe = "".join(ch if ch.isprintable() and ch not in "\\/\x00" else "_" for ch in raw)
    safe = safe.strip(" .")
    if not safe:
        safe = "upload.pdf"
    if len(safe) > 255:
        suffix = ".pdf" if safe.lower().endswith(".pdf") else ""
        safe = safe[:255 - len(suffix)].rstrip(" .") + suffix
    return safe


def validate_pdf_upload(
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    max_bytes: int,
    max_pages: int,
) -> ValidatedPDF:
    """Check name/type/signature/size and strict PDF structure before private persistence."""
    safe_name = _safe_filename(filename)
    supplied_basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not supplied_basename.lower().endswith(".pdf"):
        raise UploadRejected(415, "UNSUPPORTED_FILE_TYPE", "Unsupported file",
                             "Upload a PDF document.")

    declared_type = (content_type or "").split(";", 1)[0].strip().lower()
    if declared_type and declared_type not in {"application/pdf", "application/x-pdf"}:
        raise UploadRejected(415, "UNSUPPORTED_FILE_TYPE", "Unsupported file",
                             "Upload a PDF document.")

    if len(data) > max_bytes:
        raise UploadRejected(413, "UPLOAD_TOO_LARGE", "Upload too large",
                             "The PDF exceeds the configured file-size limit.")
    if not data or not re.search(rb"%PDF-\d\.\d", data[:1024]):
        raise UploadRejected(415, "UNSUPPORTED_FILE_TYPE", "Unsupported file",
                             "The uploaded content is not a PDF document.")

    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise UploadRejected(422, "ENCRYPTED_PDF", "Unsupported PDF",
                                 "Password-protected PDFs are not accepted.")
        page_count = len(reader.pages)
        if page_count < 1:
            raise UploadRejected(422, "CORRUPT_PDF", "Invalid PDF",
                                 "The PDF does not contain a readable page tree.")
        if page_count > max_pages:
            raise UploadRejected(413, "PDF_PAGE_LIMIT_EXCEEDED", "Upload too large",
                                 "The PDF exceeds the configured page-count limit.")
    except UploadRejected:
        raise
    except Exception as exc:
        # Parser/library messages may contain untrusted input; only a stable code crosses the API.
        raise UploadRejected(422, "CORRUPT_PDF", "Invalid PDF",
                             "The PDF structure could not be validated.") from exc

    return ValidatedPDF(
        content=data,
        original_filename=safe_name,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        page_count=page_count,
    )
