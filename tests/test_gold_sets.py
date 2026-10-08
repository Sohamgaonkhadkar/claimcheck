"""Gold-tree behaviour: a label must be readable, attributable, and never quietly a machine's.

These tests protect the rules that make the gold sets usable as evidence — that a reviewer answer
can never be mistaken for a proposal, that the retrieval gold is compiled from the log rather than
from the proposals, and that an empty gold set reports itself as unmeasured instead of zero.
"""

from __future__ import annotations

import json


def test_a_logged_answer_is_attributed_to_a_human_reviewer(tmp_path, monkeypatch) -> None:
    from bench.gold import goldstore

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    goldstore.append_log("retrieval", {"queue": "retrieval", "item_id": "C-1",
                                       "reviewer_id": "human-1",
                                       "relevance_label": "RELEVANT"})
    rows = goldstore.read_log("retrieval")
    assert len(rows) == 1
    assert rows[0]["reviewer_id"] == "human-1"
    assert rows[0]["relevance_label"] == "RELEVANT"


def test_the_log_is_append_only_and_keeps_every_answer(tmp_path, monkeypatch) -> None:
    """An override in pass 2 must not erase the pass-1 answer: disagreements need both."""
    from bench.gold import goldstore

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    goldstore.append_log("retrieval", {"item_id": "C-1", "reviewer_id": "human-1",
                                       "relevance_label": "RELEVANT"})
    goldstore.append_log("retrieval", {"item_id": "C-1", "reviewer_id": "human-2",
                                       "relevance_label": "NOT_RELEVANT"})
    rows = goldstore.read_log("retrieval")
    assert [row["relevance_label"] for row in rows] == ["RELEVANT", "NOT_RELEVANT"]


def test_a_proposal_is_marked_as_not_a_human_label() -> None:
    from bench.gold import prelabels

    proposals = prelabels.load_prelabels()
    assert proposals, "pre-labels must exist for the reviewer to accept or override"
    for proposal in proposals.values():
        assert proposal["prelabel_is_a_human_label"] is False
        assert proposal["prelabel_source"]
        assert proposal["proposed_relevance_label"] in ("RELEVANT", "NOT_RELEVANT", "UNCLEAR")


def test_a_concept_term_alone_does_not_make_a_proposal_relevant() -> None:
    """The heuristic must be able to say UNCLEAR; a matcher that only ever says yes is useless."""
    from bench.gold import prelabels

    vague = {"candidate_id": "X",
             "query": "When is the waiting period counted from for a maternity claim made after "
                      "porting and renewal?",
             "query_head": "maternity",
             "positive_text": "Maternity expenses means the expenses incurred in respect of "
                              "delivery.",
             "source_verification": {"status": "exact"}}
    proposal = prelabels.propose(vague)
    assert proposal["proposed_relevance_label"] == "UNCLEAR"
    assert proposal["confidence"] == "low"


def test_a_source_not_verifiable_answer_is_never_a_positive() -> None:
    """Only RELEVANT + source_verified counts; the other four answers are recorded, not promoted."""
    from bench.gold.labels import NON_POSITIVE

    assert set(NON_POSITIVE) == {"NOT_RELEVANT", "CONDITIONAL", "UNCLEAR",
                                 "SOURCE_NOT_VERIFIABLE"}


def test_empty_gold_reports_unmeasured_not_zero() -> None:
    """A metric with no gold has not been measured. Reporting 0.0 would read as a failing system."""
    from eval import run_gold

    report = run_gold.run(write=False)
    assert report["retrieval"]["status"] in ("not_measured", "no_positives", "measured")
    if report["retrieval"]["status"] == "not_measured":
        assert report["retrieval"]["reason"]
    assert report["synthetic_excluded"] is True


def test_the_catalogue_marks_machine_sets_as_not_gold() -> None:
    from bench.datasets import store

    catalog = json.loads((store.DATASETS / "catalog.json").read_text(encoding="utf-8"))
    rows = {row["dataset_id"]: row for row in catalog["datasets"]}
    for dataset_id in ("retrieval_candidates", "retrieval_prelabels"):
        if dataset_id in rows:
            assert "not_gold" in rows[dataset_id]["notes"]
            assert "reviewer_status=none" in rows[dataset_id]["notes"]
    if "retrieval_gold" in rows:
        assert rows["retrieval_gold"]["version"] == "v1.1"
        assert "source_version=v1.0" in rows["retrieval_gold"]["notes"]


def test_second_pass_includes_every_disagreement_and_a_fixed_sample() -> None:
    from bench.gold import second_pass

    rows = [{"id": f"RG-{i:03d}", "query_head": "co_pay" if i % 2 else "room_rent",
             "agreement": "disagree" if i < 3 else "agree"} for i in range(40)]
    payload = second_pass.plan(rows)
    assert set(payload["disagreements"]) <= set(payload["second_pass_items"])
    assert len(payload["disagreements"]) == 3
    assert 0 < payload["agreements_sampled"] < 37
    again = second_pass.plan(list(reversed(rows)))
    assert again["second_pass_items"] == payload["second_pass_items"], \
        "the sample must not depend on file order"


