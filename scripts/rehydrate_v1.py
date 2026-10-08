#!/usr/bin/env python3
"""Rehydrate the frozen v1.0 source documents that Phase 3E has to verify against.

Why this exists: `.cache/` is excluded from the workspace snapshot, so the raw documents behind
the frozen v1.0 release are not on disk in a fresh session. Phase 3E cannot be done honestly
without them — "source_verified" has to mean *checked against the document*, not *checked against
our own extract*, and the bill and OCR gold sets are read from the page image.

This is **rehydration, not acquisition**. It downloads the documents already listed in the frozen
v1.0 `documents` table, from the URLs already recorded for them, and keeps a file only if its
SHA-256 matches the frozen hash byte for byte. Anything that does not match is discarded and
reported. No new document can enter the corpus through this script, by construction.

    python3 scripts/rehydrate_v1.py --group phase3e --dry
    python3 scripts/rehydrate_v1.py --group phase3e
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

CACHE = ROOT / ".cache" / "real_data"
MANIFEST = ROOT / "bench" / "results" / "large-dataset" / "rehydration.jsonl"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; claimcheck-research/0.1)"}

#: where each source's bytes come from. Kept explicit: a mirror that hides its origin is not a
#: provenance record, and provenance is the whole point of the v1.0 freeze.
BILL_MIRROR = "https://huggingface.co/datasets/sansverse/medical-bill-samples/resolve/main/"
IRDAI_MIRROR = "https://huggingface.co/datasets/Ashishmg10/irdai-corpus/resolve/main/"

GROUPS = {
    # everything Phase 3E annotates from: the bills, and the policy wordings and rules that the
    # retrieval gold set draws its clauses from.
    "phase3e": ("hospital_bills_public_sample", "irdai_wordings", "claimback_referenced_wordings",
                "claimback_referenced_rules"),
}


def frozen_documents() -> list[dict]:
    """The v1.0 document table — the only thing this script is allowed to fetch."""
    from bench.datasets import store

    return store.read_parquet(store.DATASETS / "real" / "documents")


def irdai_mirror_index() -> dict[str, str]:
    """basename -> path inside the IRDAI mirror, so a file name can be resolved to a URL."""
    url = ("https://huggingface.co/api/datasets/Ashishmg10/irdai-corpus/tree/main?recursive=true")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=120) as response:
        entries = json.loads(response.read())
    return {entry["path"].rsplit("/", 1)[-1]: entry["path"] for entry in entries
            if entry.get("type") == "file"}


def irdai_wording_urls() -> dict[str, str]:
    """The 13 filed wordings, from the acquisition script that originally fetched them."""
    namespace: dict = {}
    source = (ROOT / "scripts" / "acquire_phase3d.py").read_text(encoding="utf-8")
    start = source.index("IRDAI_WORDINGS: dict[str, str] = {")
    end = source.index("IRDAI_WORDINGS_UNAVAILABLE", start)
    exec(compile(source[start:end], "acquire_phase3d.py", "exec"), namespace)  # noqa: S102
    return namespace["IRDAI_WORDINGS"]


def claimback_urls() -> dict[str, str]:
    """The 2 wordings and 4 rule documents, resolved from the published metadata files."""
    import acquire_claimback_sources as acs

    out: dict[str, str] = {}
    for meta_path in acs.METADATA_FILES:
        local = CACHE / "claimback_sources" / meta_path.rsplit("/", 1)[1]
        if not local.exists():
            try:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(acs.fetch(acs.RAW + meta_path, 60))
            except Exception:                                           # noqa: BLE001
                continue
        attributes = json.loads(local.read_text(encoding="utf-8")).get("metadataAttributes", {})
        out[meta_path.rsplit("/", 1)[1].replace(".pdf.metadata.json", ".pdf")] = \
            attributes.get("source_url", "")
    return {k: v for k, v in out.items() if v}


def target_dir(source_id: str) -> pathlib.Path:
    return {
        "hospital_bills_public_sample": CACHE / "indian_bills",
        "irdai_wordings": CACHE / "wordings",
        "claimback_referenced_wordings": CACHE / "claimback_sources" / "wordings",
        "claimback_referenced_rules": CACHE / "claimback_sources" / "rules",
        "irdai_library": CACHE / "irdai_full",
    }[source_id]


def resolve(document: dict, mirror_index: dict[str, str], wording_urls: dict[str, str],
            claimback: dict[str, str]) -> str:
    source_id, name = document["source_id"], document["file_name"]
    if source_id == "hospital_bills_public_sample":
        return BILL_MIRROR + urllib.request.quote(name)
    if source_id == "irdai_wordings":
        return wording_urls.get(name, "")
    if source_id in ("claimback_referenced_wordings", "claimback_referenced_rules"):
        return claimback.get(name, "")
    if source_id == "irdai_library":
        path = mirror_index.get(name)
        return IRDAI_MIRROR + urllib.request.quote(path) if path else ""
    return ""


def fetch(url: str, timeout: int = 300) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", default="phase3e", choices=sorted(GROUPS))
    parser.add_argument("--only", default="", help="substring filter on the file name")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    wanted = set(GROUPS[args.group])
    documents = [d for d in frozen_documents() if d["source_id"] in wanted]
    if args.only:
        documents = [d for d in documents if args.only in d["file_name"]]
    print(f"frozen v1.0 documents in group {args.group}: {len(documents)}")

    mirror_index: dict[str, str] = {}
    wording_urls: dict[str, str] = {}
    claimback: dict[str, str] = {}
    if any(d["source_id"] == "irdai_library" for d in documents):
        mirror_index = irdai_mirror_index()
    if any(d["source_id"] == "irdai_wordings" for d in documents):
        wording_urls = irdai_wording_urls()
    if any(d["source_id"].startswith("claimback_referenced") for d in documents):
        claimback = claimback_urls()

    rows = []
    for document in sorted(documents, key=lambda d: (d["source_id"], d["file_name"])):
        destination = target_dir(document["source_id"]) / document["file_name"]
        url = resolve(document, mirror_index, wording_urls, claimback)
        record_base = {"document_id": document["document_id"], "source_id": document["source_id"],
                       "file_name": document["file_name"], "frozen_sha256": document["sha256"],
                       "url": url, "access_date": time.strftime("%Y-%m-%d", time.localtime())}

        if destination.exists():
            local = hashlib.sha256(destination.read_bytes()).hexdigest()
            status = "already_present" if local == document["sha256"] else "present_but_mismatched"
            rows.append({**record_base, "status": status, "sha256": local})
            print(f"  {status:>22}  {document['file_name']}")
            continue
        if args.dry:
            print(f"  would fetch  {document['file_name']}  <- {url[:90]}")
            continue
        if not url:
            rows.append({**record_base, "status": "no_recorded_url"})
            print(f"  {'no_recorded_url':>22}  {document['file_name']}")
            continue
        try:
            body = fetch(url)
        except Exception as exc:                                        # noqa: BLE001
            rows.append({**record_base, "status": "failed", "error": type(exc).__name__})
            print(f"  {'failed':>22}  {document['file_name']}: {type(exc).__name__}")
            time.sleep(0.4)
            continue
        digest = hashlib.sha256(body).hexdigest()
        if digest != document["sha256"]:
            # The file at that URL is not the file this release froze. It does not go in.
            rows.append({**record_base, "status": "hash_mismatch", "sha256": digest,
                         "bytes": len(body)})
            print(f"  {'hash_mismatch':>22}  {document['file_name']}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        rows.append({**record_base, "status": "rehydrated", "sha256": digest, "bytes": len(body)})
        print(f"  {'rehydrated':>22}  {document['file_name']}  {len(body)/1e3:.0f} kB")
        time.sleep(0.3)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    good = sum(1 for r in rows if r["status"] in ("rehydrated", "already_present"))
    print(f"verified against frozen hashes: {good}/{len(rows)}  manifest {MANIFEST.name}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.exit(main())
