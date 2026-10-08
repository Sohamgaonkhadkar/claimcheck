#!/usr/bin/env python3
"""Targeted, rights-gated public-data helper for CLAIMCHECK.

This is intentionally NOT a web crawler. It has one allow-listed acquisition:
CORD v2's official 100-row test split, pinned to one Hugging Face revision and
approved for frozen OCR/receipt-IE evaluation only. It will not fetch IRDAI,
OGD, Ombudsman, hospital-bill, Kaggle, or arbitrary URLs.

Examples (from the CLAIMCHECK repository root):
    python scripts/research_public_data.py inventory
    python scripts/research_public_data.py check-cord-test
    python scripts/research_public_data.py acquire-cord-test --dry-run
    python scripts/research_public_data.py acquire-cord-test
    python scripts/research_public_data.py verify-cord-test

No model is trained or evaluated by this script. Existing CLAIMCHECK corpora,
labels, catalogues, and model artifacts are outside its write allow-list.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.robotparser import RobotFileParser

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLIC_ROOT = ROOT / "data" / "datasets" / "public_acquired"
CATALOG_PATH = PUBLIC_ROOT / "catalog.json"
PROVENANCE_PATH = PUBLIC_ROOT / "provenance.jsonl"
CORD_ROOT = PUBLIC_ROOT / "cord-v2"
FROZEN_ROOT = CORD_ROOT / "frozen_test"
IMAGE_ROOT = FROZEN_ROOT / "images"
LABELS_PATH = FROZEN_ROOT / "labels.jsonl"
STAGING_ROOT = PUBLIC_ROOT / ".staging"

DATASET_ID = "cord-v2-official-test"
HF_REPO = "naver-clova-ix/cord-v2"
REVISION = "7f0115a4b758a71d6473b8d085751692da2fef98"
LICENSE_ID = "CC-BY-4.0"
EXPECTED_ROWS = 100
USER_AGENT = "CLAIMCHECK-public-data-research/1.0"
ALLOWED_HOSTS = {
    "huggingface.co",
    "datasets-server.huggingface.co",
}
CARD_URL = (
    f"https://huggingface.co/datasets/{HF_REPO}/raw/{REVISION}/README.md"
)
ROWS_BASE = "https://datasets-server.huggingface.co/rows"
MAX_CARD_BYTES = 2 * 1024 * 1024
MAX_ROWS_BYTES = 48 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
MAX_RETRIES = 5
RATE_LIMIT_SECONDS = 1.0

class AcquisitionError(RuntimeError):
    """A fail-closed acquisition or provenance error."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return redirects to the caller so robots/host policy can be checked."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_ROBOTS: dict[str, dict[str, Any]] = {}
_LAST_REQUEST: dict[str, float] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_url(url: str) -> str:
    """Strip ephemeral signatures; retain only stable, non-secret row params."""
    p = urllib.parse.urlsplit(url)
    if p.hostname == "datasets-server.huggingface.co" and p.path == "/rows":
        q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
        allowed = {k: q[k][0] for k in ("dataset", "config", "split", "offset", "length", "revision") if k in q}
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(allowed), ""))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def validate_url(url: str) -> urllib.parse.SplitResult:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.hostname.lower() not in ALLOWED_HOSTS:
        raise AcquisitionError(f"Refusing non-allow-listed URL: {safe_url(url)}")
    if p.username or p.password:
        raise AcquisitionError("Refusing URL with embedded credentials")
    return p


def _pace(host: str) -> None:
    now = time.monotonic()
    previous = _LAST_REQUEST.get(host)
    if previous is not None:
        remaining = RATE_LIMIT_SECONDS - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
    _LAST_REQUEST[host] = time.monotonic()


def _robots_for(url: str) -> dict[str, Any]:
    p = validate_url(url)
    host = p.hostname.lower()
    if host not in _ROBOTS:
        robots_url = f"https://{host}/robots.txt"
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        _pace(host)
        try:
            with _OPENER.open(req, timeout=20) as response:
                status = int(response.status)
                body = response.read(MAX_ROBOTS_BYTES + 1)
                if len(body) > MAX_ROBOTS_BYTES:
                    raise AcquisitionError(f"robots.txt too large for {host}; refusing")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(MAX_ROBOTS_BYTES + 1)
        except Exception as exc:  # fail closed on an unavailable robots policy
            raise AcquisitionError(f"Could not verify robots.txt for {host}: {type(exc).__name__}") from exc

        if status == 404:
            _ROBOTS[host] = {
                "status": 404,
                "robots_url": robots_url,
                "decision": "no_robots_file",
                "parser": None,
            }
        elif 200 <= status < 300:
            parser = RobotFileParser(robots_url)
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            _ROBOTS[host] = {
                "status": status,
                "robots_url": robots_url,
                "decision": "rules_loaded",
                "parser": parser,
            }
        else:
            raise AcquisitionError(f"robots.txt for {host} returned HTTP {status}; refusing")

    state = _ROBOTS[host]
    parser = state["parser"]
    allowed = True if parser is None else bool(parser.can_fetch(USER_AGENT, url))
    return {
        "host": host,
        "robots_url": state["robots_url"],
        "robots_status": state["status"],
        "decision": state["decision"],
        "allowed": allowed,
    }