def test_bill_extraction_metrics_count_invented_numbers() -> None:
    """A number reported where the reviewer could read none is the failure this metric exists for."""
    from eval import bill_metrics

    rows = [
        {"document_id": "d", "row_type": "CHARGE", "canonical_head_class": "bed_charge",
         "amount_paise": None, "amount_clarity": "unreadable", "rate_paise": None,
         "quantity_raw": "", "parser_hint": {"raw_label": "BED CHARGE", "amount_paise": 150000}},
        {"document_id": "d", "row_type": "CHARGE", "canonical_head_class": "bed_charge",
         "amount_paise": 150000, "amount_clarity": "clear", "rate_paise": 150000,
         "quantity_raw": "1", "parser_hint": {"raw_label": "BED CHARGE", "amount_paise": 150000,
                                              "rate_paise": 150000, "quantity_milli": 1000}},
    ]
    metrics = bill_metrics.evaluate(rows)
    assert metrics["false_monetary_extraction_rate"] == 1.0
    assert metrics["amount_exact_match"] == 1.0
    assert metrics["amount_checked"] == 1


def test_ocr_metrics_exclude_a_partial_transcription_from_cer() -> None:
    from eval import ocr_metrics

    rows = [
        {"item_id": "O-1", "document_id": "d", "page_number": 1, "transcription": "Total 1,150.00",
         "transcription_mechanical": "Total 1,1500.00", "transcription_partial": True},
        {"item_id": "O-2", "document_id": "d", "page_number": 2, "transcription": "Total 1,150.00",
         "transcription_mechanical": "Total 1,150.00", "transcription_partial": False},
    ]
    metrics = ocr_metrics.evaluate(rows)
    assert metrics["pages_scored_for_cer"] == 1
    assert metrics["pages_excluded_partial"] == 1
    # page 1 is excluded from CER but still carries a money token that does not match: the money
    # metric is what catches a changed decimal, so it is not excused by the exclusion
    assert metrics["money_token_exact_match"] == 0.5
    assert metrics["cer"] == 0.0


def test_a_money_token_that_changes_shape_is_not_an_exact_match() -> None:
    from eval import ocr_metrics

    assert ocr_metrics.money_tokens("Rs. 1,150.00") == ocr_metrics.money_tokens("1150.00")
    assert ocr_metrics.money_tokens("1,1500.00") != ocr_metrics.money_tokens("1,150.00")


def test_span_accuracy_is_not_satisfied_by_the_right_value_in_the_wrong_place() -> None:
    from eval import span_metrics

    assert span_metrics.overlap_ratio([0, 10], [0, 10]) == 1.0
    assert span_metrics.overlap_ratio([0, 10], [20, 30]) == 0.0
    assert 0 < span_metrics.overlap_ratio([0, 10], [5, 15]) < 1


def test_a_skip_is_recorded_and_counted_apart_from_an_answer() -> None:
    """'Not done yet' and 'decided not to decide' must never be the same number."""
    from bench.gold import labels

    answers = [
        {"item_id": "C-1", "reviewer_id": "human-1", "review_pass": "1", "judgement": "skip",
         "recorded_at": "2026-10-04T00:00:01+0000"},
        {"item_id": "C-2", "reviewer_id": "human-1", "review_pass": "1", "judgement": "relevance",
         "relevance_label": "NOT_RELEVANT", "recorded_at": "2026-10-04T00:00:02+0000"},
    ]
    rows, _unresolved, accounting = labels.build_retrieval_rows({}, answers)
    assert accounting["skipped_items"] == 1
    assert accounting["answered_items"] == 1
    assert rows == [], "a skipped item is not a gold row"


def test_skipping_an_item_that_was_later_answered_counts_as_answered() -> None:
    from bench.gold import labels

    answers = [
        {"item_id": "C-1", "reviewer_id": "human-1", "review_pass": "1", "judgement": "skip",
         "recorded_at": "2026-10-04T00:00:01+0000"},
        {"item_id": "C-1", "reviewer_id": "human-1", "review_pass": "2", "judgement": "relevance",
         "relevance_label": "UNCLEAR", "annotation_notes": "not clear from the wording",
         "recorded_at": "2026-10-04T00:00:02+0000"},
    ]
    _rows, _unresolved, accounting = labels.build_retrieval_rows({}, answers)
    assert accounting["skipped_items"] == 0
    assert accounting["answered_items"] == 1


