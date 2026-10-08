from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claimcheck.api.app import create_app
from claimcheck.api.config import AppSettings


def test_cors_preflight_allows_only_the_configured_exact_origin(tmp_path) -> None:
    app = create_app(AppSettings(
        environment="production",
        storage_root=str(tmp_path / "private"),
        cors_origins=("https://app.example.com",),
    ))

    response = TestClient(app).options(
        "/api/v1/cases",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-credentials" not in response.headers


def test_cors_is_disabled_when_no_origins_are_configured(tmp_path) -> None:
    app = create_app(AppSettings(
        environment="production",
        storage_root=str(tmp_path / "private"),
    ))

    response = TestClient(app).get(
        "/healthz", headers={"Origin": "https://unlisted.example.com"}
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_configuration_rejects_wildcards_and_non_local_http() -> None:
    with pytest.raises(ValueError, match="exact HTTPS origins"):
        AppSettings(environment="production", cors_origins=("*",))

    with pytest.raises(ValueError, match="exact HTTPS origins"):
        AppSettings(environment="production", cors_origins=("http://app.example.com",))


def test_local_http_cors_origin_is_allowed_only_in_development() -> None:
    settings = AppSettings(
        environment="development",
        cors_origins=("http://localhost:5173/",),
    )

    assert settings.cors_origins == ("http://localhost:5173",)


def test_cors_origins_are_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMCHECK_ENV", "production")
    monkeypatch.setenv(
        "CLAIMCHECK_CORS_ORIGINS",
        "https://app.example.com, https://admin.example.com ",
    )

    settings = AppSettings.from_environment()

    assert settings.cors_origins == (
        "https://app.example.com",
        "https://admin.example.com",
    )
