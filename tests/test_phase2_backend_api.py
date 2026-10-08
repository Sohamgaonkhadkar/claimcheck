"""Focused PostgreSQL integration tests for the Phase 2 HTTP/application boundary.

Set CLAIMCHECK_TEST_DATABASE_URL to a disposable PostgreSQL database. The tests run Alembic's
checked-in migration and truncate that explicitly named test database between cases.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from claimcheck.api.app import create_app
from claimcheck.api.config import AppSettings
from claimcheck.persistence.database import create_postgres_engine
from claimcheck.persistence.models import (
    AnalysisRun, AuditEvent, Case, CaseDocument, JobStatus, ProcessingJob,
)
from claimcheck.persistence.repositories import JobClaimRepository, WorkerIdentity
from claimcheck.persistence.storage import PrivateLocalFileStorage

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("CLAIMCHECK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set CLAIMCHECK_TEST_DATABASE_URL to a disposable PostgreSQL database for Phase 2 integration tests.",
)
OWNER_A = UUID("b7748770-2971-4fc0-8cda-b0193275f391")
OWNER_B = UUID("69e28f0a-cf4e-438b-971d-f6357e3b348f")


def _pdf_bytes(title: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if title:
        writer.add_metadata({"/Title": title})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(scope="module")
def pg_engine():
    if not TEST_DATABASE_URL:  # pragma: no cover - covered by the module skip marker
        pytest.skip("PostgreSQL test database not configured")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        config = Config(str(ROOT / "alembic.ini"))
        command.upgrade(config, "head")
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
def _client(tmp_path, *, owner_id: UUID = OWNER_A, max_upload_bytes: int = 25 * 1024 * 1024,
            max_request_bytes: int = 26 * 1024 * 1024, storage=None):
    app = create_app(AppSettings(
        environment="development",
        database_url=TEST_DATABASE_URL,
        storage_root=str(tmp_path / f"storage-{owner_id.hex}"),
        development_owner_id=owner_id,
        max_upload_bytes=max_upload_bytes,
        max_request_bytes=max_request_bytes,
    ), storage=storage)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, app
    finally:
        app.state.database_engine.dispose()


def _create_case(client: TestClient, name: str | None = None) -> dict:
    body = {} if name is None else {"display_name": name, "claim_date": "2026-10-01"}
    response = client.post("/api/v1/cases", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, case_id: str, *, data: bytes | None = None,
            role: str = "HOSPITAL_BILL", filename: str = "bill.pdf",
            content_type: str = "application/pdf", idempotency_key: str | None = None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        f"/api/v1/cases/{case_id}/documents",
        files={"file": (filename, data if data is not None else _pdf_bytes(), content_type)},
        data={"role": role},
        headers=headers,
    )


class _FailOnceStorage:
    def __init__(self, root: Path) -> None:
        self.inner = PrivateLocalFileStorage(root)
        self.fail_next_delete = True

    def put(self, key: str, data: bytes) -> str:
        return self.inner.put(key, data)

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(key)

    def delete(self, key: str) -> None:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise OSError("private storage unavailable")
        self.inner.delete(key)

    def metadata(self, key: str):
        return self.inner.metadata(key)

    def check_ready(self) -> bool:
        return self.inner.check_ready()


def test_owner_scoped_case_crud_pagination_and_upload_idempotency(tmp_path, pg_engine) -> None:
    with _client(tmp_path) as (client, app):
        created = [_create_case(client, f"Case {i}") for i in range(3)]
        owner_override = client.post("/api/v1/cases", json={"owner_id": str(OWNER_B)})
        assert owner_override.status_code == 422
        assert owner_override.headers["content-type"].startswith("application/problem+json")
        assert all(UUID(item["case_id"]).version == 4 for item in created)
        assert all(item["status"] == "CREATED" for item in created)
        assert created[0]["document_checklist"]["bill"] == "MISSING"

        # Client owner headers are ignored; the configured development principal owns every row.
        client.post("/api/v1/cases", json={}, headers={"X-Owner-ID": str(OWNER_B)})
        first_page = client.get("/api/v1/cases?limit=2")
        assert first_page.status_code == 200
        assert len(first_page.json()["items"]) == 2
        assert first_page.json()["next_cursor"]
        second_page = client.get(
            "/api/v1/cases?limit=2&cursor=" + first_page.json()["next_cursor"]
        )
        assert second_page.status_code == 200
        page_ids = {item["case_id"] for item in first_page.json()["items"]}
        assert page_ids.isdisjoint({item["case_id"] for item in second_page.json()["items"]})

        case_id = created[0]["case_id"]
        patched = client.patch(f"/api/v1/cases/{case_id}", json={"display_name": "Corrected label"})
        assert patched.status_code == 200
        assert patched.json()["display_name"] == "Corrected label"
        # Updating metadata and projecting the case into Phase 4's ROLE_GAP state
        # are two independently versioned changes.
        assert patched.json()["version"] == 3

        raw_pdf = _pdf_bytes()
        uploaded = _upload(
            client, case_id, data=raw_pdf, filename=r"../../Private Bill.pdf",
            role="HOSPITAL_BILL", idempotency_key="intake-1",
        )
        assert uploaded.status_code == 202, uploaded.text
        payload = uploaded.json()
        document_id = payload["document"]["document_id"]
        job_id = payload["job"]["job_id"]
        assert payload["document"]["role"] == "bill"  # canonical API-SPEC role
        assert payload["document"]["original_filename"] == "Private Bill.pdf"
        assert payload["document"]["state"] == "QUEUED"
        assert payload["job"]["status"] == "QUEUED"
        assert payload["job"]["attempt_count"] == 0
        assert payload["job"]["retryable"] is False
        assert "storage_key" not in uploaded.text
        assert str(tmp_path) not in uploaded.text
        assert "sha256" not in uploaded.text

        repeated = _upload(
            client, case_id, data=raw_pdf, filename="renamed.pdf", role="bill",
            idempotency_key="intake-1",
        )
        assert repeated.status_code == 202
        assert repeated.json()["document"]["document_id"] == document_id
        assert repeated.json()["job"]["job_id"] == job_id

        # A new request key with the same bytes/role is also idempotent within this case.
        repeated_hash = _upload(client, case_id, data=raw_pdf, role="bill")
        assert repeated_hash.status_code == 202
        assert repeated_hash.json()["document"]["document_id"] == document_id

        reused_key = _upload(
            client, case_id, data=_pdf_bytes("different bytes"), role="bill",
            idempotency_key="intake-1",
        )
        assert reused_key.status_code == 409
        assert reused_key.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        duplicate_role = _upload(client, case_id, data=raw_pdf, role="SETTLEMENT_LETTER")
        assert duplicate_role.status_code == 409
        assert duplicate_role.json()["code"] == "DUPLICATE_DOCUMENT"

        docs = client.get(f"/api/v1/cases/{case_id}/documents?role=HOSPITAL_BILL")
        assert docs.status_code == 200
        assert len(docs.json()["items"]) == 1
        assert docs.json()["items"][0]["document_id"] == document_id
        job = client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["status"] == "QUEUED"

        details = client.get(f"/api/v1/cases/{case_id}")
        assert details.json()["status"] == "PROCESSING"
        assert details.json()["document_checklist"]["bill"] == "PRESENT"

        with Session(pg_engine) as session:
            document = session.get(CaseDocument, UUID(document_id))
            assert document is not None
            assert document.owner_id == OWNER_A
            assert document.storage_key.startswith(f"cases/{UUID(case_id).hex}/objects/")
            assert document.original_filename == "Private Bill.pdf"
            assert document.sha256
            # Revision/case status projections advance the optimistic version separately;
            # this flow performs multiple idempotent and read-side projections.
            assert session.scalar(select(Case).where(Case.case_id == UUID(case_id))).version >= 3
            events = list(session.scalars(select(AuditEvent).where(AuditEvent.case_id == UUID(case_id))))
            event_types = {event.event_type for event in events}
            assert {"CASE_CREATED", "CASE_UPDATED", "DOCUMENT_UPLOADED", "JOB_CREATED"} <= event_types
            assert all("raw_pdf" not in str(event.event_data) for event in events)


def test_document_list_is_paginated_with_an_owner_scoped_cursor(tmp_path, pg_engine) -> None:
    with _client(tmp_path) as (client, app):
        case_id = _create_case(client)["case_id"]
        document_ids = []
        for index in range(3):
            response = _upload(
                client, case_id, data=_pdf_bytes(f"fixture-{index}"),
                filename=f"bill-{index}.pdf", role="HOSPITAL_BILL",
            )
            assert response.status_code == 202, response.text
            document_ids.append(response.json()["document"]["document_id"])

        first = client.get(f"/api/v1/cases/{case_id}/documents?limit=2")
        assert first.status_code == 200
        assert len(first.json()["items"]) == 2
        cursor = first.json()["next_cursor"]
        assert cursor
        second = client.get(
            f"/api/v1/cases/{case_id}/documents?limit=2&cursor={cursor}"
        )
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1
        assert {item["document_id"] for item in first.json()["items"]}.isdisjoint(
            {item["document_id"] for item in second.json()["items"]}
        )
        assert {item["document_id"] for item in first.json()["items"] + second.json()["items"]} == set(document_ids)
        invalid = client.get(f"/api/v1/cases/{case_id}/documents?cursor=not-a-cursor")
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "INVALID_CURSOR"


def test_other_owner_cannot_read_or_mutate_case_document_or_job(tmp_path, pg_engine) -> None:
    with _client(tmp_path, owner_id=OWNER_A) as (owner_a, app_a):
        case = _create_case(owner_a, "Owned by A")
        case_id = case["case_id"]
        uploaded = _upload(owner_a, case_id, idempotency_key="a-upload")
        assert uploaded.status_code == 202
        doc_id = uploaded.json()["document"]["document_id"]
        job_id = uploaded.json()["job"]["job_id"]

        with _client(tmp_path, owner_id=OWNER_B) as (owner_b, _app_b):
            cases = owner_b.get("/api/v1/cases")
            assert cases.status_code == 200
            assert cases.json()["items"] == []
            for response in (
                owner_b.get(f"/api/v1/cases/{case_id}"),
                owner_b.get(f"/api/v1/cases/{case_id}/documents"),
                owner_b.get(f"/api/v1/cases/{case_id}/documents/{doc_id}"),
                owner_b.get(f"/api/v1/cases/{case_id}/documents/{doc_id}/content"),
                owner_b.get(f"/api/v1/cases/{case_id}/jobs/{job_id}"),
                _upload(owner_b, case_id),
                owner_b.delete(f"/api/v1/cases/{case_id}"),
            ):
                assert response.status_code == 404
                assert response.headers["content-type"].startswith("application/problem+json")
                assert response.json()["code"] == "NOT_FOUND"
                assert str(tmp_path) not in response.text

            # Identical bytes under a different owner/case get an independent document/blob.
            case_b = _create_case(owner_b, "Owned by B")
            same_bytes = _upload(owner_b, case_b["case_id"], data=_pdf_bytes(), role="bill")
            assert same_bytes.status_code == 202
            assert same_bytes.json()["document"]["document_id"] != doc_id

        # An inaccessible DELETE must not change the legitimate owner's case.
        assert owner_a.get(f"/api/v1/cases/{case_id}").status_code == 200


def test_pdf_intake_rejections_are_audited_without_echoing_input(tmp_path, pg_engine) -> None:
    with _client(tmp_path, max_upload_bytes=1024, max_request_bytes=4096) as (client, app):
        case_id = _create_case(client)["case_id"]

        forged = _upload(client, case_id, data=b"not a pdf", filename="secret-claim.pdf")
        assert forged.status_code == 415
        assert forged.json()["code"] == "UNSUPPORTED_FILE_TYPE"
        assert "secret-claim" not in forged.text

        corrupt = _upload(client, case_id,
                          data=b"%PDF-1.4\nnot a valid cross-reference table", filename="bad.pdf")
        assert corrupt.status_code == 422
        assert corrupt.json()["code"] == "CORRUPT_PDF"

        wrong_extension = _upload(client, case_id, filename="bill.txt")
        assert wrong_extension.status_code == 415

        oversize_file = _upload(client, case_id, data=_pdf_bytes() + b" " * 1500)
        assert oversize_file.status_code == 413
        assert oversize_file.json()["code"] == "UPLOAD_TOO_LARGE"

        oversize_request = _upload(client, case_id, data=_pdf_bytes() + b" " * 6000)
        assert oversize_request.status_code == 413
        assert oversize_request.json()["code"] == "UPLOAD_TOO_LARGE"

        with Session(pg_engine) as session:
            events = list(session.scalars(select(AuditEvent).where(
                AuditEvent.case_id == UUID(case_id),
                AuditEvent.event_type == "DOCUMENT_REJECTED",
            )))
            assert len(events) == 5
            assert {event.result_code for event in events} >= {
                "UNSUPPORTED_FILE_TYPE", "CORRUPT_PDF", "UPLOAD_TOO_LARGE"
            }
            assert all("secret-claim" not in str(event.event_data) for event in events)
            assert list(session.scalars(select(CaseDocument).where(
                CaseDocument.case_id == UUID(case_id)
            ))) == []


def test_case_deletion_removes_files_tombstones_data_and_is_idempotent(tmp_path, pg_engine) -> None:
    with _client(tmp_path) as (client, app):
        case_id = _create_case(client, "Delete me")["case_id"]
        uploaded = _upload(client, case_id, filename="patient bill.pdf")
        assert uploaded.status_code == 202
        document_id = uploaded.json()["document"]["document_id"]
        job_id = uploaded.json()["job"]["job_id"]
        content_path = f"/api/v1/cases/{case_id}/documents/{document_id}/content"
        assert client.get(content_path).content == _pdf_bytes()

        with Session(pg_engine) as session:
            document = session.get(CaseDocument, UUID(document_id))
            storage_key = document.storage_key
            assert storage_key is not None
            assert "patient bill.pdf" not in storage_key
            assert len(storage_key.rsplit("/", 1)[-1].removesuffix(".pdf")) >= 40

        analysis_run_id = uuid4()
        with Session(pg_engine, expire_on_commit=False) as session, session.begin():
            case_row = session.get(Case, UUID(case_id))
            case_row.latest_analysis_run_id = analysis_run_id
            session.add(AnalysisRun(
                analysis_run_id=analysis_run_id,
                case_id=UUID(case_id),
                owner_id=OWNER_A,
                run_number=1,
                status="QUEUED",
            ))

        deletion = client.delete(f"/api/v1/cases/{case_id}")
        assert deletion.status_code == 200, deletion.text
        result = deletion.json()
        assert result["status"] == "DELETED"
        assert result["deletion_job_id"]
        assert app.state.storage.exists(storage_key) is False

        repeated = client.delete(f"/api/v1/cases/{case_id}")
        assert repeated.status_code == 200
        assert repeated.json()["deletion_job_id"] == result["deletion_job_id"]
        for response in (
            client.get(f"/api/v1/cases/{case_id}"),
            client.get(f"/api/v1/cases/{case_id}/documents"),
            client.get(f"/api/v1/cases/{case_id}/documents/{document_id}"),
            client.get(content_path),
            client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}"),
        ):
            assert response.status_code == 404

        with Session(pg_engine) as session:
            case = session.get(Case, UUID(case_id))
            document = session.get(CaseDocument, UUID(document_id))
            upload_job = session.get(ProcessingJob, UUID(job_id))
            deletion_job = session.get(ProcessingJob, UUID(result["deletion_job_id"]))
            assert session.get(AnalysisRun, analysis_run_id) is None
            assert case.status == "DELETED"
            assert case.deletion_status == "DELETED"
            assert document.state == "DELETED"
            assert document.storage_key is None
            assert document.original_filename == "deleted.pdf"
            assert document.sha256 == "0" * 64
            assert document.byte_size == 0
            assert document.page_count == 0
            assert case.display_name is None
            assert case.claim_date is None
            assert upload_job.status == "CANCELLED"
            assert deletion_job.status == "SUCCEEDED"
            events = list(session.scalars(select(AuditEvent).where(AuditEvent.case_id == UUID(case_id))))
            event_types = {event.event_type for event in events}
            assert {"DOCUMENT_DELETED", "CASE_DELETED", "JOB_CANCELLED", "JOB_COMPLETED"} <= event_types
            case_deleted = next(event for event in events if event.event_type == "CASE_DELETED")
            assert case_deleted.event_data["analysis_run_count"] == 1
            for event in events:
                serialized = str(event.event_data)
                assert "patient bill.pdf" not in serialized
                assert "storage_key" not in serialized
                assert "%PDF" not in serialized
            first_event_id = events[0].event_id

        # A database trigger, not only repository convention, prevents audit mutation.
        with pytest.raises(SQLAlchemyError):
            with pg_engine.begin() as connection:
                connection.execute(
                    update(AuditEvent).where(AuditEvent.event_id == first_event_id)
                    .values(result_code="TAMPERED")
                )


def test_failed_storage_cleanup_is_private_and_retryable(tmp_path, pg_engine) -> None:
    storage = _FailOnceStorage(tmp_path / "private-with-one-failure")
    with _client(tmp_path, storage=storage) as (client, app):
        case_id = _create_case(client, "Retry deletion")["case_id"]
        uploaded = _upload(client, case_id)
        assert uploaded.status_code == 202
        document_id = UUID(uploaded.json()["document"]["document_id"])
        with Session(pg_engine) as session:
            storage_key = session.get(CaseDocument, document_id).storage_key

        failed = client.delete(f"/api/v1/cases/{case_id}")
        assert failed.status_code == 503
        assert failed.json()["code"] == "STORAGE_DELETE_FAILED"
        assert failed.json()["retryable"] is True
        assert client.get(f"/api/v1/cases/{case_id}").status_code == 404
        assert storage.exists(storage_key) is True

        with Session(pg_engine) as session:
            case_row = session.get(Case, UUID(case_id))
            deletion_job = session.scalar(select(ProcessingJob).where(
                ProcessingJob.case_id == UUID(case_id),
                ProcessingJob.job_type == "CASE_DELETION",
            ))
            assert case_row.deletion_status == "DELETE_PENDING"
            assert deletion_job.status == "RETRYABLE_FAILURE"
            assert deletion_job.error_code == "STORAGE_DELETE_FAILED"

        retried = client.delete(f"/api/v1/cases/{case_id}")
        assert retried.status_code == 200
        assert retried.json()["status"] == "DELETED"
        assert storage.exists(storage_key) is False
        with Session(pg_engine) as session:
            event_types = set(session.scalars(select(AuditEvent.event_type).where(
                AuditEvent.case_id == UUID(case_id)
            )))
            assert "JOB_FAILED" in event_types
            assert "JOB_COMPLETED" in event_types


def test_job_claim_is_atomic_durable_and_does_not_run_processing(tmp_path, pg_engine) -> None:
    with _client(tmp_path) as (client, app):
        case_id = _create_case(client)["case_id"]
        upload = _upload(client, case_id)
        assert upload.status_code == 202
        job_id = UUID(upload.json()["job"]["job_id"])

        with Session(pg_engine, expire_on_commit=False) as session, session.begin():
            job = JobClaimRepository(session, WorkerIdentity("test-worker")).claim_next()
            assert job is not None
            assert job.job_id == job_id
            assert job.status == JobStatus.RUNNING.value
            assert job.attempt_count == 1
            assert job.lease_owner == "test-worker"
            assert job.lease_expires_at is not None

        with Session(pg_engine, expire_on_commit=False) as session, session.begin():
            assert JobClaimRepository(session, WorkerIdentity("other-worker")).claim_next() is None

        # Claiming changes only the durable job lease; the handler has not run.
        with Session(pg_engine) as session:
            stored = session.get(ProcessingJob, job_id)
            assert stored.status == "RUNNING"
            assert stored.payload_json["document_id"] == upload.json()["document"]["document_id"]
            assert UUID(stored.payload_json["processing_run_id"]).version == 4
            assert set(stored.payload_json) == {"document_id", "processing_run_id"}
