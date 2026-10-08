#!/usr/bin/env python3
"""Acquire the primary documents that a public knowledge repository points at.

The claimback repository ships only `.pdf.metadata.json` files for its policy wordings and
regulations — it names the canonical public URL instead of redistributing the PDF. This script
follows those names to the publishers' own public URLs and keeps the bytes locally only.

    python3 scripts/acquire_claimback_sources.py            # download what is missing
    python3 scripts/acquire_claimback_sources.py --dry      # list what would be fetched
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "real_data" / "claimback_sources"
MANIFEST = ROOT / ".cache" / "real_data" / "_acquisition_claimback.jsonl"
RAW = "https://raw.githubusercontent.com/HitanshGithub/claimback/main/"

# .metadata.json files published in the repository; the URL inside each one is the publisher's.
METADATA_FILES = [
    "knowledge-base/policy-wordings/arogya-sanjeevani-niva-bupa-2026-27.pdf.metadata.json",
    "knowledge-base/policy-wordings/arogya-sanjeevani-star-health-2025-26.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-amendment-rules-2018.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-amendment-rules-2021.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-amendment-rules-2023.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-rules-2017.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-rules-2017-consolidated-2023.pdf.metadata.json",
    "knowledge-base/regulations/insurance-ombudsman-second-amendment-rules-2021.pdf.metadata.json",
    "knowledge-base/regulations/irdai-circular-ombudsman-self-contained-note-2026.pdf.metadata.json",
    "knowledge-base/regulations/irdai-insurance-products-regulations-2024.pdf.metadata.json",
    "knowledge-base/regulations/irdai-master-circular-health-insurance-2024-annexures.pdf.metadata.json",
    "knowledge-base/regulations/irdai-master-circular-health-insurance-2024.pdf.metadata.json",
    "knowledge-base/regulations/irdai-master-circular-protection-of-policyholders-interests-2024.pdf.metadata.json",
    "knowledge-base/regulations/irdai-protection-of-policyholders-interests-regulations-2024.pdf.metadata.json",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; claimcheck-research/0.1)"}


def fetch(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for meta_path in METADATA_FILES:
        local_meta = CACHE / (meta_path.rsplit("/", 1)[1])
        try:
            if not local_meta.exists():
                local_meta.write_bytes(fetch(RAW + meta_path, 60))
            meta = json.loads(local_meta.read_text(encoding="utf-8"))
            attributes = meta.get("metadataAttributes", meta)
            url = attributes["source_url"]
        except Exception as exc:                                        # noqa: BLE001
            print(f"  {meta_path.rsplit('/', 1)[1]}: metadata FAILED {type(exc).__name__}")
            continue

        target = CACHE / meta_path.rsplit("/", 1)[1].replace(".pdf.metadata.json", ".pdf")
        if args.dry:
            print(f"  would fetch {url[:100]} -> {target.name}")
            continue
        if target.exists() and target.stat().st_size > 1024:
            row = {"url": url, "path": str(target.relative_to(ROOT)), "status": "already_present",
                   "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                   "bytes": target.stat().st_size,
                   "title": attributes.get("title", ""), "issuer": attributes.get("issuer", ""),
                   "reference_no": attributes.get("reference_no", ""),
                   "doc_type": attributes.get("doc_type", ""), "access_date": _today()}
            rows.append(row)
            continue
        try:
            body = fetch(url)
            if not body.startswith(b"%PDF"):
                raise ValueError("not a PDF")
            target.write_bytes(body)
            row = {"url": url, "path": str(target.relative_to(ROOT)), "status": "ok",
                   "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                   "title": attributes.get("title", ""), "issuer": attributes.get("issuer", ""),
                   "reference_no": attributes.get("reference_no", ""),
                   "doc_type": attributes.get("doc_type", ""), "access_date": _today()}
            print(f"  ok   {target.name}  {len(body)/1e3:.0f} kB")
        except Exception as exc:                                        # noqa: BLE001
            row = {"url": url, "path": "", "status": "failed", "error": f"{type(exc).__name__}",
                   "title": attributes.get("title", ""), "doc_type": attributes.get("doc_type", ""),
                   "access_date": _today()}
            print(f"  fail {target.name}: {type(exc).__name__}")
        rows.append(row)
        time.sleep(0.5)

    with MANIFEST.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    ok = sum(1 for r in rows if r["status"] in ("ok", "already_present"))
    print(f"claimback-referenced sources: {ok}/{len(rows)} present; manifest {MANIFEST.name}")
    return 0


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


if __name__ == "__main__":
    sys.exit(main())
