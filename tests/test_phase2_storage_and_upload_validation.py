"""Unit tests for Phase 2 private storage and PDF intake validation."""
from __future__ import annotations

import hashlib
import os
import stat
from io import BytesIO

import pytest
from pypdf import PdfWriter

from claimcheck.application.upload_validation import (
    UploadRejected,
    normalize_document_role,
    validate_pdf_upload,
)
from claimcheck.persistence.storage import InvalidStorageKey, PrivateLocalFileStorage


def _pdf_bytes(page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_validator_checks_real_signature_parser_size_and_pages() -> None:
    data = _pdf_bytes()
    result = validate_pdf_upload(
        filename=r"C:\Users\person\..\bill.pdf",
        content_type="application/pdf; charset=binary",
        data=data,
        max_bytes=1024 * 1024,
        max_pages=2,
    )
    assert result.original_filename == "bill.pdf"
    assert result.media_type == "application/pdf"
    assert result.byte_size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.page_count == 1
    assert result.content == data

    with pytest.raises(UploadRejected) as forged:
        validate_pdf_upload(
            filename="bill.pdf", content_type="application/pdf", data=b"not a pdf",
            max_bytes=1024, max_pages=10,
        )
    assert forged.value.status == 415
    assert forged.value.code == "UNSUPPORTED_FILE_TYPE"

    with pytest.raises(UploadRejected) as corrupt:
        validate_pdf_upload(
            filename="bill.pdf", content_type="application/pdf",
            data=b"%PDF-1.4\nthis has a signature but no valid PDF structure",
            max_bytes=1024, max_pages=10,
        )
    assert corrupt.value.status == 422
    assert corrupt.value.code == "CORRUPT_PDF"

    with pytest.raises(UploadRejected) as oversized:
        validate_pdf_upload(
            filename="bill.pdf", content_type="application/pdf", data=data + b" " * 2048,
            max_bytes=len(data), max_pages=10,
        )
    assert oversized.value.status == 413
    assert oversized.value.code == "UPLOAD_TOO_LARGE"

    with pytest.raises(UploadRejected) as wrong_type:
        validate_pdf_upload(
            filename="bill.txt", content_type="application/pdf", data=data,
            max_bytes=1024 * 1024, max_pages=10,
        )
    assert wrong_type.value.code == "UNSUPPORTED_FILE_TYPE"

    with pytest.raises(UploadRejected) as wrong_mime:
        validate_pdf_upload(
            filename="bill.pdf", content_type="text/plain", data=data,
            max_bytes=1024 * 1024, max_pages=10,
        )
    assert wrong_mime.value.code == "UNSUPPORTED_FILE_TYPE"

    with pytest.raises(UploadRejected) as too_many_pages:
        validate_pdf_upload(
            filename="bill.pdf", content_type="application/pdf", data=_pdf_bytes(3),
            max_bytes=1024 * 1024, max_pages=2,
        )
    assert too_many_pages.value.code == "PDF_PAGE_LIMIT_EXCEEDED"


def test_document_role_aliases_map_to_existing_domain_roles() -> None:
    assert normalize_document_role("POLICY") == "policy_wording"
    assert normalize_document_role("policy_schedule") == "policy_schedule"
    assert normalize_document_role("HOSPITAL_BILL") == "bill"
    assert normalize_document_role("SETTLEMENT_LETTER") == "settlement"
    with pytest.raises(UploadRejected) as error:
        normalize_document_role("unsupported")
    assert error.value.code == "INVALID_DOCUMENT_ROLE"


def test_private_storage_round_trip_metadata_and_idempotent_delete(tmp_path) -> None:
    root = tmp_path / "private"
    storage = PrivateLocalFileStorage(root)
    key = "cases/0fd66fd0/objects/hidden-random-token.pdf"
    data = b"private test bytes"

    assert storage.put(key, data) == key
    assert storage.exists(key)
    assert storage.get(key) == data
    metadata = storage.metadata(key)
    assert metadata.size_bytes == len(data)
    assert metadata.sha256 == hashlib.sha256(data).hexdigest()
    stored_file = root / key
    assert stat.S_IMODE(stored_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    storage.delete(key)
    storage.delete(key)
    assert not storage.exists(key)
    assert storage.check_ready() is True


def test_private_storage_rejects_traversal_and_symlink_escape(tmp_path) -> None:
    root = tmp_path / "private"
    storage = PrivateLocalFileStorage(root)
    for key in ("../escape.pdf", "/absolute.pdf", "cases/../../escape.pdf",
                "cases\\escape.pdf", "cases//empty.pdf"):
        with pytest.raises(InvalidStorageKey):
            storage.put(key, b"no")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"secret")
    (root / "cases").mkdir()
    try:
        os.symlink(outside, root / "cases" / "link")
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlink support
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(InvalidStorageKey):
        storage.get("cases/link/secret.pdf")
