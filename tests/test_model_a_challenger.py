"""Model A v2 challenger artifact, registration and proposal-gate tests.

These tests use only the frozen v2 train/validation splits or small explicit sentinel inputs; they
never open the held-out test JSONL.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bench.ml.model_a.runtime import (
    load_registered_model,
    predict_candidate,
    predict_candidates,
    preprocess_text,
    read_json,
    read_jsonl,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "data/datasets/model_a/bill_head_normalization_v2"
MODEL_ROOT = ROOT / "models/model_a"


def _known_train_example() -> dict:
    rows = read_jsonl(DATASET_DIR / "train.jsonl")
    return next(row for row in rows
                if row["canonical_category"] == "room"
                and row["provenance"]["surface_variant_id"] == "authored_base")


def test_registered_artifact_loads_and_predicts_known_train_example() -> None:
    model = load_registered_model(MODEL_ROOT)
    row = _known_train_example()
    prediction = predict_candidate(model, row["text"], row["example_id"])
    assert prediction["candidate_value"] == row["canonical_category"]
    assert prediction["confidence"] >= model.confidence_threshold
    assert prediction["model_name"] == model.model_name
    assert prediction["model_version"] == model.model_version
    assert prediction["source_text"] == row["text"]
    assert prediction["example_id"] == row["example_id"]
    assert prediction["confidence_type"]
    assert sha256_file(MODEL_ROOT / model.registration["artifact_path"]) == model.registration["artifact_sha256"]


def test_label_map_mismatch_fails_closed(tmp_path: Path) -> None:
    copy_root = tmp_path / "model_a"
    shutil.copytree(MODEL_ROOT, copy_root)
    registry_path = copy_root / "manifest.json"
    registry = read_json(registry_path)
    registration = registry["registered_models"][0]
    label_path = copy_root / registration["label_map_path"]
    label_map = read_json(label_path)
    label_map["categories"] = list(reversed(label_map["categories"]))
    label_path.write_text(json.dumps(label_map, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    registration["label_map_sha256"] = sha256_file(label_path)
    registry["registered_models"][0] = registration
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="classes and registered label map"):
        load_registered_model(copy_root)


def test_corrupt_artifact_hash_fails_before_unpickling(tmp_path: Path) -> None:
    copy_root = tmp_path / "model_a"
    shutil.copytree(MODEL_ROOT, copy_root)
    registry_path = copy_root / "manifest.json"
    registry = read_json(registry_path)
    registration = registry["registered_models"][0]
    artifact = copy_root / registration["artifact_path"]
    artifact.write_bytes(artifact.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_registered_model(copy_root)


def test_preprocessing_is_deterministic_and_noncorrective() -> None:
    raw = "I.C.U. / HbA1c—Post-operative"
    assert preprocess_text(raw, "raw_case_preserving") == raw
    assert preprocess_text(raw, "raw_lowercase") == raw
    expected = "i c u hba1c post operative"
    assert preprocess_text(raw, "normalized_nfkc_casefold_punctuation_to_spaces") == expected
    assert preprocess_text(raw, "normalized_nfkc_casefold_punctuation_to_spaces") == expected
    with pytest.raises(ValueError, match="unsupported Model A preprocessing"):
        preprocess_text(raw, "spell-correct-everything")


def test_unknown_and_low_confidence_inputs_abstain_to_unmapped() -> None:
    model = load_registered_model(MODEL_ROOT)
    unknown = predict_candidate(model, "🪐🪐🪐🪐", "out-of-vocabulary-sentinel")
    assert unknown["candidate_value"] == "UNMAPPED"
    assert unknown["abstained"] is True

    validation = read_jsonl(DATASET_DIR / "validation.jsonl")
    scores = predict_candidates(model, [row["text"] for row in validation],
                                [row["example_id"] for row in validation])
    lowest = min(scores, key=lambda item: item["confidence"])
    assert lowest["confidence"] < 1.0
    old_threshold = model.registry["confidence_threshold"]
    model.registry["confidence_threshold"] = min(1.0, lowest["confidence"] + 1e-9)
    try:
        low = predict_candidate(model, lowest["source_text"], lowest["example_id"])
        assert low["candidate_value"] == "UNMAPPED"
        assert low["abstention_reason"] == "abstain_below_validation_threshold"
    finally:
        model.registry["confidence_threshold"] = old_threshold


def test_only_registered_model_version_can_load() -> None:
    model = load_registered_model(MODEL_ROOT)
    assert model.model_version in {entry["model_version"] for entry in model.registry["registered_models"]}
    with pytest.raises(ValueError, match="not uniquely registered"):
        load_registered_model(MODEL_ROOT, "v999.0.0")


def test_model_manifest_has_reproducibility_and_decision_metadata() -> None:
    registry = read_json(MODEL_ROOT / "manifest.json")
    for field in (
        "model_id", "model_name", "model_version", "task", "algorithm", "features",
        "preprocessing", "label_map_version", "dataset_id", "dataset_version", "dataset_hash",
        "train_split_hash", "validation_split_hash", "test_split_hash", "seed",
        "hyperparameters", "validation_metrics", "test_metrics", "artifact_sha256",
        "code_revision", "runtime", "dependency_lock", "confidence_threshold", "limitations",
    ):
        assert field in registry
    lock_path = ROOT / registry["dependency_lock"]["path"]
    assert sha256_file(lock_path) == registry["dependency_lock"]["sha256"]
    assert registry["model_version"] == registry["registered_models"][0]["model_version"]
    assert registry["training_config_sha256"]
    assert registry["code_revision"]["code_sha256"]
    if registry.get("test_evaluation_status") == "COMPLETED_ONCE":
        assert registry["MODEL_A_CHALLENGER_WON"] is registry["challenger_won"]
        adapter = registry["proposal_adapter"]
        assert sha256_file(ROOT / adapter["path"]) == adapter["sha256"]
        assert adapter["worker_integrated"] is False
        assert adapter["never_overrides_accepted_deterministic_result"] is True
        assert registry["inference_eligibility_scope"] == "CONTROLLED_SYNTHETIC_PROPOSAL_ADAPTER_ONLY"
        assert registry["test_metrics"] is not None
    else:
        assert registry["registry_status"] == "TRAINED_VALIDATION_SELECTED_TEST_PENDING"
        assert registry["test_metrics"] is None
    assert registry["registered_models"][0]["registration_status"] in {
        "REGISTERED_BENCHMARK_CHALLENGER_TEST_PENDING",
        "REGISTERED_BENCHMARK_ONLY",
        "REGISTERED_PROPOSAL_CHALLENGER",
    }


def test_inference_eligibility_is_explicit_and_never_implicit() -> None:
    registry = read_json(MODEL_ROOT / "manifest.json")
    if registry["inference_eligible"]:
        load_registered_model(MODEL_ROOT, require_inference_eligible=True)
    else:
        with pytest.raises(RuntimeError, match="benchmark-only"):
            load_registered_model(MODEL_ROOT, require_inference_eligible=True)
