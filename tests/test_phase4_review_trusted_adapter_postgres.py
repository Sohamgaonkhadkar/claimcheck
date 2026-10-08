"""PostgreSQL Phase 4 review/trust and Phase 5 persisted-analysis integration tests.

Set CLAIMCHECK_TEST_DATABASE_URL to a disposable PostgreSQL database. The fixture applies the
checked-in Alembic head and truncates that dedicated test database between tests.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from claimcheck.api.app import create_app
from claimcheck.api.config import AppSettings
from claimcheck.application.errors import ApplicationError
from claimcheck.application.identity import OwnerIdentity
from claimcheck.application.trusted_case import TrustedCaseAdapter
from claimcheck.persistence.database import create_postgres_engine
from claimcheck.persistence.models import (
    Case,
    CaseDocument,
    CaseInputRevision,
    DocumentEvidenceSpan,
    DocumentProcessingField,
    DocumentProcessingRecord,
    DocumentProcessingRun,
    DocumentRoleAssignment,
    ProcessingJob,
    ReviewCorrection,
)
from claimcheck.schema import CanonicalCategory, MappingSource, Origin
from claimcheck.worker.runner import DocumentWorker, WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("CLAIMCHECK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set CLAIMCHECK_TEST_DATABASE_URL to a disposable PostgreSQL database for Phase 4/5 integration tests.",
)
OWNER_A = UUID("b7748770-2971-4fc0-8cda-b0193275f391")
OWNER_B = UUID("69e28f0a-cf4e-438b-971d-f6357e3b348f")


def _pdf_bytes(lines: list[str]) -> bytes:
    """Build a one-page synthetic text PDF entirely in memory."""
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
def _client(tmp_path: Path, *, owner_id: UUID = OWNER_A):
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


def _create_case(client: TestClient) -> UUID:
    response = client.post("/api/v1/cases", json={"display_name": "Phase 4 synthetic"})
    assert response.status_code == 201, response.text
    return UUID(response.json()["case_id"])


def _upload(client: TestClient, case_id: UUID, role: str, content: bytes) -> dict:
    response = client.post(
        f"/api/v1/cases/{case_id}/documents",
        files={"file": (f"{role}.pdf", content, "application/pdf")},
        data={"role": role},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _worker(pg_engine, app, *, worker_id: str) -> DocumentWorker:
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


def _post_correction(client: TestClient, case_id: UUID, *, document_id: UUID,
                     processing_run_id: UUID, field_path: str, action: str,
                     verification_method: str, corrected_value=None,
                     evidence_id: UUID | None = None, page_number: int | None = None):
    response = client.post(
        f"/api/v1/cases/{case_id}/review-corrections",
        json={
            "document_id": str(document_id),
            "processing_run_id": str(processing_run_id),
            "field_path": field_path,
            "action": action,
            "corrected_value": corrected_value,
            "evidence_id": str(evidence_id) if evidence_id is not None else None,
            "verification_method": verification_method,
            "page_number": page_number,
        },
    )
    return response


def _correction_value(*, action: str, corrected_value=None, evidence_id=None,
                      verification_method: str, page_number=None) -> dict:
    return {
        "action": action,
        "corrected_value": corrected_value,
        "evidence_id": evidence_id,
        "verification_method": verification_method,
        "page_number": page_number,
    }


def test_upload_worker_review_adapter_boundary_and_persisted_analysis(
        pg_engine, tmp_path, monkeypatch):
    policy_text = (
        "This policy wording describes the coverage and exclusions that apply to eligible medical "
        "expenses. The insurer will assess the claim based on the records submitted by the "
        "policyholder. Benefits are subject to the terms, limits, conditions, waiting periods, "
        "and definitions set out in this wording. The policyholder should retain each original "
        "bill, receipt, and communication that supports a request for reimbursement."
    )
    documents = {
        "policy_wording": _pdf_bytes(["POLICY WORDING", "General Conditions", policy_text]),
        "policy_schedule": _pdf_bytes([
            "Policy Schedule", "Policy Number 123456", "Sum Insured Rs 500000.00",
        ]),
        "bill": _pdf_bytes([
            "Hospital Bill", "01/01/2026 Room Charge 100.00", "Grand Total 100.00",
        ]),
        "settlement": _pdf_bytes([
            "Insurer Statement",
            "Settlement claimed 100.00; deducted non-payable 20.00; final payable 80.00; "
            "partial disallowance yes; not repudiated",
        ]),
    }

    with _client(tmp_path) as (client, app):
        case_id = _create_case(client)
        uploaded = {
            role: _upload(client, case_id, role, content)
            for role, content in documents.items()
        }
        ids = {role: UUID(result["document"]["document_id"])
               for role, result in uploaded.items()}
        assert all(result["document"]["selected_for_role"] for result in uploaded.values())
        worker = _worker(pg_engine, app, worker_id="phase4-adapter-worker")
        assert all(worker.run_once() is True for _ in range(4))
        assert worker.run_once() is False

        before_review = client.get(f"/api/v1/cases/{case_id}/readiness")
        assert before_review.status_code == 200, before_review.text
        readiness = before_review.json()
        assert readiness["status"] == "NEEDS_REVIEW"
        assert readiness["ready_for_analysis"] is False
        assert any(gap["field_path"] == "policy.sum_insured_paise"
                   for gap in readiness["gaps"])
        assert any(gap["field_path"] and gap["field_path"].endswith(".category")
                   for gap in readiness["gaps"])

        queue_response = client.get(f"/api/v1/cases/{case_id}/review-queue")
        assert queue_response.status_code == 200, queue_response.text
        queue = queue_response.json()
        assert queue["status"] == "NEEDS_REVIEW"
        assert any(warning["code"] == "POLICY_SCHEDULE_PARSER_UNAVAILABLE"
                   for warning in queue["warnings"])
        assert not any(item["code"] == "POLICY_SCHEDULE_PARSER_UNAVAILABLE"
                       for item in queue["items"])

        # Owner scoping is enforced for both read APIs before any review data is returned.
        with _client(tmp_path, owner_id=OWNER_B) as (other, _other_app):
            assert other.get(f"/api/v1/cases/{case_id}/readiness").status_code == 404
            assert other.get(f"/api/v1/cases/{case_id}/review-queue").status_code == 404

        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            runs = {
                role: session.scalar(select(DocumentProcessingRun).where(
                    DocumentProcessingRun.owner_id == OWNER_A,
                    DocumentProcessingRun.case_id == case_id,
                    DocumentProcessingRun.document_id == document_id,
                ))
                for role, document_id in ids.items()
            }
            assert all(run is not None for run in runs.values())
            run_ids = {role: run.processing_run_id for role, run in runs.items()}
            bill_total_record = session.scalar(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == OWNER_A,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == ids["bill"],
                DocumentProcessingRecord.processing_run_id == run_ids["bill"],
                DocumentProcessingRecord.record_type == "BILL_TOTAL",
            ))
            bill_line_records = list(session.scalars(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == OWNER_A,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == ids["bill"],
                DocumentProcessingRecord.processing_run_id == run_ids["bill"],
                DocumentProcessingRecord.record_type == "BILL_LINE",
                DocumentProcessingRecord.field_state == "VERIFIED",
            ).order_by(DocumentProcessingRecord.record_key)))
            bill_line_record = next(
                row for row in bill_line_records
                if not bool((row.record_json or {}).get("is_summary_row"))
            )
            bill_amount = session.scalar(select(DocumentProcessingField).where(
                DocumentProcessingField.owner_id == OWNER_A,
                DocumentProcessingField.case_id == case_id,
                DocumentProcessingField.document_id == ids["bill"],
                DocumentProcessingField.processing_run_id == run_ids["bill"],
                DocumentProcessingField.field_path ==
                    f"bill.line.{bill_line_record.record_key}.amount_paise",
                DocumentProcessingField.state == "VERIFIED",
            ))
            settlement_line_record = session.scalar(select(DocumentProcessingRecord).where(
                DocumentProcessingRecord.owner_id == OWNER_A,
                DocumentProcessingRecord.case_id == case_id,
                DocumentProcessingRecord.document_id == ids["settlement"],
                DocumentProcessingRecord.processing_run_id == run_ids["settlement"],
                DocumentProcessingRecord.record_type == "SETTLEMENT_LINE",
            ))
            wrong_scope_evidence = session.scalar(select(DocumentEvidenceSpan.evidence_id).where(
                DocumentEvidenceSpan.owner_id == OWNER_A,
                DocumentEvidenceSpan.case_id == case_id,
                DocumentEvidenceSpan.document_id == ids["policy_wording"],
                DocumentEvidenceSpan.verification_status == "VERIFIED",
            ).limit(1))
            assert bill_amount is not None and bill_amount.value_json == 10_000
            assert bill_total_record is not None and bill_total_record.evidence_id is not None
            assert bill_line_record is not None
            assert settlement_line_record is not None
            assert wrong_scope_evidence is not None
            raw_bill_field_before = (
                bill_amount.processing_field_id, bill_amount.value_json,
                bill_amount.state, bill_amount.evidence_id,
            )
            bill_amount_path = bill_amount.field_path
            bill_category_path = f"bill.line.{bill_line_record.record_key}.category"
            settlement_amount_path = \
                f"settlement.line.{settlement_line_record.record_key}.amount_paise"
            settlement_head_path = \
                f"settlement.line.{settlement_line_record.record_key}.head"
            total_evidence_id = bill_total_record.evidence_id
            line_evidence_id = bill_amount.evidence_id

        # A verified span from another document cannot support a bill correction, even when
        # both documents are in the same owner/case and the source bytes are readable.
        wrong_evidence = _post_correction(
            client, case_id,
            document_id=ids["bill"], processing_run_id=run_ids["bill"],
            field_path=bill_amount_path, action="CONFIRM",
            verification_method="EVIDENCE_SPAN", evidence_id=wrong_scope_evidence,
        )
        assert wrong_evidence.status_code == 422
        assert wrong_evidence.json()["code"] == "EVIDENCE_NOT_TIED_TO_FIELD"

        # UNRESOLVED is append-only and explicitly keeps a required value untrusted.
        unresolved = _post_correction(
            client, case_id,
            document_id=ids["bill"], processing_run_id=run_ids["bill"],
            field_path="bill.total_paise", action="UNRESOLVED",
            verification_method="UNRESOLVED",
        )
        assert unresolved.status_code == 201, unresolved.text
        assert unresolved.json()["correction"]["action"] == "UNRESOLVED"
        assert unresolved.json()["readiness"]["status"] == "NEEDS_REVIEW"

        # Each trusted correction binds the selected source, immutable processing run, field or
        # parser-record reference, source hash, and either a supporting verified span or an
        # explicit human visual page. Visual checks never fabricate a quote/evidence ID.
        visual_corrections = (
            ("policy_schedule", "policy.sum_insured_paise", 50_000_000),
            ("bill", bill_category_path, CanonicalCategory.ROOM.value),
            ("settlement", "settlement.claimed_amount_paise", 10_000),
            ("settlement", "settlement.final_payable_paise", 8_000),
            ("settlement", "settlement.partial_disallowance", True),
            ("settlement", "settlement.repudiated", False),
            ("settlement", settlement_amount_path, 2_000),
            ("settlement", settlement_head_path, "non_payable"),
        )
        for role, field_path, value in visual_corrections:
            response = _post_correction(
                client, case_id,
                document_id=ids[role], processing_run_id=run_ids[role],
                field_path=field_path, action="CORRECT", corrected_value=value,
                verification_method="HUMAN_VISUAL", page_number=1,
            )
            assert response.status_code == 201, response.text
            assert response.json()["correction"]["verification_method"] == "HUMAN_VISUAL"
            assert response.json()["correction"]["page_number"] == 1
            assert response.json()["correction"]["evidence_id"] is None

        # Bill total is explicitly bound to the typed BILL_TOTAL record and its verified span.
        bill_total_correction = _post_correction(
            client, case_id,
            document_id=ids["bill"], processing_run_id=run_ids["bill"],
            field_path="bill.total_paise", action="CORRECT", corrected_value=10_000,
            verification_method="EVIDENCE_SPAN", evidence_id=total_evidence_id,
        )
        assert bill_total_correction.status_code == 201, bill_total_correction.text

        # CONFIRM uses the original verified processing candidate, without editing that row.
        confirm_amount = _post_correction(
            client, case_id,
            document_id=ids["bill"], processing_run_id=run_ids["bill"],
            field_path=bill_amount_path, action="CONFIRM",
            verification_method="EVIDENCE_SPAN", evidence_id=line_evidence_id,
        )
        assert confirm_amount.status_code == 201, confirm_amount.text

        after_review = client.get(f"/api/v1/cases/{case_id}/readiness")
        assert after_review.status_code == 200, after_review.text
        assert after_review.json()["status"] == "READY_FOR_ANALYSIS", after_review.json()
        assert after_review.json()["ready_for_analysis"] is True
        assert any(gap["code"] == "POLICY_SCHEDULE_PARSER_UNAVAILABLE"
                   and gap["blocking"] is False for gap in after_review.json()["gaps"])

        # Raw extraction candidates remain exactly as persisted; corrections are separate rows.
        with factory() as session:
            raw_bill_field_after = session.get(DocumentProcessingField,
                                               raw_bill_field_before[0])
            assert raw_bill_field_after is not None
            assert (raw_bill_field_after.processing_field_id,
                    raw_bill_field_after.value_json,
                    raw_bill_field_after.state,
                    raw_bill_field_after.evidence_id) == raw_bill_field_before
            corrections = list(session.scalars(select(ReviewCorrection).where(
                ReviewCorrection.owner_id == OWNER_A,
                ReviewCorrection.case_id == case_id,
            ).order_by(ReviewCorrection.field_path, ReviewCorrection.correction_number)))
            assert len(corrections) == len(visual_corrections) + 3
            total_versions = [row for row in corrections if row.field_path == "bill.total_paise"]
            assert [row.action for row in total_versions] == ["UNRESOLVED", "CORRECT"]
            assert all(row.source_sha256 == session.get(
                CaseDocument, row.document_id
            ).sha256 for row in corrections)
            visual_sum_insured = next(row for row in corrections
                                      if row.field_path == "policy.sum_insured_paise")
            assert visual_sum_insured.verification_method == "HUMAN_VISUAL"
            assert visual_sum_insured.evidence_id is None
            assert visual_sum_insured.page_number == 1

            # Database triggers enforce append-only revision, role and correction history.
            revision_id = session.scalar(select(CaseInputRevision.revision_id).where(
                CaseInputRevision.owner_id == OWNER_A,
                CaseInputRevision.case_id == case_id,
            ).limit(1))
            assignment_id = session.scalar(select(DocumentRoleAssignment.assignment_id).where(
                DocumentRoleAssignment.owner_id == OWNER_A,
                DocumentRoleAssignment.case_id == case_id,
            ).limit(1))
            correction_id = corrections[0].correction_id
        for model, key_column, key_value, values in (
            (CaseInputRevision, CaseInputRevision.revision_id, revision_id,
             {"reason_code": "DOCUMENT_UPLOADED"}),
            (DocumentRoleAssignment, DocumentRoleAssignment.assignment_id, assignment_id,
             {"reason_code": "OTHER_REVIEW_REASON"}),
            (ReviewCorrection, ReviewCorrection.correction_id, correction_id,
             {"reason_code": "OTHER_REVIEW_REASON"}),
        ):
            with pytest.raises(DBAPIError):
                with factory() as session, session.begin():
                    session.execute(update(model).where(key_column == key_value).values(**values))

        # A tampered current PDF is rejected at the adapter boundary even though all stored
        # candidates and corrections still carry the upload-time digest.
        with factory() as session:
            wording_document = session.get(CaseDocument, ids["policy_wording"])
            wording_storage_key = wording_document.storage_key
        
        principal = OwnerIdentity(
            owner_id=OWNER_A, identity_provider="development", subject=str(OWNER_A),
            auth_mode="development",
        )

        original_storage_get = app.state.storage.get
        with monkeypatch.context() as scoped:
            def tampered_source(key):
                if key == wording_storage_key:
                    return b"tampered bytes, not the persisted PDF"
                return original_storage_get(key)

            scoped.setattr(app.state.storage, "get", tampered_source)
            with factory() as session:
                with pytest.raises(ApplicationError) as rejected_source:
                    TrustedCaseAdapter(session, app.state.storage, principal).build(case_id)
            assert rejected_source.value.code == "SOURCE_HASH_MISMATCH"

        # The adapter is deliberately not an analysis entry point. It may only construct inputs.
        import claimcheck.pipeline as pipeline_module

        def forbidden_analysis(*_args, **_kwargs):
            pytest.fail("Phase 4 adapter attempted to run pipeline analysis")

        monkeypatch.setattr(pipeline_module, "run_case", forbidden_analysis)
        with factory() as session:
            trusted_case = TrustedCaseAdapter(session, app.state.storage, principal).build(case_id)

        assert trusted_case.case_id == str(case_id)
        assert trusted_case.origin == "user_upload"
        assert set(trusted_case.documents) == {
            "policy_wording", "policy_schedule", "bill", "settlement",
        }
        assert trusted_case.policy.sum_insured.as_money() == 50_000_000
        sum_insured_fact = trusted_case.facts.get("policy.sum_insured")
        assert sum_insured_fact is not None
        assert sum_insured_fact.origin is Origin.USER
        assert sum_insured_fact.evidence_id is None
        assert sum_insured_fact.raw_text is None
        assert "HUMAN_VISUAL" in sum_insured_fact.note
        assert "page 1" in sum_insured_fact.note
        assert trusted_case.bill.bill_total == 10_000
        assert len(trusted_case.bill.lines) == 1
        assert trusted_case.bill.lines[0].category is CanonicalCategory.ROOM
        assert trusted_case.bill.lines[0].mapping_source is MappingSource.USER_OVERRIDE
        assert trusted_case.settlement.claimed_amount == 10_000
        assert trusted_case.settlement.final_payable == 8_000
        assert trusted_case.settlement.partial_disallowance is True
        assert trusted_case.settlement.repudiated is False
        assert [(item.head, item.amount) for item in trusted_case.settlement.deductions] == [
            ("non_payable", 2_000),
        ]
        assert trusted_case.policy.clauses
        assert any("policy_schedule parser remains unavailable" in note
                   for note in trusted_case.history_notes)
        assert any("HUMAN_VISUAL" in note and "source page 1" in note
                   for note in trusted_case.history_notes)
        assert trusted_case.input_revision == after_review.json()["input_revision"]
        assert trusted_case.corpus_snapshot_id.endswith(
            f"-{after_review.json()['input_revision']}"
        )

        # The adapter above did not enter analysis. The production case endpoint is the only
        # bridge that now calls the deterministic pipeline and persists its safe result.
        monkeypatch.undo()
        analyzed = client.post(f"/api/v1/cases/{case_id}/analyze")
        assert analyzed.status_code == 201, analyzed.text
        first_run = analyzed.json()["analysis_run"]
        assert first_run["status"] == "SUCCEEDED", analyzed.text
        assert first_run["input_revision"] == trusted_case.input_revision
        assert first_run["result"]["verdict"]["state"] in {
            "CONSISTENT", "POTENTIALLY_INCONSISTENT", "UNDETERMINED",
        }
        assert first_run["result"]["rulepack_version"]
        assert policy_text not in analyzed.text
        assert "raw_text" not in analyzed.text
        assert "quoted_text" not in analyzed.text
        assert "storage_key" not in analyzed.text
        case_view = client.get(f"/api/v1/cases/{case_id}").json()
        assert case_view["status"] == "ANALYZED"
        assert case_view["latest_analysis_run_id"] == first_run["analysis_run_id"]
        run_list = client.get(f"/api/v1/cases/{case_id}/analysis")
        assert run_list.status_code == 200, run_list.text
        assert [row["analysis_run_id"] for row in run_list.json()["items"]] == [
            first_run["analysis_run_id"],
        ]
        run_detail = client.get(
            f"/api/v1/cases/{case_id}/analysis/{first_run['analysis_run_id']}"
        )
        assert run_detail.status_code == 200, run_detail.text
        assert run_detail.json()["analysis_run"]["result"] == first_run["result"]

        with _client(tmp_path, owner_id=OWNER_B) as (other, _other_app):
            assert other.get(f"/api/v1/cases/{case_id}/analysis").status_code == 404
            assert other.get(
                f"/api/v1/cases/{case_id}/analysis/{first_run['analysis_run_id']}"
            ).status_code == 404
            assert other.post(f"/api/v1/cases/{case_id}/analyze").status_code == 404

        # A second run is a new immutable historical row. Failure is safe and does not expose
        # the exception text or mark the current case ANALYZED.
        import claimcheck.application.analysis as analysis_module

        def fail_analysis(*_args, **_kwargs):
            raise RuntimeError("private stack sentinel")

        monkeypatch.setattr(analysis_module, "run_case", fail_analysis)
        failed = client.post(f"/api/v1/cases/{case_id}/analyze")
        assert failed.status_code == 201, failed.text
        failed_run = failed.json()["analysis_run"]
        assert failed_run["analysis_run_id"] != first_run["analysis_run_id"]
        assert failed_run["run_number"] == first_run["run_number"] + 1
        assert failed_run["status"] == "FAILED"
        assert failed_run["error_code"] == "ANALYSIS_FAILED"
        assert failed_run["result"] is None
        assert "private stack sentinel" not in failed.text
        assert client.get(f"/api/v1/cases/{case_id}").json()["status"] == "FAILED"

        # Later input edits do not rewrite either historical analysis snapshot.
        edited = client.patch(
            f"/api/v1/cases/{case_id}", json={"claim_date": "2026-10-06"}
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["input_revision"] > failed_run["input_revision"]
        assert edited.json()["status"] == "READY_FOR_ANALYSIS"
        history_response = client.get(f"/api/v1/cases/{case_id}/analysis")
        history = history_response.json()["items"]
        assert len(history) == 2
        assert history_response.json()["current_input_revision"] > failed_run["input_revision"]
        assert {row["input_revision"] for row in history} == {
            first_run["input_revision"],
        }


def test_role_candidates_are_not_silently_reselected_and_role_reprocessing_is_immutable(
        pg_engine, tmp_path):
    data_first = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 100.00"])
    data_second = _pdf_bytes(["Hospital Bill", "01/01/2026 Room Charge 200.00"])
    with _client(tmp_path) as (client, app):
        case_id = _create_case(client)
        initial = client.get(f"/api/v1/cases/{case_id}/readiness").json()
        assert initial["status"] == "ROLE_GAP"
        assert {gap["role"] for gap in initial["gaps"] if gap["code"] == "ROLE_SOURCE_NOT_ASSIGNED"} == {
            "policy_wording", "policy_schedule", "bill", "settlement",
        }
        blocked_analysis = client.post(f"/api/v1/cases/{case_id}/analyze")
        assert blocked_analysis.status_code == 409
        assert blocked_analysis.json()["code"] == "TRUSTED_INPUT_NOT_READY"
        assert client.get(f"/api/v1/cases/{case_id}/analysis").json()["items"] == []

        first = _upload(client, case_id, "bill", data_first)
        second = _upload(client, case_id, "bill", data_second)
        first_id = UUID(first["document"]["document_id"])
        second_id = UUID(second["document"]["document_id"])
        assert first["document"]["selected_for_role"] is True
        assert second["document"]["selected_for_role"] is False

        worker = _worker(pg_engine, app, worker_id="phase4-role-worker")
        assert worker.run_once() is True
        assert worker.run_once() is True
        assert worker.run_once() is False
        selected = client.get(f"/api/v1/cases/{case_id}/readiness").json()
        assert selected["status"] == "ROLE_GAP"
        assert selected["selected_roles"]["bill"] == str(first_id)
        assert any(gap["code"] == "UNSELECTED_ROLE_CANDIDATES_REMAIN"
                   and gap["blocking"] is False for gap in selected["gaps"])

        # A foreign principal cannot enumerate role history or source selections.
        with _client(tmp_path, owner_id=OWNER_B) as (other, _other_app):
            assert other.get(f"/api/v1/cases/{case_id}/readiness").status_code == 404
            assert other.get(f"/api/v1/cases/{case_id}/review-queue").status_code == 404

        unassign = client.post(
            f"/api/v1/cases/{case_id}/document-role-assignments",
            json={"role": "bill", "document_id": None, "confirm_mismatch": False},
        )
        assert unassign.status_code == 201, unassign.text
        after_unassign = client.get(f"/api/v1/cases/{case_id}/readiness").json()
        assert after_unassign["selected_roles"]["bill"] is None
        assert after_unassign["status"] == "ROLE_GAP"

        with sessionmaker(bind=pg_engine, expire_on_commit=False)() as session:
            document = session.get(CaseDocument, first_id)
            original_key = document.storage_key
            original_sha = document.sha256
        schedule_assignment = client.post(
            f"/api/v1/cases/{case_id}/document-role-assignments",
            json={
                "role": "policy_schedule",
                "document_id": str(first_id),
                "confirm_mismatch": True,
                "reason_code": "HUMAN_VISUAL_CHECK",
            },
        )
        assert schedule_assignment.status_code == 201, schedule_assignment.text
        assert schedule_assignment.json()["job"]["job_type"] == "DOCUMENT_PROCESS"
        assert schedule_assignment.json()["assignment"]["resolved_mismatch"] is True
        assert worker.run_once() is True

        with sessionmaker(bind=pg_engine, expire_on_commit=False)() as session:
            runs = list(session.scalars(select(DocumentProcessingRun).where(
                DocumentProcessingRun.owner_id == OWNER_A,
                DocumentProcessingRun.case_id == case_id,
                DocumentProcessingRun.document_id == first_id,
            ).order_by(DocumentProcessingRun.run_number)))
            document = session.get(CaseDocument, first_id)
            assignments = list(session.scalars(select(DocumentRoleAssignment).where(
                DocumentRoleAssignment.owner_id == OWNER_A,
                DocumentRoleAssignment.case_id == case_id,
                DocumentRoleAssignment.role == "policy_schedule",
            ).order_by(DocumentRoleAssignment.input_revision)))
        assert [run.assigned_role for run in runs] == ["bill", "policy_schedule"]
        assert runs[0].processing_run_id != runs[1].processing_run_id
        assert runs[1].source_sha256 == original_sha == document.sha256
        assert document.storage_key == original_key
        assert app.state.storage.get(original_key) == data_first
        assert [row.document_id for row in assignments] == [first_id]

        final_readiness = client.get(f"/api/v1/cases/{case_id}/readiness").json()
        assert final_readiness["selected_roles"]["policy_schedule"] == str(first_id)
        assert final_readiness["selected_roles"]["bill"] is None
        assert any(gap["code"] == "ROLE_MISMATCH_EXPLICITLY_RESOLVED"
                   and gap["blocking"] is False for gap in final_readiness["gaps"])
        assert second_id != first_id
