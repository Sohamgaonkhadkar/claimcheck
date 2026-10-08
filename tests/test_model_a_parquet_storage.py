"""Storage-only checks for the Parquet representation of frozen Model A v2 rows.

These tests compare data bytes/rows and do not run model inference, baseline scoring, training, or
held-out evaluation. Reading the test split here is solely a storage-equivalence check.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from bench.datasets.model_a_v2_storage import load_storage_manifest, read_parquet_split
from bench.ml.model_a.runtime import compute_dataset_identity, read_jsonl

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "data/datasets/model_a/bill_head_normalization_v2"
EXPECTED_ROWS = {"train": 14_000, "validation": 3_000, "test": 3_000}
EXPECTED_DATASET_HASH = "57e6c15cd45931ff9b7bfda74a6ec776cc334b7beb5fd158afe93e1e4329cdab"


def _jsonl_rows(split: str) -> list[dict]:
    with (DATASET_DIR / f"{split}.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@pytest.mark.parametrize("split", ("train", "validation", "test"))
def test_active_parquet_is_ordered_semantic_copy_of_frozen_jsonl(split: str) -> None:
    storage = load_storage_manifest(DATASET_DIR)
    entry = storage["splits"][split]
    original_path = DATASET_DIR / f"{split}.jsonl"
    original_bytes = original_path.read_bytes()
    original = _jsonl_rows(split)

    # Passing the legacy logical path must dispatch to the active Parquet representation.
    active_rows = read_jsonl(original_path)
    explicit_parquet_rows = read_jsonl(DATASET_DIR / entry["path"])
    verified_rows = read_parquet_split(DATASET_DIR, split)

    assert len(original) == EXPECTED_ROWS[split] == len(active_rows)
    assert original == active_rows == explicit_parquet_rows == verified_rows
    assert [row["example_id"] for row in original] == [row["example_id"] for row in active_rows]
    assert [row["text"] for row in original] == [row["text"] for row in active_rows]
    assert [row["canonical_category"] for row in original] == [row["canonical_category"] for row in active_rows]
    assert [row["provenance"] for row in original] == [row["provenance"] for row in active_rows]
    assert [row["group_id"] for row in original] == [row["group_id"] for row in active_rows]
    assert [row["split"] for row in original] == [row["split"] for row in active_rows]
    assert all(row["split"] == split for row in active_rows)
    assert len({row["example_id"] for row in active_rows}) == EXPECTED_ROWS[split]

    assert hashlib.sha256(original_bytes).hexdigest() == entry["source_jsonl_sha256"]
    parquet_path = DATASET_DIR / entry["path"]
    assert hashlib.sha256(parquet_path.read_bytes()).hexdigest() == entry["sha256"]
    parquet = pq.ParquetFile(parquet_path)
    assert parquet.metadata.num_rows == EXPECTED_ROWS[split]
    arrow_schema = parquet.schema_arrow
    assert arrow_schema.names == [field["name"] for field in storage["schema"]["fields"]]
    assert hashlib.sha256(str(arrow_schema).encode("utf-8")).hexdigest() == storage["schema"]["sha256"]
    assert all(
        parquet.metadata.row_group(row_group).column(column).compression == "ZSTD"
        for row_group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(row_group).num_columns)
    )


def test_group_membership_is_unchanged_across_frozen_splits() -> None:
    split_manifest = json.loads((DATASET_DIR / "SPLIT-MANIFEST.json").read_text(encoding="utf-8"))
    assignment = {
        entry["group_id"]: entry["split"]
        for entry in split_manifest["group_assignments"]
    }
    seen_ids: set[str] = set()
    seen_groups: dict[str, set[str]] = {}
    for split in ("train", "validation", "test"):
        rows = read_parquet_split(DATASET_DIR, split)
        ids = {row["example_id"] for row in rows}
        groups = {row["group_id"] for row in rows}
        assert not (seen_ids & ids)
        seen_ids |= ids
        seen_groups[split] = groups
        assert all(assignment[row["group_id"]] == split for row in rows)
    assert not (seen_groups["train"] & seen_groups["validation"])
    assert not (seen_groups["train"] & seen_groups["test"])
    assert not (seen_groups["validation"] & seen_groups["test"])
    assert len(seen_ids) == 20_000


def test_training_identity_keeps_the_preconversion_frozen_hash() -> None:
    # The default verification reads train and validation only; it does not open test rows.
    identity = compute_dataset_identity(DATASET_DIR)
    assert identity["dataset_hash"] == EXPECTED_DATASET_HASH
    assert identity["split_file_sha256"]["train"] == "6a1fd18266fdacb0108f59c9aefe504e7b898734b765361aa123db8e44128ffb"
    assert identity["split_file_sha256"]["validation"] == "f12a8fde16543dfb98fa31f520e6b7ca75b9590e04bbe35f9fee66867679b491"
