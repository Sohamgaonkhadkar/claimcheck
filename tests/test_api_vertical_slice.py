"""Focused HTTP-boundary tests for the deterministic GOLDEN-001 vertical slice."""
from __future__ import annotations

from fastapi.testclient import TestClient

from claimcheck.api.app import app, create_app
from claimcheck.api.config import AppSettings
from claimcheck.api.schemas import GoldenDemoResponse
from claimcheck.cases.golden_001 import build_case
from claimcheck.explain.report import build_report
from claimcheck.pipeline import run_case


def _development_app(service=None):
    return create_app(AppSettings(environment="development"), golden_demo_service=service)


def test_app_import_startup_and_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_ready_endpoint_is_minimal_and_safe() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert "environment" not in response.text.lower()
    assert "/home/" not in response.text


def test_golden_route_runs_only_in_explicit_development_mode() -> None:
    production_app = create_app(AppSettings(environment="production"))
    with TestClient(production_app) as client:
        response = client.get("/api/v1/dev/golden-001")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert "golden_001" not in response.text.lower()


def test_golden_route_returns_the_typed_synthetic_response() -> None:
    with TestClient(_development_app()) as client:
        response = client.get("/api/v1/dev/golden-001")
    assert response.status_code == 200
    payload = GoldenDemoResponse.model_validate(response.json())
    assert payload.case_id == "GOLDEN-001"
    assert payload.run_id == "RUN-GOLDEN-001"
    assert payload.synthetic_demo is True
    assert "Synthetic demonstration" in payload.synthetic_notice
    assert payload.overall_result.state == "POTENTIALLY_INCONSISTENT"
    assert payload.financial_findings
    assert payload.procedural_findings
    assert payload.evidence_gaps
    assert payload.evidence_references
    assert payload.reconciliation.identity_ok is True
    assert payload.calculation_steps


def test_response_preserves_existing_report_values_and_is_deterministic() -> None:
    with TestClient(_development_app()) as client:
        first = client.get("/api/v1/dev/golden-001")
        second = client.get("/api/v1/dev/golden-001")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    report = build_report(run_case(build_case()))
    payload = first.json()
    assert payload["overall_result"]["state"] == report["verdict"]["state"]
    assert payload["overall_result"]["head_states"] == report["verdict"]["head_states"]
    assert payload["overall_result"]["supported_difference_display"] == \
        report["verdict"]["supported_difference_display"]
    api_financial = payload["financial_findings"][0]
    report_financial = next(item for item in report["findings"] if item["type"] == "FINANCIAL")
    assert api_financial["finding_id"] == report_financial["finding_id"]
    assert api_financial["amount_paise"] == report_financial["amount_paise"]
    assert payload["reconciliation"]["lawful_payable_paise"] == \
        report["reconciliation"]["lawful_payable_paise"]
    assert [step["output_paise"] for step in payload["calculation_steps"]] == [
        step["output_paise"] for step in report["money_flow"]["steps"]
    ]


def test_response_exposes_references_but_no_source_text_or_paths() -> None:
    with TestClient(_development_app()) as client:
        response = client.get("/api/v1/dev/golden-001")
    payload = response.json()
    serialized = response.text

    assert response.status_code == 200
    assert "evidence_references" in payload
    assert all("page_number" in item for item in payload["evidence_references"])
    for forbidden in (
        "/home/user",
        "cases/GOLDEN-001/doc",
        ".pdf",
        "SYNTHETIC PATIENT A",
        "SYNTHETIC GENERAL INSURANCE CO.",
        "page_text",
        "object_key",
        "storage_key",
        '"quote"',
    ):
        assert forbidden not in serialized


def test_unexpected_service_errors_are_structured_and_do_not_leak_details() -> None:
    class BrokenService:
        def run(self):
            raise RuntimeError("secret /home/user/claimcheck/private.pdf stack trace")

    app_with_failure = _development_app(BrokenService())
    with TestClient(app_with_failure, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/dev/golden-001")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["detail"] == \
        "The request could not be completed. Use the request ID when reporting this issue."
    assert body["request_id"] == response.headers["x-request-id"]
    assert "/home/user" not in response.text
    assert "private.pdf" not in response.text
    assert "stack trace" not in response.text
