"""Small, safe-by-default API configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


_DEFAULT_DEVELOPMENT_OWNER = UUID("a7de45b2-26b7-4ad6-83b7-3d4784f5e61a")


@dataclass(frozen=True)
class AppSettings:
    environment: str = "production"
    database_url: str | None = None
    storage_root: str | None = None
    development_owner_id: UUID = _DEFAULT_DEVELOPMENT_OWNER
    max_upload_bytes: int = 25 * 1024 * 1024
    max_request_bytes: int = 26 * 1024 * 1024
    max_pdf_pages: int = 250
    cors_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.max_upload_bytes, self.max_request_bytes, self.max_pdf_pages) <= 0:
            raise ValueError("Upload resource limits must be positive.")
        if self.max_request_bytes < self.max_upload_bytes:
            raise ValueError("Request limit cannot be smaller than the per-file upload limit.")

        # CORS is opt-in, exact-origin only, and never a substitute for authentication.
        normalized: list[str] = []
        for raw_origin in self.cors_origins:
            origin = str(raw_origin).strip().rstrip("/")
            if not origin:
                continue
            parsed = urlsplit(origin)
            try:
                _ = parsed.port  # also validate malformed ports
            except ValueError as exc:
                raise ValueError("CLAIMCHECK_CORS_ORIGINS contains an invalid origin.") from exc
            host = (parsed.hostname or "").lower()
            local_http = self.development_mode and host in {"localhost", "127.0.0.1", "::1"}
            if (
                "*" in origin
                or parsed.scheme not in ({"https", "http"} if local_http else {"https"})
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "CLAIMCHECK_CORS_ORIGINS must contain exact HTTPS origins; HTTP is "
                    "allowed only for localhost in development."
                )
            normalized.append(f"{parsed.scheme.lower()}://{parsed.netloc}")
        object.__setattr__(self, "cors_origins", tuple(dict.fromkeys(normalized)))

    @classmethod
    def from_environment(cls) -> "AppSettings":
        # Only an explicit development setting enables the synthetic demo identity/route.
        environment = os.environ.get("CLAIMCHECK_ENV", "production").strip().lower()
        raw_owner = os.environ.get("CLAIMCHECK_DEV_OWNER_ID")
        development_owner_id = UUID(raw_owner) if raw_owner else _DEFAULT_DEVELOPMENT_OWNER
        upload_bytes = int(os.environ.get("CLAIMCHECK_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
        request_bytes = int(os.environ.get("CLAIMCHECK_MAX_REQUEST_BYTES", 26 * 1024 * 1024))
        max_pages = int(os.environ.get("CLAIMCHECK_MAX_PDF_PAGES", 250))
        cors_origins = tuple(
            origin.strip()
            for origin in os.environ.get("CLAIMCHECK_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
        if min(upload_bytes, request_bytes, max_pages) <= 0 or request_bytes < upload_bytes:
            raise ValueError("Invalid upload resource limits.")
        storage_root = os.environ.get("CLAIMCHECK_STORAGE_ROOT")
        if not storage_root:
            storage_root = str(Path.home() / ".local" / "share" / "claimcheck" / "private-storage")
        return cls(
            environment=environment,
            database_url=os.environ.get("DATABASE_URL") or None,
            storage_root=storage_root,
            development_owner_id=development_owner_id,
            max_upload_bytes=upload_bytes,
            max_request_bytes=request_bytes,
            max_pdf_pages=max_pages,
            cors_origins=cors_origins,
        )

    @property
    def development_mode(self) -> bool:
        return self.environment == "development"
