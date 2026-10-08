"""Focused Phase 3 ingestion, evidence and private-storage tests (no PostgreSQL required)."""
from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from claimcheck.application.document_processing import ProcessingFailure, process_document_bytes
from claimcheck.persistence.storage import InvalidStorageKey, PrivateLocalFileStorage
from claimcheck.worker.handlers.document_process import DocumentProcessHandler


def _pdf_bytes(lines: list[str], *, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    operations = ["BT /F1 12 Tf 50 740 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            operations.append("0 -18 Td")
        operations.append(f"({escaped}) Tj")
    operations.append("ET")
    stream = DecodedStreamObject()
    stream.set_data(" ".join(operations).encode("ascii", errors="backslashreplace"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _process(data: bytes, *, role: str = "bill", expected_sha: str | None = None,
             ocr_enabled: bool = False):
    return process_document_bytes(
        data,
        case_id=uuid4(),
        document_id=uuid4(),
        processing_run_id=uuid4(),
        assigned_role=role,
        expected_sha256=expected_sha or sha256(data).hexdigest(),
        expected_pages=1,
        max_pages=10,
        ocr_enabled=ocr_enabled,
    )


def test_existing_dual_reader_parser_and_span_verification_are_used():
    data = _pdf_bytes([
        "Hospital Bill",
        "01/01/2026 Room Charge 100.00",
        "Grand Total 100.00",
    ])
    output = _process(data)

    assert output.status == "READY_FOR_ANALYSIS"
    assert output.role_mismatch is False
    item_amount = next(field for field in output.fields
                       if field.field_path == "bill.item_amounts")
    assert item_amount.value == [10_000]
    assert item_amount.state == "VERIFIED"
    assert {span.reader for span in output.evidence} == {"layout", "native"}
    assert all(span.verification_status == "VERIFIED" for span in output.evidence)
    assert output.summary["analysis_or_verdict_created"] is False
    assert not hasattr(output, "structured_case")
    assert not hasattr(output, "verdict")


def test_user_role_and_detected_role_are_separate_and_mismatch_requires_review():
    data = _pdf_bytes(["Hospital Bill", "Grand Total 1234.00"])
    output = _process(data, role="settlement")

    assert output.detected_role == "bill"
    assert output.role_mismatch is True
    assert output.status == "NEEDS_REVIEW"
    role_field = next(field for field in output.fields
                      if field.field_path == "document.role.detected")
    assert role_field.value == "bill"
    assert role_field.state == "NEEDS_REVIEW"
    assert all(field.field_path != "document.role.assigned" for field in output.fields)


def test_missing_and_unreadable_amounts_are_never_zero():
    missing = _process(_pdf_bytes(["Hospital Bill"]))
    missing_amounts = next(field for field in missing.fields
                           if field.field_path == "bill.item_amounts")
    assert missing.status == "EVIDENCE_GAP"
    assert missing_amounts.state == "MISSING"
    assert missing_amounts.value is None
    assert missing_amounts.value != 0

    unreadable = _process(_pdf_bytes(["01/01/2026 Room Charge 100,00"]))
    line_amounts = [field for field in unreadable.fields
                    if field.field_path.endswith(".amount_paise")]
    assert line_amounts
    assert line_amounts[0].value is None
    assert line_amounts[0].state in {"UNREADABLE", "QUARANTINED", "CONFLICTING"}
    assert line_amounts[0].value != 0
    assert unreadable.status == "NEEDS_REVIEW"


def test_reader_disagreement_is_quarantined_and_not_repaired():
    # The native text stream and geometry-ordered reader preserve different grouping around
    # the comma; the existing parser must refuse to select a more plausible magnitude.
    data = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 1,234.00"])
    output = _process(data)
    amount = next(field for field in output.fields
                  if field.field_path.endswith(".amount_paise"))
    assert amount.value is None
    assert amount.state in {"QUARANTINED", "CONFLICTING", "UNREADABLE"}
    assert output.status == "NEEDS_REVIEW"


def test_settlement_parser_records_only_literal_lines_with_dual_verified_amount():
    data = _pdf_bytes(["Settlement letter", "Approved amount 350.00"])
    output = _process(data, role="settlement")

    assert output.detected_role == "settlement"
    assert output.role_mismatch is False
    assert output.status == "READY_FOR_ANALYSIS"
    amount = next(field for field in output.fields
                  if field.field_path.startswith("settlement.line.")
                  and field.field_path.endswith(".amount_paise")
                  and field.state == "VERIFIED")
    assert amount.value == 35_000
    assert amount.state == "VERIFIED"
    line = next(record for record in output.records
                if record.record_type == "SETTLEMENT_LINE"
                and "Approved amount" in record.record["description"])
    assert "Approved amount" in line.record["description"]
    assert "lawful" not in str(output.summary).lower()
    assert output.summary["analysis_or_verdict_created"] is False


def test_policy_schedule_without_an_existing_parser_remains_an_evidence_gap():
    data = _pdf_bytes(["Policy Schedule", "Sum Insured 500000"])
    output = _process(data, role="policy_schedule")
    assert output.status == "EVIDENCE_GAP"
    field = next(field for field in output.fields
                 if field.field_path == "policy_schedule.records")
    assert field.state == "MISSING"
    assert field.reason_code == "PARSER_NOT_AVAILABLE"


def test_encrypted_malformed_and_hash_mismatched_pdfs_fail_safely():
    encrypted = _pdf_bytes(["Encrypted source"], encrypted=True)
    with pytest.raises(ProcessingFailure) as encrypted_error:
        _process(encrypted)
    assert encrypted_error.value.error_code == "ENCRYPTED_PDF"
    assert encrypted_error.value.retryable is False

    malformed = b"%PDF-1.4\nnot a valid xref or page tree"
    with pytest.raises(ProcessingFailure) as malformed_error:
        _process(malformed)
    assert malformed_error.value.error_code == "INVALID_PDF"

    valid = _pdf_bytes(["Hospital Bill"])
    with pytest.raises(ProcessingFailure) as hash_error:
        _process(valid, expected_sha="0" * 64)
    assert hash_error.value.error_code == "STORED_HASH_MISMATCH"


def test_existing_ocr_fallback_is_called_for_pages_without_a_text_layer(monkeypatch):
    import claimcheck.ingest.pdf as pdf_module

    calls = {"words": 0, "plain": 0}

    def empty_words(*_args, **_kwargs):
        calls["words"] += 1
        return "", []

    def empty_plain(*_args, **_kwargs):
        calls["plain"] += 1
        return ""

    monkeypatch.setattr(pdf_module, "ocr_words", empty_words)
    monkeypatch.setattr(pdf_module, "ocr_plain", empty_plain)
    output = _process(_pdf_bytes([" "]), ocr_enabled=True)
    assert calls["words"] == 1
    assert calls["plain"] >= 1
    assert output.status == "EVIDENCE_GAP"
    assert output.summary["pages_without_readable_text"] == 1


def test_private_storage_rejects_traversal_and_symlink_and_immutable_artifact_corruption(tmp_path):
    storage = PrivateLocalFileStorage(tmp_path / "private")
    for key in ("../outside", "/absolute", "cases/../../outside", "cases\\escape"):
        with pytest.raises(InvalidStorageKey):
            storage.put(key, b"private")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = storage.root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidStorageKey):
        storage.put("linked/file.bin", b"private")

    handler = DocumentProcessHandler(lambda: None, storage, worker_id="unit-test")
    storage.put("artifacts/immutable.bin", b"tampered")
    with pytest.raises(ProcessingFailure) as error:
        handler._put_immutable("artifacts/immutable.bin", b"expected")
    assert error.value.error_code == "ARTIFACT_HASH_MISMATCH"


def test_unexpected_api_exception_does_not_leak_exception_path_or_source_text():
    from starlette.requests import Request

    from claimcheck.api.errors import unexpected_exception_handler

    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/cases",
        "raw_path": b"/api/v1/cases",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("test", 1234),
        "server": ("test", 80),
    })
    request.state.request_id = "safe-request-id"
    private_detail = "/private/storage/patient.pdf: OCR parser saw PHI-12345"
    response = asyncio.run(
        unexpected_exception_handler(request, RuntimeError(private_detail))
    )
    payload = json.loads(response.body)
    assert response.status_code == 500
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["request_id"] == "safe-request-id"
    assert private_detail not in response.body.decode()
    assert "/private/storage" not in response.body.decode()
    assert "PHI-12345" not in response.body.decode()
