"""Disposable-PostgreSQL Phase 3 worker, lease, idempotency and API integration tests."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from claimcheck.api.app import create_app
from claimcheck.api.config import AppSettings
from claimcheck.persistence.database import create_postgres_engine
from claimcheck.application.document_processing import process_document_bytes
from claimcheck.persistence.models import (
    CaseDocument,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    ProcessingJob,
    StoredPageExtraction,
    utc_now,
)
from claimcheck.persistence.repositories import JobClaimRepository, WorkerIdentity
from claimcheck.worker.runner import DocumentWorker, WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("CLAIMCHECK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set CLAIMCHECK_TEST_DATABASE_URL to a disposable PostgreSQL database for Phase 3 integration tests.",
)
OWNER_A = UUID("b7748770-2971-4fc0-8cda-b0193275f391")
OWNER_B = UUID("69e28f0a-cf4e-438b-971d-f6357e3b348f")


def _pdf_bytes(lines: list[str]) -> bytes:
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
    ops = ["BT /F1 12 Tf 50 740 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            ops.append("0 -18 Td")
        ops.append(f"({escaped}) Tj")
    ops.append("ET")
    stream = DecodedStreamObject()
    stream.set_data(" ".join(ops).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(scope="module")
def pg_engine():
    if not TEST_DATABASE_URL:  # pragma: no cover - covered by module skip
        pytest.skip("PostgreSQL integration database not configured")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    engine = create_postgres_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_database(pg_engine):
    with pg_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE owners CASCADE"))


@contextmanager
def _client(tmp_path, *, owner_id: UUID = OWNER_A):
    app = create_app(AppSettings(
        environment="development",
        database_url=TEST_DATABASE_URL,
        storage_root=str(tmp_path / f"storage-{owner_id.hex}"),
        development_owner_id=owner_id,
    ))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, app
    finally:
        app.state.database_engine.dispose()


def _create_case(client: TestClient) -> dict:
    response = client.post("/api/v1/cases", json={"display_name": "Phase 3 synthetic"})
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, case_id: UUID, data: bytes,
            *, idempotency_key: str | None = None, role: str = "HOSPITAL_BILL"):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    response = client.post(
        f"/api/v1/cases/{case_id}/documents",
        files={"file": ("synthetic.pdf", data, "application/pdf")},
        data={"role": role},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()


def _worker(pg_engine, app, *, worker_id: str = "phase3-worker") -> DocumentWorker:
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    return DocumentWorker(factory, app.state.storage, settings=WorkerSettings(
        worker_id=worker_id,
        poll_interval_seconds=0.05,
        lease_seconds=60,
        heartbeat_seconds=5,
        retry_delay_seconds=1,
        max_pdf_pages=250,
        max_pdf_bytes=25 * 1024 * 1024,
        ocr_enabled=False,
    ))


def test_worker_persists_immutable_versions_and_owner_scoped_apis(pg_engine, tmp_path):
    data = _pdf_bytes([
        "Hospital Bill",
        "01/01/2026 Room Charge 100.00",
        "Grand Total 100.00",
    ])
    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data, idempotency_key="original-v1")
        duplicate_retry = _upload(client, case_id, data, idempotency_key="original-v1")
        duplicate_bytes = _upload(client, case_id, data)
        document_id = UUID(uploaded["document"]["document_id"])
        first_job_id = UUID(uploaded["job"]["job_id"])
        assert duplicate_retry["document"]["document_id"] == str(document_id)
        assert duplicate_retry["job"]["job_id"] == str(first_job_id)
        assert duplicate_bytes["document"]["document_id"] == str(document_id)
        assert duplicate_bytes["job"]["job_id"] == str(first_job_id)
        serialized = str(uploaded)
        assert uploaded["job"]["job_type"] == "DOCUMENT_PROCESS"
        assert "storage_key" not in serialized
        assert "private-storage" not in serialized
        assert data[:32].decode("latin1") not in serialized

        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            document = session.get(CaseDocument, document_id)
            original_key = document.storage_key
            original_hash = document.sha256
            assert session.scalar(select(func.count()).select_from(CaseDocument)) == 1
            assert session.scalar(select(func.count()).select_from(ProcessingJob)) == 1
            assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 1
        worker = _worker(pg_engine, app)
        assert worker.run_once() is True

        detail_response = client.get(
            f"/api/v1/cases/{case_id}/documents/{document_id}"
        )
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()
        assert len(detail["processing_runs"]) == 1
        assert detail["state"] == "READY_FOR_ANALYSIS"
        assert detail["processing_runs"][0]["status"] == "READY_FOR_ANALYSIS"
        assert "storage_key" not in detail_response.text
        assert detail["processing_runs"][0]["summary"]["analysis_or_verdict_created"] is False

        fields_response = client.get(
            f"/api/v1/cases/{case_id}/documents/{document_id}/fields"
        )
        assert fields_response.status_code == 200, fields_response.text
        fields = fields_response.json()["fields"]
        amount = next(item for item in fields if item["field_path"] == "bill.item_amounts")
        assert amount["state"] == "VERIFIED"
        assert amount["value"] == [10_000]
        evidence_id = amount["evidence_id"]
        # Aggregate amount fields intentionally do not pretend one span supports every line;
        # retrieve the line-level evidence through its corresponding amount field.
        evidence_response = None
        evidence = None
        for line_amount in fields:
            if (not line_amount["field_path"].endswith(".amount_paise")
                    or line_amount["state"] != "VERIFIED"):
                continue
            candidate_response = client.get(
                f"/api/v1/cases/{case_id}/evidence/{line_amount['evidence_id']}"
            )
            if candidate_response.status_code == 200 and (
                    "Room Charge" in candidate_response.json()["quoted_text"]):
                evidence_response = candidate_response
                evidence = candidate_response.json()
                evidence_id = line_amount["evidence_id"]
                break
        assert evidence_response is not None
        assert evidence["verification_status"] == "VERIFIED"
        assert "quote_artifact_key" not in evidence_response.text

        jobs_response = client.get(f"/api/v1/cases/{case_id}/jobs")
        assert jobs_response.status_code == 200
        assert jobs_response.json()["items"][0]["status"] == "SUCCEEDED"
        status_response = client.get(f"/api/v1/cases/{case_id}/processing-status")
        assert status_response.status_code == 200
        status = status_response.json()
        assert status["ready_for_analysis"] is False
        assert status["status"] == "ROLE_GAP"

        # Another development identity sees the same resources only as not found.
        with _client(tmp_path, owner_id=OWNER_B) as (other, _other_app):
            assert other.get(f"/api/v1/cases/{case_id}/documents/{document_id}").status_code == 404
            assert other.get(f"/api/v1/cases/{case_id}/evidence/{evidence_id}").status_code == 404
            assert other.get(f"/api/v1/cases/{case_id}/jobs").status_code == 404

        # Reprocessing is an explicit new immutable run; HTTP retries with the same key reuse it.
        first_reprocess = client.post(
            f"/api/v1/cases/{case_id}/documents/{document_id}/reprocess",
            headers={"Idempotency-Key": "run-v2"},
        )
        second_reprocess = client.post(
            f"/api/v1/cases/{case_id}/documents/{document_id}/reprocess",
            headers={"Idempotency-Key": "run-v2"},
        )
        assert first_reprocess.status_code == 202
        assert second_reprocess.status_code == 202
        assert first_reprocess.json()["job"]["job_id"] == second_reprocess.json()["job"]["job_id"]
        assert UUID(first_reprocess.json()["job"]["job_id"]) != first_job_id
        assert worker.run_once() is True
        detail = client.get(f"/api/v1/cases/{case_id}/documents/{document_id}").json()
        assert {run["run_number"] for run in detail["processing_runs"]} == {1, 2}
        with factory() as session:
            document = session.get(CaseDocument, document_id)
            assert document.storage_key == original_key
            assert document.sha256 == original_hash == sha256(app.state.storage.get(original_key)).hexdigest()
        assert app.state.storage.get(original_key) == data


def test_worker_crash_after_artifact_write_retries_same_run_without_duplicate_rows(pg_engine, tmp_path):
    data = _pdf_bytes([
        "Hospital Bill",
        "01/01/2026 Room Charge 100.00",
        "Grand Total 100.00",
    ])
    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data)
        document_id = UUID(uploaded["document"]["document_id"])
        job_id = UUID(uploaded["job"]["job_id"])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        crashed = _worker(pg_engine, app, worker_id="crash-before-finalize")

        # Simulate a process death after files were written but before the DB commit.
        with factory() as session:
            with session.begin():
                claimed = JobClaimRepository(
                    session, WorkerIdentity("crash-before-finalize")
                ).claim_next(lease_for=timedelta(minutes=1))
                assert claimed is not None and claimed.job_id == job_id
        context = crashed.process_handler._mark_running(job_id)
        assert context is not None
        output = process_document_bytes(
            data,
            case_id=context["case_id"],
            document_id=context["document_id"],
            processing_run_id=context["processing_run_id"],
            assigned_role=context["assigned_role"],
            expected_sha256=context["source_sha256"],
            expected_pages=context["page_count"],
            max_pages=crashed.settings.max_pdf_pages,
            ocr_enabled=False,
        )
        artifacts, _page_keys, _evidence_keys = crashed.process_handler._artifact_plan(
            context, output
        )
        crashed.process_handler._reserve_artifacts(job_id, artifacts)
        crashed.process_handler._write_artifacts(job_id, context, artifacts)
        with factory() as session:
            with session.begin():
                session.get(ProcessingJob, job_id).lease_expires_at = utc_now() - timedelta(seconds=1)

        recovered = _worker(pg_engine, app, worker_id="recovery-worker")
        assert recovered.run_once() is True
        with factory() as session:
            job = session.get(ProcessingJob, job_id)
            run = session.scalar(select(DocumentProcessingRun).where(
                DocumentProcessingRun.document_id == document_id
            ))
            assert job.status == "SUCCEEDED"
            assert job.attempt_count == 2
            assert run.status == "READY_FOR_ANALYSIS"
            assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 1
            assert session.scalar(select(func.count()).select_from(StoredPageExtraction)) == 1
            assert session.scalar(select(func.count()).select_from(DocumentEvidenceSpan)) == len(output.evidence)
            assert session.scalar(select(func.count()).select_from(DocumentProcessingField)) == len(output.fields)
            assert session.scalar(select(func.count()).select_from(DocumentProcessingRecord)) == len(output.records)
            original_key = session.get(CaseDocument, document_id).storage_key
        assert app.state.storage.get(original_key) == data


def test_skip_locked_lease_expiry_retry_and_terminal_attempt(pg_engine, tmp_path):
    data = _pdf_bytes(["Hospital Bill", "Grand Total 1234.00"])
    with _client(tmp_path) as (client, _app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data)
        job_id = UUID(uploaded["job"]["job_id"])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        locked = threading.Event()
        release = threading.Event()

        def first_claim():
            with factory() as session:
                with session.begin():
                    job = JobClaimRepository(session, WorkerIdentity("claim-A")).claim_next(
                        lease_for=timedelta(minutes=1)
                    )
                    assert job is not None and job.job_id == job_id
                    locked.set()
                    assert release.wait(timeout=10)
                    return job.job_id

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(first_claim)
            assert locked.wait(timeout=10)
            with factory() as session:
                with session.begin():
                    skipped = JobClaimRepository(
                        session, WorkerIdentity("claim-B")
                    ).claim_next(lease_for=timedelta(minutes=1))
                    assert skipped is None
            release.set()
            assert first.result(timeout=10) == job_id

        with factory() as session:
            with session.begin():
                job = session.get(ProcessingJob, job_id)
                job.max_attempts = 3
                job.lease_expires_at = utc_now() - timedelta(seconds=1)
        with factory() as session:
            with session.begin():
                reclaimed = JobClaimRepository(
                    session, WorkerIdentity("claim-B")
                ).claim_next(lease_for=timedelta(minutes=1))
                assert reclaimed is not None
                assert reclaimed.job_id == job_id
                assert reclaimed.attempt_count == 2
                failed = JobClaimRepository(
                    session, WorkerIdentity("claim-B")
                ).fail(job_id, error_code="TRANSIENT_TEST", retryable=True)
                assert failed is not None
                assert failed.status == "RETRYABLE_FAILURE"
                assert failed.error_code == "TRANSIENT_TEST"
        with factory() as session:
            with session.begin():
                session.get(ProcessingJob, job_id).available_at = utc_now() - timedelta(seconds=1)
        with factory() as session:
            with session.begin():
                last_attempt = JobClaimRepository(
                    session, WorkerIdentity("claim-B")
                ).claim_next(lease_for=timedelta(minutes=1))
                assert last_attempt is not None
                assert last_attempt.job_id == job_id
                assert last_attempt.attempt_count == 3
                terminal = JobClaimRepository(
                    session, WorkerIdentity("claim-B")
                ).fail(job_id, error_code="TRANSIENT_TEST", retryable=True)
                assert terminal is not None
                assert terminal.status == "FAILED"  # the bounded last attempt is terminal
                assert terminal.error_code == "TRANSIENT_TEST"


def test_document_delete_purges_original_and_all_run_artifacts(pg_engine, tmp_path):
    data = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 100.00"])
    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data)
        document_id = UUID(uploaded["document"]["document_id"])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            original_key = session.get(CaseDocument, document_id).storage_key
        worker = _worker(pg_engine, app)
        assert worker.run_once() is True

        deletion = client.delete(f"/api/v1/cases/{case_id}/documents/{document_id}")
        assert deletion.status_code == 202, deletion.text
        assert deletion.json()["state"] == "DELETE_PENDING"
        assert deletion.json()["job"]["job_type"] == "DOCUMENT_DELETE"
        delete_job_id = UUID(deletion.json()["job"]["job_id"])
        with factory() as session:
            with session.begin():
                job = session.get(ProcessingJob, delete_job_id)
                job.available_at = utc_now() - timedelta(seconds=1)
        assert worker.run_once() is True
        assert client.get(f"/api/v1/cases/{case_id}/documents/{document_id}").status_code == 404
        assert not app.state.storage.exists(original_key)
        with factory() as session:
            assert session.scalar(select(DocumentProcessingRun).where(
                DocumentProcessingRun.document_id == document_id
            )) is None
            document = session.get(CaseDocument, document_id)
            assert document.state == "DELETED"
            assert document.storage_key is None
            delete_job = session.get(ProcessingJob, delete_job_id)
            assert delete_job.status == "SUCCEEDED"


def test_stored_original_corruption_returns_safe_errors(pg_engine, tmp_path):
    data = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 100.00"])
    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data)
        document_id = UUID(uploaded["document"]["document_id"])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            original_key = session.get(CaseDocument, document_id).storage_key
        app.state.storage.put(original_key, b"corrupt original")
        content = client.get(f"/api/v1/cases/{case_id}/documents/{document_id}/content")
        assert content.status_code == 409
        assert content.json()["code"] == "DOCUMENT_CONTENT_CORRUPT"
        assert original_key not in content.text
        assert b"corrupt original".decode() not in content.text

        worker = _worker(pg_engine, app)
        assert worker.run_once() is True
        job_response = client.get(
            f"/api/v1/cases/{case_id}/jobs/{uploaded['job']['job_id']}"
        )
        assert job_response.status_code == 200
        assert job_response.json()["error_code"] == "STORED_HASH_MISMATCH"
        assert original_key not in job_response.text



def test_evidence_artifact_corruption_is_owner_scoped_and_never_leaks_storage_key(pg_engine, tmp_path):
    data = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 100.00"])
    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        uploaded = _upload(client, case_id, data)
        document_id = UUID(uploaded["document"]["document_id"])
        worker = _worker(pg_engine, app)
        assert worker.run_once() is True
        fields = client.get(
            f"/api/v1/cases/{case_id}/documents/{document_id}/fields"
        ).json()["fields"]
        amount_field = next(item for item in fields
                            if item["field_path"].endswith(".amount_paise")
                            and item["state"] == "VERIFIED")
        evidence_id = UUID(amount_field["evidence_id"])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            evidence = session.get(DocumentEvidenceSpan, evidence_id)
            quote_key = evidence.quote_artifact_key
        app.state.storage.put(quote_key, b"corrupt evidence text")
        response = client.get(f"/api/v1/cases/{case_id}/evidence/{evidence_id}")
        assert response.status_code == 409
        assert response.json()["code"] == "EVIDENCE_ARTIFACT_CORRUPT"
        assert quote_key not in response.text
        assert "corrupt evidence text" not in response.text


def test_document_lifecycle_terminal_states_and_recovery(pg_engine, tmp_path):
    valid_bill = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 100.00", "Grand Total 100.00"])
    valid_policy = _pdf_bytes(["Policy Wording", "Terms and conditions"])
    valid_schedule = _pdf_bytes(["Policy Schedule", "Sum Insured: 500000"])
    valid_settlement = _pdf_bytes(["Settlement Letter", "Approved 50000"])
    irrelevant = _pdf_bytes(["Laplace Transform", "Math math math"])

    with _client(tmp_path) as (client, app):
        case_id = UUID(_create_case(client)["case_id"])
        
        _upload(client, case_id, valid_policy, idempotency_key="doc1", role="policy_wording")
        _upload(client, case_id, valid_schedule, idempotency_key="doc2", role="policy_schedule")
        _upload(client, case_id, valid_settlement, idempotency_key="doc3", role="settlement")
        _upload(client, case_id, irrelevant, idempotency_key="doc4", role="bill")
        
        worker = _worker(pg_engine, app)
        while worker.run_once():
            pass
            
        status = client.get(f"/api/v1/cases/{case_id}/processing-status").json()
        assert status["ready_for_analysis"] is False
        
        docs_by_role = {d["assigned_role"]: d for d in status["documents"]}
        assert docs_by_role["policy_wording"]["latest_run"]["status"] in ("READY_FOR_ANALYSIS", "EVIDENCE_GAP", "NEEDS_REVIEW")
        
        bill_doc = docs_by_role["bill"]
        assert bill_doc["latest_run"]["status"] in ("FAILED", "READY_FOR_ANALYSIS", "EVIDENCE_GAP", "NEEDS_REVIEW")
        if bill_doc["latest_run"]["status"] != "FAILED":
            assert bill_doc["latest_run"]["detected_role"] is None
        
        error_code = bill_doc["latest_run"]["error_code"] or ""
        assert "sql" not in error_code.lower()
            
        delete_resp = client.delete(f"/api/v1/cases/{case_id}/documents/{bill_doc['document_id']}")
        assert delete_resp.status_code == 202
        
        _upload(client, case_id, valid_bill, idempotency_key="doc4-fixed", role="bill")
        while worker.run_once():
            pass
            
        status2 = client.get(f"/api/v1/cases/{case_id}/processing-status").json()
        docs2_by_role = {d["assigned_role"]: d for d in status2["documents"]}
        assert docs2_by_role["bill"]["latest_run"]["status"] in ("READY_FOR_ANALYSIS", "EVIDENCE_GAP", "NEEDS_REVIEW")
