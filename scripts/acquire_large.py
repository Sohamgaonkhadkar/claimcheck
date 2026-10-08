#!/usr/bin/env python3
"""Acquire the real corpora for the large-dataset milestone.

Every acquisition goes through one manifest so provenance, hashes and licence status are
recorded once, at the moment of download, and never reconstructed from memory afterwards.

Raw documents land in ``.cache/real_data/`` — outside git, outside the workspace snapshot —
because public availability is not permission to redistribute. What ships is derived data plus
this manifest: a URL, a timestamp, a SHA-256 and a licence status per document are enough to
re-acquire and to verify, and they carry no document text.

Usage::

    python3 scripts/acquire_large.py --source irdai_full
    python3 scripts/acquire_large.py --source github_bills --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "real_data"
MANIFEST = CACHE / "_acquisition_large.jsonl"
UA = "Mozilla/5.0 (X11; Linux x86_64) claimcheck-research/1.0"

SOURCES: dict[str, dict[str, Any]] = {
    "irdai_full": {
        "kind": "regulator_mirror",
        "publisher": "Insurance Regulatory and Development Authority of India",
        "mirror": "huggingface.co/datasets/Ashishmg10/irdai-corpus",
        "base": "https://huggingface.co/datasets/Ashishmg10/irdai-corpus/resolve/main/",
        "tree_api": "https://huggingface.co/api/datasets/Ashishmg10/irdai-corpus/tree/main?recursive=true",
        "dest": "irdai_full",
        "licence_status": "PUBLIC_LOCAL_ONLY",
        "licence_note": "regulator's own publications; provenance is the instrument number on "
                        "each document; mirror used for discovery, not redistributed",
        "pii_status": "none_expected",
        "extensions": (".pdf",),
    },
    "github_bills": {
        "kind": "third_party_dataset",
        "publisher": "various GitHub repositories",
        "base": "",
        "dest": "github_bills",
        "licence_status": "USAGE_UNVERIFIED",
        "licence_note": "no licence declared on the source repository",
        "pii_status": "present",
        "extensions": (".pdf", ".png", ".jpg", ".jpeg"),
    },
}


# --------------------------------------------------------------------------- helpers


def http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def record(entry: dict[str, Any]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def download(url: str, dest: pathlib.Path, *, source: str, extra: dict[str, Any],
             retries: int = 2) -> dict[str, Any]:
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    entry: dict[str, Any] = {"source": source, "url": url, "path": str(dest),
                             "access_date": started, **extra}
    if dest.exists() and dest.stat().st_size > 0:
        entry.update(status="already_present", bytes=dest.stat().st_size,
                     sha256=sha256_file(dest))
        record(entry)
        return entry
    last: str = ""
    for attempt in range(retries + 1):
        try:
            payload = http_get(url)
            if payload[:5].lower().startswith(b"<!doc") or payload[:6].lower().startswith(b"<html"):
                entry.update(status="fail:html", bytes=len(payload),
                             head=payload[:80].decode("latin-1"))
                record(entry)
                return entry
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            entry.update(status="ok", bytes=len(payload),
                         sha256=hashlib.sha256(payload).hexdigest())
            record(entry)
            return entry
        except urllib.error.HTTPError as exc:
            last = f"fail:http{exc.code}"
        except Exception as exc:                                   # noqa: BLE001
            last = f"fail:{type(exc).__name__}"
        time.sleep(1.0 + attempt)
    entry.update(status=last)
    record(entry)
    return entry


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def list_tree(api_url: str) -> list[dict[str, Any]]:
    return json.loads(http_get(api_url).decode())


# --------------------------------------------------------------------------- sources


def acquire_irdai_full(limit: int, dry: bool) -> None:
    spec = SOURCES["irdai_full"]
    tree = list_tree(spec["tree_api"])
    files = [f for f in tree if f["path"].lower().endswith(spec["extensions"])]
    files.sort(key=lambda f: f["path"])
    dest_dir = CACHE / spec["dest"]
    print(f"{len(files)} files, {sum(f['size'] for f in files)/1e6:.0f} MB")
    if dry:
        print("dry run; first 5:", [f["path"] for f in files[:5]])
        return
    done = 0
    for f in files:
        name = f["path"].replace("/", "__")
        entry = download(spec["base"] + f["path"].replace(" ", "%20"),
                         dest_dir / name, source="irdai_full",
                         extra={"upstream_path": f["path"], "upstream_bytes": f["size"],
                                "publisher": spec["publisher"], "mirror": spec["mirror"],
                                "licence_status": spec["licence_status"],
                                "licence_note": spec["licence_note"],
                                "pii_status": spec["pii_status"],
                                "document_type": "regulation"})
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(files)}", flush=True)
        if limit and done >= limit:
            break
    print(f"irdai_full: {done} attempted")


def acquire_github_listing(limit: int, dry: bool) -> None:
    """Download the explicit GitHub file list in ``scripts/github_bill_sources.json``."""
    path = ROOT / "scripts" / "github_bill_sources.json"
    if not path.exists():
        print("no github_bill_sources.json yet")
        return
    files = json.loads(path.read_text(encoding="utf-8"))
    spec = SOURCES["github_bills"]
    print(f"{len(files)} candidate files")
    if dry:
        for f in files[:10]:
            print("  ", f["url"])
        return
    dest_dir = CACHE / spec["dest"]
    for item in files[:limit] if limit else files:
        download(item["url"], dest_dir / item["name"], source="github_bills",
                 extra={"repository": item.get("repository", ""),
                        "publisher": item.get("repository", ""),
                        "licence_status": item.get("licence_status", spec["licence_status"]),
                        "licence_note": item.get("licence_note", spec["licence_note"]),
                        "pii_status": "present", "document_type": "hospital_bill"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if args.source == "irdai_full":
        acquire_irdai_full(args.limit, args.dry)
    elif args.source == "github_bills":
        acquire_github_listing(args.limit, args.dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
