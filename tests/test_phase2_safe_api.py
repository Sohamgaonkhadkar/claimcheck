"""No-database tests for authentication boundaries and readiness behavior."""
from __future__ import annotations

from fastapi.testclient import TestClient

from claimcheck.api.app import create_app
from claimcheck.api.config import AppSettings


def test_production_does_not_mint_a_development_identity_or_claim_database_readiness(tmp_path) -> None:
    app = create_app(AppSettings(environment="production", storage_root=str(tmp_path / "private")))
    with TestClient(app) as client:
        cases = client.get("/api/v1/cases")
        assert cases.status_code == 401
        assert cases.headers["content-type"].startswith("application/problem+json")
        assert cases.json()["code"] == "UNAUTHENTICATED"
        assert client.get("/readyz").status_code == 503
        # The original Phase 1 probes remain available with their original response contract.
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}


def test_development_identity_is_server_configured_and_missing_db_is_safe(tmp_path) -> None:
    app = create_app(AppSettings(environment="development", storage_root=str(tmp_path / "private")))
    expected_owner = str(app.state.identity_provider.owner_id)
    with TestClient(app) as client:
        response = client.post("/api/v1/cases", json={}, headers={"X-Owner-ID": "not-an-owner"})
    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_NOT_CONFIGURED"
    assert expected_owner not in response.text
    assert "not-an-owner" not in response.text
