"""CLI entry point for the separate claimcheck-worker process."""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from claimcheck.api.config import AppSettings
from claimcheck.persistence.database import create_postgres_engine
from claimcheck.persistence.storage import PrivateLocalFileStorage
from claimcheck.worker.runner import DocumentWorker, WorkerSettings


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean setting: {name}")


def _worker_settings(app: AppSettings) -> WorkerSettings:
    worker_id = os.environ.get("CLAIMCHECK_WORKER_ID") or \
        f"{socket.gethostname()}-{uuid4().hex[:12]}"
    return WorkerSettings(
        worker_id=worker_id,
        poll_interval_seconds=float(os.environ.get("CLAIMCHECK_WORKER_POLL_SECONDS", "1")),
        lease_seconds=int(os.environ.get("CLAIMCHECK_WORKER_LEASE_SECONDS", "300")),
        heartbeat_seconds=int(os.environ.get("CLAIMCHECK_WORKER_HEARTBEAT_SECONDS", "30")),
        retry_delay_seconds=int(os.environ.get("CLAIMCHECK_WORKER_RETRY_SECONDS", "30")),
        max_pdf_pages=app.max_pdf_pages,
        max_pdf_bytes=app.max_upload_bytes,
        ocr_enabled=_boolean("CLAIMCHECK_OCR_ENABLED", True),
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("CLAIMCHECK_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app_settings = AppSettings.from_environment()
    if not app_settings.database_url:
        raise SystemExit("DATABASE_URL must be configured for the document worker.")
    if not app_settings.storage_root:
        raise SystemExit("CLAIMCHECK_STORAGE_ROOT must be configured for the document worker.")
    engine = create_postgres_engine(app_settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = PrivateLocalFileStorage(app_settings.storage_root)
    worker = DocumentWorker(factory, storage, settings=_worker_settings(app_settings))
    shutdown = threading.Event()

    def request_shutdown(_signum, _frame) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        worker.run_forever(shutdown)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