def _retry_delay(headers: Any, attempt: int) -> float:
    retry_after = None
    if headers is not None:
        retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            try:
                dt = email.utils.parsedate_to_datetime(retry_after)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return min(60.0, max(0.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
    return min(30.0, (2 ** attempt) + random.random())


def fetch_bytes(url: str, *, max_bytes: int, allowed_types: tuple[str, ...] | None = None) -> tuple[bytes, dict[str, Any]]:
    """HTTPS GET with robots checks, redirect revalidation, pacing, and retries."""
    current = url
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        redirects = 0
        try:
            while True:
                p = validate_url(current)
                robots = _robots_for(current)
                if not robots["allowed"]:
                    raise AcquisitionError(f"robots.txt disallows {safe_url(current)}")
                _pace(p.hostname.lower())
                req = urllib.request.Request(
                    current,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json, image/jpeg, text/plain;q=0.9, */*;q=0.1",
                    },
                )
                try:
                    response = _OPENER.open(req, timeout=45)
                    status = int(response.status)
                    headers = response.headers
                    content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    declared = headers.get("Content-Length")
                    if declared and int(declared) > max_bytes:
                        response.close()
                        raise AcquisitionError(f"Response too large at {safe_url(current)}")
                    body = response.read(max_bytes + 1)
                    response.close()
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    headers = exc.headers
                    content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    if status in (301, 302, 303, 307, 308):
                        location = headers.get("Location")
                        if not location or redirects >= 5:
                            raise AcquisitionError("Invalid or excessive HTTP redirects") from exc
                        current = urllib.parse.urljoin(current, location)
                        validate_url(current)
                        redirects += 1
                        continue
                    if status in (408, 425, 429) or 500 <= status <= 599:
                        raise _TransientHTTP(status, headers) from exc
                    raise AcquisitionError(f"HTTP {status} for {safe_url(current)}") from exc

                if status in (408, 425, 429) or 500 <= status <= 599:
                    raise _TransientHTTP(status, headers)
                if not (200 <= status < 300):
                    raise AcquisitionError(f"HTTP {status} for {safe_url(current)}")
                if len(body) > max_bytes:
                    raise AcquisitionError(f"Response exceeded byte cap at {safe_url(current)}")
                if allowed_types and content_type not in allowed_types:
                    raise AcquisitionError(
                        f"Unexpected Content-Type {content_type or '(missing)'} at {safe_url(current)}"
                    )
                final_p = urllib.parse.urlsplit(current)
                meta = {
                    "http_status": status,
                    "content_type": content_type,
                    "bytes": len(body),
                    "requested_url": safe_url(url),
                    "final_url": safe_url(current),
                    "robots": _robots_for(current),
                }
                return body, meta
        except _TransientHTTP as exc:
            last_error = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep(_retry_delay(exc.headers, attempt))
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep(_retry_delay(None, attempt))
                continue
            break
    if isinstance(last_error, _TransientHTTP):
        raise AcquisitionError(f"HTTP {last_error.status} after {MAX_RETRIES} attempts") from last_error
    if last_error is not None:
        raise AcquisitionError(f"Network request failed after {MAX_RETRIES} attempts: {type(last_error).__name__}") from last_error
    raise AcquisitionError("Network request failed without a response")


class _TransientHTTP(Exception):
    def __init__(self, status: int, headers: Any):
        self.status = status
        self.headers = headers
        super().__init__(f"transient HTTP {status}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AcquisitionError(f"Required file is missing: {path.relative_to(ROOT)}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise AcquisitionError(f"Cannot read valid JSON from {path.relative_to(ROOT)}") from exc
    if not isinstance(value, dict):
        raise AcquisitionError(f"Expected JSON object at {path.relative_to(ROOT)}")
    return value


def read_events() -> list[dict[str, Any]]:
    if not PROVENANCE_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    with PROVENANCE_PATH.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AcquisitionError(f"Malformed provenance.jsonl line {line_no}") from exc
            if not isinstance(obj, dict):
                raise AcquisitionError(f"Invalid provenance event on line {line_no}")
            events.append(obj)
    return events


def append_event(event: dict[str, Any]) -> None:
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    existing = read_events()
    event_id = event.get("event_id")
    if event_id and any(item.get("event_id") == event_id for item in existing):
        return
    payload = dict(event)
    payload.setdefault("accessed_at_utc", utc_now())
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    # Append-only: never truncate, replace, or silently rewrite the ledger.
    with PROVENANCE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_catalog_entry() -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = read_json(CATALOG_PATH)
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        raise AcquisitionError("catalog.json has no datasets list")
    matches = [d for d in datasets if isinstance(d, dict) and d.get("dataset_id") == DATASET_ID]
    if len(matches) != 1:
        raise AcquisitionError(f"Expected one catalog entry for {DATASET_ID}; found {len(matches)}")
    entry = matches[0]
    if entry.get("acquisition_decision") != "APPROVED_FOR_FROZEN_TEST_ONLY":
        raise AcquisitionError("Catalog does not explicitly approve this exact frozen test split")
    if entry.get("license_id") != LICENSE_ID or entry.get("source_revision") != REVISION:
        raise AcquisitionError("Catalog license/revision differs from the script's reviewed allow-list")
    return catalog, entry


def record_metadata_fetch(kind: str, url: str, payload: bytes, meta: dict[str, Any]) -> None:
    append_event({
        "event_id": f"{DATASET_ID}:{kind}:{sha256_bytes(payload)}",
        "dataset_id": DATASET_ID,
        "action": kind,
        "source_url": safe_url(url),
        "http_status": meta["http_status"],
        "content_type": meta["content_type"],
        "bytes": meta["bytes"],
        "sha256": sha256_bytes(payload),
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "robots": meta["robots"],
        "note": "Response content is not copied into the provenance ledger.",
    })


def verify_license_card(*, log: bool) -> dict[str, Any]:
    body, meta = fetch_bytes(CARD_URL, max_bytes=MAX_CARD_BYTES, allowed_types=("text/plain", "text/markdown", "text/x-markdown", "application/octet-stream"))
    text = body.decode("utf-8", errors="replace")
    if not re.search(r"(?im)^\s*license\s*:\s*cc[-_]by[-_]4\.0\s*$", text):
        raise AcquisitionError("Pinned dataset card does not declare CC BY 4.0; refusing acquisition")
    if log:
        record_metadata_fetch("dataset_card_verified", CARD_URL, body, meta)
    return {"source_url": safe_url(CARD_URL), **meta}


def row_api_url(length: int = EXPECTED_ROWS) -> str:
    params = {
        "dataset": HF_REPO,
        "config": "default",
        "split": "test",
        "offset": "0",
        "length": str(length),
        "revision": REVISION,
    }
    return ROWS_BASE + "?" + urllib.parse.urlencode(params)


def get_rows(*, log: bool) -> tuple[list[dict[str, Any]], dict[str, Any], bytes]:
    url = row_api_url(EXPECTED_ROWS)
    body, meta = fetch_bytes(url, max_bytes=MAX_ROWS_BYTES, allowed_types=("application/json",))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AcquisitionError("Hugging Face rows endpoint returned invalid JSON") from exc
    features = payload.get("features", [])
    feature_names = {feature.get("name") for feature in features if isinstance(feature, dict)}
    if not {"image", "ground_truth"}.issubset(feature_names):
        raise AcquisitionError(f"Unexpected CORD test schema: {sorted(feature_names)}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != EXPECTED_ROWS:
        raise AcquisitionError(f"Expected {EXPECTED_ROWS} rows; received {len(rows) if isinstance(rows, list) else 'invalid'}")
    if payload.get("num_rows_total") != EXPECTED_ROWS:
        raise AcquisitionError(f"Expected {EXPECTED_ROWS} test rows in pinned snapshot; got {payload.get('num_rows_total')}")
    if log:
        record_metadata_fetch("test_rows_manifest_fetched", url, body, meta)
    normalized: list[dict[str, Any]] = []
    for expected_idx, wrapped in enumerate(rows):
        row = wrapped.get("row") if isinstance(wrapped, dict) else None
        row_idx = wrapped.get("row_idx") if isinstance(wrapped, dict) else None
        if not isinstance(row, dict) or row_idx != expected_idx:
            raise AcquisitionError(f"Unexpected row index/schema at position {expected_idx}")
        image = row.get("image")
        gt = row.get("ground_truth")
        if not isinstance(image, dict) or not isinstance(image.get("src"), str):
            raise AcquisitionError(f"Missing image URL at row {expected_idx}")
        if not isinstance(gt, str):
            raise AcquisitionError(f"Missing string ground_truth at row {expected_idx}")
        image_url = image["src"]
        validate_cord_image_url(image_url, expected_idx)
        try:
            ground_truth = json.loads(gt)
        except json.JSONDecodeError as exc:
            raise AcquisitionError(f"Malformed ground_truth JSON at row {expected_idx}") from exc
        if not isinstance(ground_truth, dict) or not {"gt_parse", "meta", "valid_line"}.issubset(ground_truth):
            raise AcquisitionError(f"Unexpected ground_truth structure at row {expected_idx}")
        flags = privacy_flags(ground_truth)
        if flags:
            # Log flag types only, never the matched text or identifier.
            append_event({
                "event_id": f"{DATASET_ID}:privacy-block:{expected_idx}:{sha256_bytes(gt.encode('utf-8'))}",
                "dataset_id": DATASET_ID,
                "action": "privacy_gate_blocked",
                "source_url": safe_url(url),
                "http_status": meta["http_status"],
                "record_index": expected_idx,
                "flag_types": flags,
                "sha256": sha256_bytes(gt.encode("utf-8")),
                "license_id": LICENSE_ID,
                "source_revision": REVISION,
                "note": "No image or label was committed; flag values are not recorded.",
            })
            raise AcquisitionError(f"Privacy gate flagged row {expected_idx} ({', '.join(flags)}); no dataset committed")
        normalized.append({
            "row_idx": expected_idx,
            "image_url": image_url,
            "ground_truth": ground_truth,
            "width": image.get("width"),
            "height": image.get("height"),
        })
    return normalized, meta, body


def validate_cord_image_url(url: str, row_idx: int) -> None:
    p = validate_url(url)
    pattern = (
        rf"^/cached-assets/naver-clova-ix/cord-v2/--/{re.escape(REVISION)}"
        rf"/--/default/test/{row_idx}/image/image\.jpg$"
    )
    if p.hostname.lower() != "datasets-server.huggingface.co" or not re.fullmatch(pattern, p.path):
        raise AcquisitionError(f"Image URL does not match pinned CORD test row {row_idx}: {safe_url(url)}")


def privacy_flags(ground_truth: dict[str, Any]) -> list[str]:
    """Flag direct personal fields/emails/card-like values before saving.

    CORD's paper describes manual blurring/removal of sensitive receipt data.
    Long item/menu codes can be 13+ digits, so length alone is not treated as
    evidence of a card number. Business/store codes are excluded; a Luhn-valid
    long number or a long number in a card/payment context blocks the split.
    Any flag blocks the whole split for manual review.
    """
    flags: set[str] = set()
    email_re = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
    long_number_re = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
    sensitive_keys = re.compile(r"(?i)(customer|patient|buyer|cardholder|card_number|card_no|email|mobile|personal_phone|aadhaar|passport)")
    card_context = re.compile(r"(?i)(card|credit|debit|payment|account|pan)")

    def luhn_valid(value: str) -> bool:
        digits = [int(char) for char in re.sub(r"\D", "", value)]
        if not 13 <= len(digits) <= 19:
            return False
        total = 0
        parity = len(digits) % 2
        for index, digit in enumerate(digits):
            if index % 2 == parity:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return total % 10 == 0

    def visit(value: Any, path: tuple[str, ...] = (), category: str | None = None) -> None:
        if isinstance(value, dict):
            local_category = str(value.get("category", category or ""))
            for key, child in value.items():
                key_s = str(key)
                next_path = path + (key_s,)
                joined = ".".join(next_path)
                # Store contact fields describe the business printed on a receipt.
                if sensitive_keys.search(key_s) and not joined.lower().startswith("store."):
                    if child not in (None, "", [], {}):
                        flags.add("personal_field_key")
                visit(child, next_path, local_category)
        elif isinstance(value, list):
            for child in value:
                visit(child, path, category)
        elif isinstance(value, str):
            if email_re.search(value):
                flags.add("email_pattern")
            if long_number_re.search(value):
                context = (".".join(path) + "." + (category or "")).lower()
                # Product/menu/store codes are business identifiers, not treated
                # as customer card data by this automated screen.
                business_code = ".menu." in f".{context}." or ".store." in f".{context}."
                if not business_code and (card_context.search(context) or luhn_valid(value)):
                    flags.add("card_or_account_like_sequence")

    visit(ground_truth)
    return sorted(flags)


def _read_provenance_index() -> tuple[dict[int, str], dict[str, str], set[str]]:
    by_row: dict[int, str] = {}
    by_hash: dict[str, str] = {}
    event_ids: set[str] = set()
    for event in read_events():
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            event_ids.add(event_id)
        if event.get("dataset_id") != DATASET_ID:
            continue
        action = event.get("action")
        rel = event.get("local_path")
        digest = event.get("sha256")
        row_idx = event.get("record_index")
        if isinstance(rel, str) and isinstance(digest, str) and action in {"asset_committed", "asset_duplicate_skipped"}:
            by_hash.setdefault(digest, rel)
            if isinstance(row_idx, int) and action == "asset_committed":
                by_row[row_idx] = rel
    return by_row, by_hash, event_ids


def _write_temp(path: pathlib.Path, data: bytes) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".claimcheck-", suffix=".part", dir=str(path.parent))
    temp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _commit_no_overwrite(temp: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # A hard-link commit is atomic and fails rather than overwriting an existing target.
        os.link(temp, target)
    except FileExistsError as exc:
        raise AcquisitionError(f"Refusing to overwrite existing artifact: {target.relative_to(ROOT)}") from exc
    finally:
        temp.unlink(missing_ok=True)


def download_image(row: dict[str, Any], existing_rows: dict[int, str], hash_paths: dict[str, str]) -> str:
    idx = int(row["row_idx"])
    target_rel = f"data/datasets/public_acquired/cord-v2/frozen_test/images/test-{idx:03d}.jpg"
    target = ROOT / target_rel
    if idx in existing_rows:
        rel = existing_rows[idx]
        path = ROOT / rel
        if not path.is_file():
            # Ledger says an artifact was committed but it is absent: do not silently repair it.
            raise AcquisitionError(f"Committed artifact is missing: {rel}")
        digest = sha256_file(path)
        events = read_events()
        expected = [e.get("sha256") for e in events if e.get("dataset_id") == DATASET_ID and e.get("record_index") == idx and e.get("action") == "asset_committed"]
        if digest not in expected:
            raise AcquisitionError(f"Existing image does not match append-only provenance for row {idx}")
        return rel
    if target.exists():
        raise AcquisitionError(f"Untracked target exists; refusing to overwrite: {target_rel}")

    body, meta = fetch_bytes(row["image_url"], max_bytes=MAX_IMAGE_BYTES, allowed_types=("image/jpeg", "application/octet-stream", "binary/octet-stream"))
    if not body.startswith(b"\xff\xd8\xff"):
        raise AcquisitionError(f"Row {idx} did not return JPEG bytes")
    digest = sha256_bytes(body)
    source = safe_url(row["image_url"])
    if digest in hash_paths:
        existing_rel = hash_paths[digest]
        append_event({
            "event_id": f"{DATASET_ID}:duplicate:{idx}:{digest}",
            "dataset_id": DATASET_ID,
            "action": "asset_duplicate_skipped",
            "source_url": source,
            "local_path": existing_rel,
            "record_index": idx,
            "split": "frozen_test",
            "http_status": meta["http_status"],
            "content_type": meta["content_type"],
            "bytes": meta["bytes"],
            "sha256": digest,
            "license_id": LICENSE_ID,
            "source_revision": REVISION,
            "robots": meta["robots"],
            "duplicate_of": existing_rel,
        })
        return existing_rel

    temp = _write_temp(target, body)
    # Log the verified response before the atomic no-overwrite commit. If the process
    # stops between these operations, the verified event makes recovery auditable.
    append_event({
        "event_id": f"{DATASET_ID}:verified:{idx}:{digest}",
        "dataset_id": DATASET_ID,
        "action": "asset_download_verified_pending_commit",
        "source_url": source,
        "local_path": target_rel,
        "record_index": idx,
        "split": "frozen_test",
        "http_status": meta["http_status"],
        "content_type": meta["content_type"],
        "bytes": meta["bytes"],
        "sha256": digest,
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "robots": meta["robots"],
    })
    _commit_no_overwrite(temp, target)
    append_event({
        "event_id": f"{DATASET_ID}:committed:{idx}:{digest}",
        "dataset_id": DATASET_ID,
        "action": "asset_committed",
        "source_url": source,
        "local_path": target_rel,
        "record_index": idx,
        "split": "frozen_test",
        "http_status": meta["http_status"],
        "content_type": meta["content_type"],
        "bytes": meta["bytes"],
        "sha256": digest,
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "robots": meta["robots"],
    })
    hash_paths[digest] = target_rel
    return target_rel


def _ensure_labels_commit_event(digest: str, payload_bytes: int, record_count: int, *, recovered_after_interruption: bool) -> None:
    """Record a labels commit, repairing only the verified-file/no-commit-event crash window."""
    expected = {
        "sha256": digest,
        "bytes": payload_bytes,
        "record_count": record_count,
        "source_revision": REVISION,
        "split": "frozen_test",
        "local_path": "data/datasets/public_acquired/cord-v2/frozen_test/labels.jsonl",
    }
    commit_id = f"{DATASET_ID}:labels-committed:{digest}"
    events = [e for e in read_events() if e.get("dataset_id") == DATASET_ID]
    same_id = [e for e in events if e.get("event_id") == commit_id]
    if same_id:
        if len(same_id) != 1 or any(same_id[0].get(k) != v for k, v in expected.items()):
            raise AcquisitionError("Existing labels-committed event conflicts with the frozen labels")
        return
    if any(e.get("action") == "labels_committed" for e in events):
        raise AcquisitionError("A conflicting labels-committed event exists in provenance")

    verified = [
        e for e in events
        if e.get("action") == "labels_download_verified_pending_commit"
        and all(e.get(k) == v for k, v in expected.items())
    ]
    if not verified:
        raise AcquisitionError("Cannot commit labels without a matching verified-payload provenance event")
    prior = verified[-1]
    append_event({
        "event_id": commit_id,
        "dataset_id": DATASET_ID,
        "action": "labels_committed",
        "source_url": prior.get("source_url", safe_url(row_api_url())),
        "local_path": expected["local_path"],
        "http_status": prior.get("http_status", 200),
        "content_type": prior.get("content_type", "application/x-ndjson"),
        "bytes": payload_bytes,
        "sha256": digest,
        "record_count": record_count,
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "split": "frozen_test",
        "recovered_after_interruption": recovered_after_interruption,
        "note": (
            "Recovered append-only commit event: the already-present labels file matched the pinned, verified payload hash."
            if recovered_after_interruption
            else "Labels committed after verified payload was atomically written."
        ),
    })


def write_labels(rows: list[dict[str, Any]], image_paths: dict[int, str]) -> tuple[str, int]:
    lines: list[str] = []
    for row in rows:
        idx = int(row["row_idx"])
        obj = {
            "dataset_id": DATASET_ID,
            "source_dataset": HF_REPO,
            "source_revision": REVISION,
            "source_split": "test",
            "split": "frozen_test",
            "source_row_index": idx,
            "image_id": f"cord-v2-test-{idx:03d}",
            "image_path": image_paths[idx].removeprefix("data/datasets/public_acquired/cord-v2/frozen_test/"),
            "ground_truth": row["ground_truth"],
        }
        lines.append(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    digest = sha256_bytes(payload)
    if LABELS_PATH.exists():
        current = sha256_file(LABELS_PATH)
        if current != digest:
            raise AcquisitionError("Existing labels.jsonl differs; refusing to overwrite frozen labels")
        _ensure_labels_commit_event(digest, len(payload), len(lines), recovered_after_interruption=True)
        return digest, len(lines)
    temp = _write_temp(LABELS_PATH, payload)
    append_event({
        "event_id": f"{DATASET_ID}:labels-verified:{digest}",
        "dataset_id": DATASET_ID,
        "action": "labels_download_verified_pending_commit",
        "source_url": safe_url(row_api_url()),
        "local_path": "data/datasets/public_acquired/cord-v2/frozen_test/labels.jsonl",
        "http_status": 200,
        "content_type": "application/x-ndjson",
        "bytes": len(payload),
        "sha256": digest,
        "record_count": len(lines),
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "split": "frozen_test",
    })
    _commit_no_overwrite(temp, LABELS_PATH)
    _ensure_labels_commit_event(digest, len(payload), len(lines), recovered_after_interruption=False)
    return digest, len(lines)


def _atomic_write_catalog(catalog: dict[str, Any], entry: dict[str, Any]) -> None:
    previous_bytes = CATALOG_PATH.read_bytes()
    previous_hash = sha256_bytes(previous_bytes)
    new_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    new_hash = sha256_bytes(new_bytes)
    if new_bytes == previous_bytes:
        return
    temp = _write_temp(CATALOG_PATH, new_bytes)
    # Catalog status is intentionally updated once from pending to acquired; data files
    # themselves are never overwritten. Guard against concurrent/manual edits rather
    # than clobbering them silently.
    if sha256_file(CATALOG_PATH) != previous_hash:
        temp.unlink(missing_ok=True)
        raise AcquisitionError("catalog.json changed concurrently; refusing to overwrite it")
    os.replace(temp, CATALOG_PATH)
    append_event({
        "event_id": f"{DATASET_ID}:catalog:{new_hash}",
        "dataset_id": DATASET_ID,
        "action": "catalog_status_updated",
        "source_url": safe_url(row_api_url()),
        "local_path": "data/datasets/public_acquired/catalog.json",
        "http_status": 200,
        "previous_catalog_sha256": previous_hash,
        "sha256": new_hash,
        "license_id": LICENSE_ID,
        "source_revision": REVISION,
        "note": "Explicit pending-to-acquired update after split artifacts were verified.",
    })


def _catalog_update(catalog: dict[str, Any], entry: dict[str, Any], *, counts: dict[str, Any], labels_sha: str) -> None:
    entry.update({
        "acquisition_status": "acquired_frozen_test_only",
        "acquired_at_utc": utc_now(),
        "source_revision": REVISION,
        "record_count": EXPECTED_ROWS,
        "acquired_image_count": counts["unique_images"],
        "acquired_label_record_count": counts["label_records"],
        "source_split_counts": {"train": 800, "validation": 100, "test": 100},
        "acquired_split_counts": {"train": 0, "development": 0, "frozen_test": EXPECTED_ROWS},
        "labels_path": "data/datasets/public_acquired/cord-v2/frozen_test/labels.jsonl",
        "labels_sha256": labels_sha,
        "image_directory": "data/datasets/public_acquired/cord-v2/frozen_test/images",
        "frozen_test": True,
        "training_performed": False,
        "model_metrics_created": False,
        "coverage": {
            "policy_documents": 0,
            "hospital_bill_documents": 0,
            "receipt_images": counts["unique_images"],
            "ocr_documents": EXPECTED_ROWS,
            "insurer_decisions": 0,
            "rejections": 0,
            "claim_packages": 0,
            "claim_outcomes": 0,
            "source_ground_truth_records": counts["label_records"],
        },
    })
    catalog["generated_at_utc"] = utc_now()
    catalog["end_to_end_public_data_gap"] = True
    catalog["public_acquired_counts"] = {
        "datasets": 1,
        "records": EXPECTED_ROWS,
        "source_labeled_records": EXPECTED_ROWS,
        "images": counts["unique_images"],
        "ocr_documents": EXPECTED_ROWS,
        "policy_documents": 0,
        "hospital_bill_documents": 0,
        "insurer_decision_documents": 0,
        "rejection_documents": 0,
        "claim_packages": 0,
        "claim_outcomes": 0,
    }


def _verify_local(entry: dict[str, Any]) -> dict[str, Any]:
    if entry.get("acquisition_status") != "acquired_frozen_test_only":
        raise AcquisitionError(f"Dataset is not fully acquired (status={entry.get('acquisition_status')})")
    events = read_events()
    committed: dict[int, dict[str, Any]] = {}
    duplicate_count = 0
    for event in events:
        if event.get("dataset_id") != DATASET_ID:
            continue
        if event.get("action") == "asset_committed" and isinstance(event.get("record_index"), int):
            committed[int(event["record_index"])] = event
        if event.get("action") == "asset_duplicate_skipped":
            duplicate_count += 1
    labels_events = [e for e in events if e.get("dataset_id") == DATASET_ID and e.get("action") == "labels_committed"]
    if not LABELS_PATH.is_file():
        raise AcquisitionError("Frozen labels file is missing")
    labels_hash = sha256_file(LABELS_PATH)
    if labels_hash != entry.get("labels_sha256"):
        raise AcquisitionError("Frozen labels file hash differs from catalog")
    try:
        labels = [json.loads(line) for line in LABELS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise AcquisitionError("Frozen labels file is invalid") from exc
    if len(labels) != EXPECTED_ROWS:
        raise AcquisitionError(f"Expected {EXPECTED_ROWS} frozen test labels; found {len(labels)}")
    for idx, label in enumerate(labels):
        if label.get("source_row_index") != idx or label.get("source_revision") != REVISION or label.get("split") != "frozen_test":
            raise AcquisitionError(f"Frozen label metadata mismatch at row {idx}")
        rel = label.get("image_path")
        if not isinstance(rel, str):
            raise AcquisitionError(f"Missing image path in frozen label row {idx}")
        image_path = FROZEN_ROOT / rel
        if not image_path.is_file():
            raise AcquisitionError(f"Frozen image missing for label row {idx}: {rel}")
        digest = sha256_file(image_path)
        expected_event = next((e for e in committed.values() if e.get("local_path") == str(image_path.relative_to(ROOT))), None)
        if expected_event is None or expected_event.get("sha256") != digest:
            raise AcquisitionError(f"Image provenance/hash mismatch for label row {idx}")
    expected_label_path = str(LABELS_PATH.relative_to(ROOT))
    matching_labels_events = [
        e for e in labels_events
        if e.get("sha256") == labels_hash
        and e.get("bytes") == LABELS_PATH.stat().st_size
        and e.get("record_count") == len(labels)
        and e.get("source_revision") == REVISION
        and e.get("split") == "frozen_test"
        and e.get("local_path") == expected_label_path
    ]
    if len(matching_labels_events) != 1:
        raise AcquisitionError("Missing, duplicated, or conflicting labels-committed provenance event")
    return {
        "records": len(labels),
        "unique_committed_images": len({e.get("sha256") for e in committed.values()}),
        "duplicate_rows": duplicate_count,
        "labels_sha256": labels_hash,
        "status": "verified",
    }


def cmd_inventory(_: argparse.Namespace) -> int:
    catalog, entry = load_catalog_entry()
    summary = {
        "catalog": str(CATALOG_PATH.relative_to(ROOT)),
        "dataset_id": entry.get("dataset_id"),
        "dataset_name": entry.get("dataset_name"),
        "source_revision": entry.get("source_revision"),
        "acquisition_decision": entry.get("acquisition_decision"),
        "acquisition_status": entry.get("acquisition_status"),
        "record_count": entry.get("record_count", 0),
        "frozen_test": entry.get("frozen_test", False),
        "end_to_end_public_data_gap": catalog.get("end_to_end_public_data_gap", True),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    catalog, entry = load_catalog_entry()
    card = verify_license_card(log=False)
    rows, row_meta, _ = get_rows(log=False)
    print(json.dumps({
        "decision": entry.get("acquisition_decision"),
        "license_card": {k: v for k, v in card.items() if k != "robots"} | {"robots": card["robots"]},
        "rows_endpoint": {k: v for k, v in row_meta.items() if k != "robots"} | {"robots": row_meta["robots"]},
        "verified_rows": len(rows),
        "revision": REVISION,
        "privacy_gate": "passed on source-provided annotations; pixel-level review remains a separate QA step",
        "would_download_bytes": "not estimated; no image requests made by check command",
        "dry_run": True,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    catalog, entry = load_catalog_entry()
    if entry.get("acquisition_status") == "acquired_frozen_test_only":
        print(json.dumps(_verify_local(entry), indent=2))
        print("Already acquired and verified; no network requests or overwrites performed.")
        return 0
    if args.dry_run:
        result = cmd_check(args)
        print("Dry run only: no dataset files written.")
        return result

    # Re-confirm the pinned card and the split schema at acquisition time.
    card_meta = verify_license_card(log=True)
    rows, rows_meta, rows_body = get_rows(log=True)
    by_row, hash_paths, _ = _read_provenance_index()
    image_paths: dict[int, str] = {}
    for row in rows:
        rel = download_image(row, by_row, hash_paths)
        image_paths[int(row["row_idx"])] = rel
        by_row[int(row["row_idx"])] = rel
        digest = sha256_file(ROOT / rel)
        hash_paths.setdefault(digest, rel)
        print(f"saved/reused {int(row['row_idx']) + 1:03d}/{EXPECTED_ROWS}: {rel}", flush=True)

    labels_sha, label_count = write_labels(rows, image_paths)
    unique_images = len({sha256_file(ROOT / p) for p in image_paths.values()})
    counts = {"unique_images": unique_images, "label_records": label_count}
    _catalog_update(catalog, entry, counts=counts, labels_sha=labels_sha)
    _atomic_write_catalog(catalog, entry)
    verified = _verify_local(entry)
    print(json.dumps({
        "result": "acquired_and_verified",
        "dataset_id": DATASET_ID,
        "revision": REVISION,
        "source_split": "test",
        "local_split": "frozen_test",
        "records": label_count,
        "unique_images": unique_images,
        "labels_sha256": labels_sha,
        "dataset_card": {"url": card_meta["source_url"], "http_status": card_meta["http_status"], "bytes": card_meta["bytes"]},
        "rows_manifest": {"url": safe_url(row_api_url()), "http_status": rows_meta["http_status"], "bytes": rows_meta["bytes"], "sha256": sha256_bytes(rows_body)},
        "verified": verified,
        "training_performed": False,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    _, entry = load_catalog_entry()
    print(json.dumps(_verify_local(entry), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="show the approved public-data catalogue entry").set_defaults(func=cmd_inventory)
    sub.add_parser("check-cord-test", help="check robots, pinned CC-BY card, schema, and split count without saving files").set_defaults(func=cmd_check)
    acquire = sub.add_parser("acquire-cord-test", help="acquire only the pre-approved official CORD test split")
    acquire.add_argument("--dry-run", action="store_true", help="perform checks only; save no source files")
    acquire.set_defaults(func=cmd_acquire)
    sub.add_parser("verify-cord-test", help="verify hashes, frozen split labels, and provenance").set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AcquisitionError as exc:
        print(f"REFUSED/FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
