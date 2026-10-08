"""The guided, browser-free review loop, driven exactly as a reviewer would drive it.

These tests feed keystrokes into `annotate_cli review` over stdin and then read the log back, so
what is being tested is the loop a human actually uses: show one item, collect the fields, validate,
save, advance, skip, quit. Every run writes to a temporary log directory — the gold tree is never the
target of a test, and one test asserts exactly that.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run(args: list[str], answers: list[str], log_dir: pathlib.Path, timeout: int = 900):
    return subprocess.run(
        [sys.executable, "-m", "bench.gold.annotate_cli", "review", *args, "--log-dir", str(log_dir)],
        input="\n".join(answers) + "\n", capture_output=True, text=True, cwd=ROOT, timeout=timeout)


def _records(log_dir: pathlib.Path, queue: str) -> list[dict]:
    path = log_dir / f"{queue}.log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def tmp_log(tmp_path) -> pathlib.Path:
    return tmp_path / "labels"


def _bill_id(n: int = 0) -> str:
    """A pending bill item the guided reviewer would actually be offered."""
    from bench.gold import annotate_cli

    rows, _ = annotate_cli._eligible_review_rows("bills", annotate_cli.pending("bills"))
    return str(rows[n]["item_id"])


def _ocr_id() -> str:
    from bench.gold import items

    return str(items.load_items("ocr")[0]["item_id"])


def _retrieval_id() -> str:
    from bench.gold import goldstore

    return str(goldstore.read_dataset("retrieval_candidates")[0]["candidate_id"])


# --------------------------------------------------------------------------- bills

def test_a_bill_line_can_be_answered_end_to_end(tmp_log) -> None:
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                 "--pass", "1", "--quick", "--yes"],
                ["1 ICU ROOM RENT CHARGES 21/11/2025", "1", "1", "45000.00"], tmp_log)
    assert proc.returncode == 0, proc.stdout[-2000:]
    rows = _records(tmp_log, "bills")
    assert len(rows) == 1
    row = rows[0]
    assert row["judgement"] == "bill_line"
    assert row["raw_head_text"] == "1 ICU ROOM RENT CHARGES 21/11/2025"
    assert row["canonical_head_class"] == "bed_charge"
    assert row["row_type"] == "CHARGE"
    assert row["amount_paise"] == 4500000
    assert row["amount_clarity"] == "clear"
    assert row["reviewer_id"] == "human-soham" and row["review_pass"] == "1"
    assert row["answer_channel"] == "cli"
    assert "progress:" in proc.stdout


def test_the_head_and_row_menus_accept_names_as_well_as_numbers(tmp_log) -> None:
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                 "--quick", "--yes"],
                ["IP REGISTRATION FEES", "administration", "NON_CHARGE", "100.00"], tmp_log)
    assert proc.returncode == 0
    row = _records(tmp_log, "bills")[0]
    assert row["canonical_head_class"] == "administration"
    assert row["row_type"] == "NON_CHARGE"


def test_amount_clarity_letters_are_honoured(tmp_log) -> None:
    for letter, clarity in (("a", "ambiguous"), ("u", "unreadable")):
        log_dir = tmp_log.parent / f"clarity-{letter}"
        proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                     "--quick", "--yes"], ["SOME CHARGE", "1", "1", letter], log_dir)
        assert proc.returncode == 0
        row = _records(log_dir, "bills")[0]
        assert row["amount_clarity"] == clarity
        assert row["amount_paise"] is None, "an ambiguous amount must never be recorded as a value"


def test_copying_the_machine_reading_is_recorded_as_such(tmp_log) -> None:
    """The hint may be copied, but only after an explicit confirmation, and the record says so."""
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                 "--quick", "--yes"],
                ["h", "y", "1", "1", "45000.00"], tmp_log)
    assert proc.returncode == 0, proc.stdout[-1500:]
    row = _records(tmp_log, "bills")[0]
    assert row["raw_head_text"], "the copied text must be the recorded value"
    assert "confirmed from hint" in row["annotation_notes"]
    assert row["source_verified"] is True


def test_an_invalid_answer_is_refused_and_nothing_is_written(tmp_log) -> None:
    """An empty raw head text cannot become gold — the loop redoes the fields instead."""
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                 "--quick", "--yes"],
                ["", "1", "1", "100", "q"], tmp_log)
    assert proc.returncode == 0
    assert "REFUSED by validation" in proc.stdout
    assert _records(tmp_log, "bills") == []


def test_skip_is_recorded_as_a_skip_and_never_as_an_answer(tmp_log) -> None:
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham", "--quick"],
                ["s"], tmp_log)
    assert proc.returncode == 0
    rows = _records(tmp_log, "bills")
    assert len(rows) == 1 and rows[0]["judgement"] == "skip"
    assert rows[0]["answer_channel"] == "cli"


def test_quitting_writes_nothing(tmp_log) -> None:
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham", "--quick"],
                ["q"], tmp_log)
    assert proc.returncode == 0
    assert "nothing else was written" in proc.stdout
    assert _records(tmp_log, "bills") == []


# --------------------------------------------------------------------------- retrieval

def test_retrieval_uses_the_number_keys_and_records_the_proposal_comparison(tmp_log) -> None:
    proc = _run(["--queue", "retrieval", "--item", _retrieval_id(), "--reviewer", "human-soham",
                 "--yes"], ["2", "y", ""], tmp_log)
    assert proc.returncode == 0, proc.stdout[-2000:]
    row = _records(tmp_log, "retrieval")[0]
    assert row["judgement"] == "relevance" and row["relevance_label"] == "NOT_RELEVANT"
    assert row["source_verified"] is True
    assert "answer_agrees_with_proposal" in row, "the proposal comparison must be recorded"
    assert row["proposal_shown_label"], "the proposal that was shown is recorded, not inferred"


def test_conditional_requires_a_note_and_keeps_asking(tmp_log) -> None:
    proc = _run(["--queue", "retrieval", "--item", _retrieval_id(), "--reviewer", "human-soham",
                 "--yes"], ["3", "y", "", "because the clause applies only to a listed condition"],
                tmp_log)
    assert proc.returncode == 0, proc.stdout[-1500:]
    row = _records(tmp_log, "retrieval")[0]
    assert row["relevance_label"] == "CONDITIONAL"
    assert row["annotation_notes"] == "because the clause applies only to a listed condition"


def test_source_not_verifiable_is_recorded_as_unverified(tmp_log) -> None:
    proc = _run(["--queue", "retrieval", "--item", _retrieval_id(), "--reviewer", "human-soham",
                 "--yes"], ["5", ""], tmp_log)
    assert proc.returncode == 0
    row = _records(tmp_log, "retrieval")[0]
    assert row["relevance_label"] == "SOURCE_NOT_VERIFIABLE"
    assert row["source_verified"] is False, "an unverifiable source must not be marked verified"


# --------------------------------------------------------------------------- ocr

def test_ocr_takes_a_multi_line_transcription(tmp_log) -> None:
    proc = _run(["--queue", "ocr", "--item", _ocr_id(), "--reviewer", "human-soham", "--yes"],
                ["ROOM CHARGES SubTotal : 45000.00", "1 ICU ROOM RENT CHARGES 3 15000.00 0.00 "
                 "45000.00", ".", "n", "y", ""], tmp_log)
    assert proc.returncode == 0, proc.stdout[-2000:]
    row = _records(tmp_log, "ocr")[0]
    assert row["judgement"] == "ocr_transcription"
    assert row["transcription"].startswith("ROOM CHARGES SubTotal")
    assert "\n" in row["transcription"], "line breaks in the page must survive"
    assert row["transcription_partial"] is False
    assert row["compared_to_image"] is True and row["source_verified"] is True


def test_ocr_partial_is_recorded_when_asked(tmp_log) -> None:
    proc = _run(["--queue", "ocr", "--item", _ocr_id(), "--reviewer", "human-soham", "--yes"],
                ["illegible digits here", ".", "y", "y", "two amounts are smudged"], tmp_log)
    assert proc.returncode == 0
    row = _records(tmp_log, "ocr")[0]
    assert row["transcription_partial"] is True
    assert row["annotation_notes"] == "two amounts are smudged"


# --------------------------------------------------------------------------- the loop itself

def test_answered_items_are_not_offered_again(tmp_path, monkeypatch) -> None:
    """`review` without --redo walks only what is still owed."""
    import argparse

    from bench.gold import annotate_cli, goldstore, review_batch, viewer

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    item_id = _bill_id()
    viewer.append_label("bills", {"item_id": item_id, "reviewer_id": "human-soham",
                                  "review_pass": "1", "judgement": "bill_line",
                                  "raw_head_text": "ICU ROOM RENT", "canonical_head_class":
                                  "bed_charge", "row_type": "CHARGE", "amount_paise": 4500000,
                                  "amount_clarity": "clear", "source_verified": True}, channel="cli")
    args = argparse.Namespace(item="", redo=False, limit=0, queue="bills")
    offered = {row["item_id"] for row in annotate_cli._reviewable("bills", args)}
    assert item_id not in offered
    eligible, _ = annotate_cli._eligible_review_rows("bills", annotate_cli.queue_rows("bills"))
    size = len(eligible)
    assert len(offered) == size - 1
    args.redo = True
    offered = {row["item_id"] for row in annotate_cli._reviewable("bills", args)}
    assert item_id in offered and len(offered) == size


def test_a_stray_record_does_not_block_the_item_from_being_reviewed(tmp_path, monkeypatch) -> None:
    """A record with no answer channel is not an answer, so the item is still owed a real one.

    The stray record is written here into a fixture log rather than read from the real one: the
    property under test is about a channel-less record, and a fixture states it without depending
    on an incident staying in the gold log.
    """
    import argparse

    from bench.gold import annotate_cli, goldstore

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    item_id = _bill_id()
    goldstore.append_log("bills", {"item_id": item_id, "reviewer_id": "human-1", "review_pass": "1",
                                   "judgement": "bill_line", "raw_head_text": "from a test harness",
                                   "recorded_at": "2026-10-04T18:53:56+0000",
                                   "reviewer_kind": "HUMAN"})
    args = argparse.Namespace(item="", redo=False, limit=0, queue="bills")
    offered = {row["item_id"] for row in annotate_cli._reviewable("bills", args)}
    assert item_id in offered, "an item whose only record has unknown provenance is still pending"


def test_the_gold_logs_are_never_written_by_a_guided_run(tmp_log) -> None:
    """The whole point: a guided test writes to its own log directory and nothing else."""
    from bench.gold import goldstore

    before = {path.name: path.read_text(encoding="utf-8") if path.exists() else ""
              for path in sorted(goldstore.ANNOTATION_LOGS.glob("*.log.jsonl"))}
    proc = _run(["--queue", "bills", "--item", _bill_id(), "--reviewer", "human-soham",
                 "--quick", "--yes"], ["ICU ROOM RENT", "1", "1", "45000.00"], tmp_log)
    assert proc.returncode == 0
    after = {path.name: path.read_text(encoding="utf-8") if path.exists() else ""
             for path in sorted(goldstore.ANNOTATION_LOGS.glob("*.log.jsonl"))}
    assert before == after, "the gold logs must be byte-identical after a guided test"


# --------------------------------------------------------------------------- the fixture instance

def test_fixture_mode_writes_outside_the_gold_tree(tmp_path, monkeypatch) -> None:
    """The bug found while verifying the fixture flag: it must redirect the *write* path.

    `append_label` resolves its file through `goldstore.ANNOTATION_LOGS`. Redirecting the display
    variable (`viewer.LABEL_DIR`) alone left a running fixture instance writing answers into the
    real gold log — which happened once, with a scripted POST, and is recorded in
    bench/results/gold/incidents/. This asserts the redirect actually takes effect.
    """
    from bench.gold import goldstore, viewer

    target = tmp_path / "fixture"
    target.mkdir()
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", gold_dir)

    # what main(--log-dir ...) does: redirect the path the write resolves through
    goldstore.ANNOTATION_LOGS = target
    viewer.append_label("bills", {"item_id": _bill_id(), "reviewer_id": "human-fixture",
                                  "review_pass": "1", "judgement": "bill_line",
                                  "raw_head_text": "ROOM RENT",
                                  "canonical_head_class": "bed_charge",
                                  "row_type": "CHARGE", "amount_paise": 100,
                                  "amount_clarity": "clear", "source_verified": True},
                        channel="web")
    assert (target / "bills.log.jsonl").exists(), "the answer must land in the fixture directory"
    assert not (gold_dir / "bills.log.jsonl").exists(), "the gold directory must stay empty"


def test_the_gold_answer_logs_hold_exactly_what_they_should() -> None:
    """A guard against any future stray write: the gold logs are pinned by content, not by count."""
    import json as _json

    from bench.gold import goldstore

    expected = {
        "retrieval": [],
        "bills": [{"reviewer_id": "human-1", "item_id": "B-BL-297935975AC134BA",
                   "answer_channel": None}],
        "ocr": [{"reviewer_id": "human-1", "item_id": "B-BL-297935975AC134BA",
                 "answer_channel": None, "judgement": "ocr_transcription"}],
    }
    for queue, wanted in expected.items():
        rows = goldstore.read_log(queue)
        assert len(rows) == len(wanted), (
            f"{queue}.log.jsonl holds {len(rows)} records but the workspace should hold {len(wanted)}: "
            f"{[(r.get('reviewer_id'), r.get('recorded_at')) for r in rows]}")
        for row, want in zip(rows, wanted):
            assert row.get("reviewer_id") == want["reviewer_id"]
            assert row.get("item_id") == want["item_id"]
            assert row.get("answer_channel") == want["answer_channel"]
            if "judgement" in want:
                assert row.get("judgement") == want["judgement"]


# --------------------------------------------------------------------------- no-answer-leakage rules

def test_the_batch_manifest_example_contains_no_real_item_id(tmp_path) -> None:
    """An example built from real data primes the reviewer; the example must be fiction only."""
    from bench.gold import review_batch

    manifest = review_batch.build("bills", 1, tmp_path / "batch", reviewer="human-soham",
                                  review_pass="1")
    example = manifest["answer_format"]
    assert "EXAMPLE-000" in example, "the example must be visibly fictional"
    real_id = manifest["items"][0]["item_id"]
    assert real_id not in example, "the example must not name a real pending item"
    assert "[" in example and "your answer" in example, "the example must contain no field values"


def test_the_cli_help_shows_no_real_item_id_and_no_suggested_values() -> None:
    from bench.gold import annotate_cli

    text = annotate_cli.__doc__ or ""
    assert "B-BL-" not in text, "the help text must not name a real review item"
    assert "<item id>" in text
    for value in ("ICU ROOM RENT", "bed_charge", "45000"):
        assert value not in text, f"the help text must not suggest a value ({value!r})"


# --------------------------------------------------------------------------- contamination screen

def test_the_screen_catches_an_exposed_answer_and_passes_a_coincidence() -> None:
    """A money value counts as a leak only when it sits next to that row's wording or document.

    The first version of this test was itself wrong: its "coincidence" string still placed the value
    inside the proximity window, so the screen rejected it — correctly. Distance is the point.
    """
    from bench.gold import review_batch

    item = {"item_id": "B-BL-TESTONLY", "document_id": "hospital_bill-deadbeef0000",
            "file_name": "sample_x.pdf",
            "parser_hint": {"raw_label": "12 WIDGET REPAIR CHARGE 01/01/2025 1 4,500.00 0.00 "
                                         "4,500.00", "amount_paise": 450000}}
    exposed = ("note for the reviewer: 12 WIDGET REPAIR CHARGE 01/01/2025 1 4,500.00 0.00 4,500.00 "
               "was answered earlier in this document")
    verdict = review_batch.screen_item(item, exposed)
    assert verdict["verdict"] == "REJECT", verdict

    # the same value, far away from every anchor, reveals nothing about this row
    coincidence = ("The dataset holds 4,500.00 synthetic rows. " + "padding. " * 40 +
                   "A separate paragraph mentions sample_x.pdf in passing.")
    verdict = review_batch.screen_item(item, coincidence)
    assert verdict["verdict"] == "OK", verdict["reasons"]

    assert review_batch.screen_item(item, "nothing relevant here at all")["verdict"] == "OK"


def test_the_screen_rejects_any_mention_of_the_item_id() -> None:
    from bench.gold import review_batch

    item = {"item_id": "B-BL-ABCDEF0123456789", "document_id": "d", "file_name": "f.pdf",
            "parser_hint": {}}
    assert review_batch.screen_item(item, "see B-BL-ABCDEF0123456789")["verdict"] == "REJECT"


def test_every_recorded_exclusion_states_a_reason_and_is_skipped() -> None:
    from bench.gold import review_batch

    excluded = review_batch.load_exclusions()
    assert excluded, "this workspace has a recorded exclusion"
    for item_id, record in excluded.items():
        assert record.get("reason"), f"{item_id} must state why it is excluded"
        assert item_id.count("-") >= 2 and item_id.endswith(tuple("0123456789ABCDEF"))
    screened = {row["item_id"]: row for row in review_batch.contaminated_items("bills")}
    for item_id in excluded:
        if item_id in screened:
            assert screened[item_id]["verdict"] == "REJECT"
            assert "recorded exclusion" in screened[item_id]["reasons"][0]


def test_real_handoff_refuses_ocr_before_bill_target(tmp_path) -> None:
    """Stage order is enforced for the real log; this command must write nothing."""
    proc = subprocess.run(
        [sys.executable, "-m", "bench.gold.annotate_cli", "review", "--queue", "ocr",
         "--reviewer", "human-soham", "--pass", "1", "--limit", "1"],
        cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert proc.returncode == 2
    assert "OCR starts only after 100 valid human bill-line labels" in proc.stdout


def test_real_single_answer_path_cannot_bypass_ocr_stage_gate() -> None:
    """The low-level answer command obeys the same real-log queue order as guided review."""
    proc = subprocess.run(
        [sys.executable, "-m", "bench.gold.annotate_cli", "answer", _ocr_id(),
         "--queue", "ocr", "--reviewer", "human-soham", "--pass", "1",
         "--transcription", "FIXTURE ONLY", "--compared"],
        cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert proc.returncode == 2
    assert "OCR starts only after 100 valid human bill-line labels" in proc.stdout
