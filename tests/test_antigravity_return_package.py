"""The Antigravity return boundary stays portable and append-only.

Every label written in these tests goes to a per-test temporary log directory. These are fixture
records, not human answers, and they never touch the real gold logs.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench.gold import annotate_cli, goldstore, import_human_labels, return_package, viewer


def _eligible_item(queue: str) -> str:
    manifest = json.loads((goldstore.store.ROOT / "docs" /
                           "ANTIGRAVITY-ANNOTATION-MANIFEST.json").read_text(encoding="utf-8"))
    name = {"bills": "bill_lines", "ocr": "ocr", "retrieval": "retrieval"}[queue]
    return next(row["item_id"] for row in manifest["queues"][name]["items"]
                if row["annotation_eligibility"] == "eligible")


def _fixture_answer(queue: str, item_id: str) -> dict:
    base = {"item_id": item_id, "reviewer_id": "human-soham", "review_pass": "1",
            "adjudication_status": "fixture_only"}
    if queue == "bills":
        return {**base, "judgement": "bill_line", "raw_head_text": "FIXTURE-ONLY",
                "canonical_head_class": "misc", "row_type": "CHARGE", "rate_paise": 100,
                "quantity_raw": "1", "amount_paise": 100, "amount_as_printed": "1.00",
                "amount_clarity": "clear", "line_extent": "ok", "source_verified": True}
    if queue == "ocr":
        return {**base, "judgement": "ocr_transcription", "transcription": "FIXTURE ONLY",
                "transcription_partial": False, "transcription_method": "typed_from_scratch",
                "compared_to_image": True, "source_verified": True}
    return {**base, "judgement": "relevance", "relevance_label": "NOT_RELEVANT",
            "source_verified": True, "annotation_notes": "fixture-only reason"}


def _write_fixture_labels(log_dir: Path, *, skip_ocr: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with return_package._patched_logs(log_dir):
        for queue in ("bills", "ocr", "retrieval"):
            item_id = _eligible_item(queue)
            if queue == "ocr" and skip_ocr:
                viewer.append_label(queue, {"item_id": item_id, "reviewer_id": "human-soham",
                                            "review_pass": "1", "judgement": "skip",
                                            "annotation_notes": "fixture-only skip reason"},
                                   channel="cli")
            else:
                viewer.append_label(queue, _fixture_answer(queue, item_id), channel="cli")


def test_empty_export_is_portable_valid_and_checksummed(tmp_path: Path) -> None:
    logs = tmp_path / "empty-labels"
    logs.mkdir()
    package = tmp_path / "claimcheck-human-gold-return"

    return_package.export(package, log_dir=logs)

    expected = {"manifest.json", "README.md", "progress.json", "validation_report.json",
                "checksums.sha256", "labels/bill_lines.jsonl", "labels/ocr.jsonl",
                "labels/retrieval.jsonl", "labels/skipped.jsonl",
                "evidence/optional/README.md"}
    actual = {str(path.relative_to(package)) for path in package.rglob("*") if path.is_file()}
    assert expected == actual
    report = json.loads((package / "validation_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "VALID"
    assert report["total_records"] == report["valid_records"] == 0
    assert return_package.verify_checksums(package) == []


def test_export_wraps_existing_human_records_and_keeps_queue_schemas(tmp_path: Path) -> None:
    logs = tmp_path / "source-labels"
    _write_fixture_labels(logs, skip_ocr=True)
    package = tmp_path / "claimcheck-human-gold-return"

    manifest = return_package.export(package, log_dir=logs)
    report = json.loads((package / "validation_report.json").read_text(encoding="utf-8"))

    assert report["status"] == "VALID"
    assert report["total_records"] == 3
    assert report["valid_records"] == 2
    assert report["skipped"] == 1
    assert manifest["counts"]["bills"]["answers"] == 1
    assert manifest["counts"]["ocr"]["skipped"] == 1
    bill = json.loads((package / "labels/bill_lines.jsonl").read_text(encoding="utf-8"))
    assert bill["reviewer_id"] == "human-soham"
    assert bill["reviewer_kind"] == "HUMAN"
    assert bill["review_pass"] == "1"
    assert bill["answer_channel"] == "cli"
    assert bill["schema_version"] == "v1.1"
    assert bill["annotation_format"] == "claimcheck-annotation-v1"
    assert bill["human_answer"]["raw_head_text"] == "FIXTURE-ONLY"
    ocr_skip = json.loads((package / "labels/skipped.jsonl").read_text(encoding="utf-8"))
    assert ocr_skip["queue"] == "ocr" and ocr_skip["human_answer"]["judgement"] == "skip"
    assert return_package.verify_checksums(package) == []


def test_import_is_dry_by_default_and_appends_only_through_canonical_writer(tmp_path: Path) -> None:
    logs = tmp_path / "source-labels"
    _write_fixture_labels(logs)
    package = tmp_path / "claimcheck-human-gold-return"
    return_package.export(package, log_dir=logs)
    destination = tmp_path / "destination-labels"

    dry = import_human_labels.import_package(package, log_dir=destination)
    assert dry["status"] == "DRY_RUN"
    assert dry["counts"]["imported"] == 3
    assert not (destination / "bills.log.jsonl").exists()

    applied = import_human_labels.import_package(package, apply=True, log_dir=destination)
    assert applied["status"] == "IMPORTED"
    assert applied["counts"]["imported"] == 3
    with return_package._patched_logs(destination):
        labels = viewer.load_labels("bills")
    assert len(labels) == 1
    assert labels[0]["answer_channel"] == "cli"
    assert labels[0]["queue"] == "bills"
    assert labels[0]["reviewer_id"] == "human-soham"
    repeat = import_human_labels.import_package(package, apply=True, log_dir=destination)
    assert repeat["status"] == "UNCHANGED"
    assert repeat["counts"]["already_imported"] == 3


def test_package_rejects_the_recorded_contaminated_item(tmp_path: Path) -> None:
    logs = tmp_path / "fixture-labels"
    logs.mkdir()
    excluded_id = "B-BL-297935975AC134BA"
    answer = _fixture_answer("bills", excluded_id)
    with return_package._patched_logs(logs):
        viewer.append_label("bills", answer, channel="cli")
    package = tmp_path / "claimcheck-human-gold-return"

    return_package.export(package, log_dir=logs)
    report = json.loads((package / "validation_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert report["contaminated_or_excluded"] == 1
    assert any("excluded_contaminated" in problem or "contamination" in problem
               for problem in report["problems"])
    destination = tmp_path / "destination-labels"
    imported = import_human_labels.import_package(package, apply=True, log_dir=destination)
    assert imported["status"] == "REFUSED"
    assert not (destination / "bills.log.jsonl").exists()


def test_bad_channel_and_conflicting_transport_alias_are_rejected(tmp_path: Path) -> None:
    logs = tmp_path / "source-labels"
    _write_fixture_labels(logs)
    package = tmp_path / "claimcheck-human-gold-return"
    return_package.export(package, log_dir=logs)
    path = package / "labels/retrieval.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["answer_channel"] = "assistant"
    record["human_answer"]["label"] = "RELEVANT"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return_package.checksums(package)

    report = return_package.validate(package, write_report=False)
    assert report["status"] == "INVALID"
    assert any("answer_channel" in problem or "conflicts with relevance_label" in problem
               for problem in report["problems"])
    destination = tmp_path / "destination-labels"
    imported = import_human_labels.import_package(package, apply=True, log_dir=destination)
    assert imported["status"] == "REFUSED"
    assert not (destination / "retrieval.log.jsonl").exists()


def test_checksum_verification_detects_tampering_and_unlisted_files(tmp_path: Path) -> None:
    logs = tmp_path / "empty-labels"
    logs.mkdir()
    package = tmp_path / "claimcheck-human-gold-return"
    return_package.export(package, log_dir=logs)

    (package / "labels/ocr.jsonl").write_text("{}\n", encoding="utf-8")
    (package / "extra.txt").write_text("not in the checksum inventory", encoding="utf-8")
    errors = return_package.verify_checksums(package)
    assert any("sha256 mismatch" in error for error in errors)
    assert any("not covered" in error for error in errors)


def test_handoff_index_ids_do_not_self_contaminate_clean_replacement() -> None:
    """The coordination index may list IDs, but it must not bless a prose-leaked answer."""
    from bench.gold import review_batch

    handoff = json.loads((goldstore.store.ROOT / "docs" /
                          "ANTIGRAVITY-ANNOTATION-MANIFEST.json").read_text(encoding="utf-8"))
    replacement = handoff["exclusions"][0]["replacement"]
    item = next(row for row in annotate_cli.queue_rows("bills")
                if row["item_id"] == replacement)
    corpus = review_batch._norm(review_batch._screen_text())
    assert review_batch.screen_item(item, corpus, normalized=True)["verdict"] == "OK"
    assert handoff["exclusions"][0]["replacement_verified"] is True
