"""Safety tests for the research-only Model A proposal adapter."""
from __future__ import annotations

from pathlib import Path

import pytest

from bench.ml.model_a.proposal_adapter import (
    CONTROLLED_SYNTHETIC_CONTEXT,
    ModelAProposalAdapter,
)
from bench.ml.model_a.runtime import read_jsonl

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "data/datasets/model_a/bill_head_normalization_v2"


def test_accepted_deterministic_result_is_never_replaced() -> None:
    adapter = ModelAProposalAdapter(ROOT / "models/model_a")
    row = next(row for row in read_jsonl(DATASET_DIR / "train.jsonl")
               if row["canonical_category"] == "room")
    result = adapter.propose(
        source_text=row["text"],
        example_id=row["example_id"],
        context_kind=CONTROLLED_SYNTHETIC_CONTEXT,
        deterministic_accepted=True,
        deterministic_candidate="room",
    )
    assert result["status"] == "SKIPPED_DETERMINISTIC_ACCEPTED"
    assert result["proposal"] is None
    assert result["deterministic_candidate"] == "room"
    assert result["authoritative"] is False
    assert result["production_worker_connected"] is False


def test_unresolved_result_returns_only_a_non_authoritative_proposal() -> None:
    adapter = ModelAProposalAdapter(ROOT / "models/model_a")
    row = next(row for row in read_jsonl(DATASET_DIR / "train.jsonl")
               if row["canonical_category"] == "room"
               and row["provenance"]["surface_variant_id"] == "authored_base")
    result = adapter.propose(
        source_text=row["text"],
        example_id=row["example_id"],
        context_kind=CONTROLLED_SYNTHETIC_CONTEXT,
        deterministic_accepted=False,
        deterministic_candidate=None,
    )
    assert result["status"] == "PROPOSED"
    assert result["proposal"]["candidate_value"] == "room"
    assert result["proposal"]["source_text"] == row["text"]
    assert result["proposal"]["example_id"] == row["example_id"]
    assert result["proposal"]["model_name"] == adapter.model.model_name
    assert result["proposal"]["model_version"] == adapter.model.model_version
    assert "source_span" not in result["proposal"]
    assert result["authoritative"] is False
    assert result["production_worker_connected"] is False
    assert result["future_real_document_requirements"]


def test_real_document_context_is_rejected() -> None:
    adapter = ModelAProposalAdapter(ROOT / "models/model_a")
    with pytest.raises(ValueError, match="controlled synthetic examples only"):
        adapter.propose(
            source_text="room charges",
            example_id="doc-1/page-1/line-1",
            context_kind="real_document",
            deterministic_accepted=False,
            deterministic_candidate=None,
        )