def test_a_re_answer_replaces_the_earlier_one_without_erasing_it() -> None:
    from bench.gold import labels

    candidate = {"C-1": {"candidate_id": "C-1", "query": "q", "query_head": "co_pay",
                         "document_id": "d", "document_type": "policy_wording",
                         "file_name": "f.pdf", "clause_id": "cl", "page_number": 1,
                         "char_start": 0, "char_end": 5, "positive_span_id": "SP",
                         "positive_text": "text", "positive_text_sha16": "s",
                         "hard_negatives": [], "source_verification": {"status": "exact"}}}
    answers = [
        {"item_id": "C-1", "reviewer_id": "human-1", "review_pass": "1", "judgement": "relevance",
         "relevance_label": "RELEVANT", "source_verified": True,
         "recorded_at": "2026-10-04T00:00:01+0000"},
        {"item_id": "C-1", "reviewer_id": "human-1", "review_pass": "1", "judgement": "relevance",
         "relevance_label": "CONDITIONAL", "source_verified": True, "annotation_notes": "on review",
         "recorded_at": "2026-10-04T00:00:02+0000"},
    ]
    rows, _unresolved, _accounting = labels.build_retrieval_rows(candidate, answers)
    assert len(rows) == 1
    assert rows[0]["relevance_label"] == "CONDITIONAL"
    assert rows[0]["is_positive"] is False


def test_a_machine_answer_cannot_become_gold_even_with_a_human_looking_record() -> None:
    from bench.gold import labels

    candidate = {"C-1": {"candidate_id": "C-1", "query": "q", "query_head": "co_pay",
                         "document_id": "d", "document_type": "policy_wording",
                         "file_name": "f.pdf", "clause_id": "cl", "page_number": 1,
                         "char_start": 0, "char_end": 5, "positive_span_id": "SP",
                         "positive_text": "text", "positive_text_sha16": "s",
                         "hard_negatives": [], "source_verification": {"status": "exact"}}}
    # a record with no reviewer_kind field at all, as an older log would have written it
    rows, _unresolved, accounting = labels.build_retrieval_rows(candidate, [
        {"item_id": "C-1", "reviewer_id": "ai-prelabel", "review_pass": "1",
         "judgement": "relevance", "relevance_label": "RELEVANT", "source_verified": True,
         "recorded_at": "2026-10-04T00:00:01+0000"}])
    assert rows == []
    assert accounting["records_from_non_human_reviewers"] == 1


def test_the_server_refuses_a_mis_paired_answer() -> None:
    """A stale screen must not be able to file a bill line as an OCR page."""
    from bench.gold import viewer

    ok, error = viewer.validate_answer(
        {"item_id": "B-BL-1", "reviewer_id": "human-1", "review_pass": "1",
         "judgement": "ocr_transcription", "transcription": "TOTAL 250.00"},
        queue="ocr", item_ids={"O-1"})
    assert ok is False
    assert "not in the 'ocr' queue" in error
    ok, error = viewer.validate_answer(
        {"item_id": "O-1", "reviewer_id": "human-1", "review_pass": "1",
         "judgement": "relevance", "relevance_label": "RELEVANT"},
        queue="ocr", item_ids={"O-1"})
    assert ok is False and "accepts judgements" in error


def test_an_empty_transcription_is_refused() -> None:
    from bench.gold import viewer

    ok, error = viewer.validate_answer(
        {"item_id": "O-1", "reviewer_id": "human-1", "review_pass": "1",
         "judgement": "ocr_transcription", "transcription": "   "},
        queue="ocr", item_ids={"O-1"})
    assert ok is False and "empty transcription" in error


def test_records_outside_the_queue_are_rejected_with_a_reason_not_deleted() -> None:
    from bench.gold import labels

    kept, rejected = labels.reject_records(
        [{"item_id": "B-BL-1", "judgement": "ocr_transcription", "transcription": "x"},
         {"item_id": "O-1", "judgement": "ocr_transcription", "transcription": " "},
         {"item_id": "O-2", "judgement": "ocr_transcription", "transcription": "ok"}],
        {"O-1", "O-2"}, content_field="transcription")
    assert [row["item_id"] for row in kept] == ["O-2"]
    assert [entry["reason"] for entry in rejected] == ["item_not_in_this_queue",
                                                       "empty_transcription"]


def test_the_readiness_check_covers_the_seven_preconditions() -> None:
    from bench.gold import readiness

    payload = readiness.run(write=False)
    names = {result["check"] for result in payload["checks"]}
    assert names == {"make_review_opens_the_ui", "progress_persisted_immediately",
                     "queues_match_the_milestone", "reviewer_and_pass_identifiers_enforced",
                     "labels_cannot_silently_become_machine_labels",
                     "skipped_is_distinguishable_from_unanswered",
                     "dependencies_declared_and_importable"}
    assert payload["ready_for_review"] is True, payload["failed"]
