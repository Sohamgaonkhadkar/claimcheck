"""An answer may only count if it says which interface produced it.

This rule exists because of a real incident during the review-UI hardening: a test harness run by
the assistant wrote one well-formed answer into `bills.log.jsonl` — a shell variable failed to
expand, so a redirect meant for a temporary directory pointed at the real one. The record has a
human reviewer id, valid fields, and an item that is genuinely in the queue. Nothing about its
*content* is detectably wrong; the only thing wrong is that no human produced it.

Deleting it would destroy the audit trail, and leaving it alone would have let a fabricated label
become gold. So the log is append-only and the rule is structural: an answer must name its channel
(`web` or `cli`), records without one are counted as `provenance_unknown`, and the compilers ignore
them. The record stays in the log as evidence of the incident.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from bench.gold import goldstore, viewer

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _bill_item_id() -> str:
    from bench.gold import items

    return str(items.load_items("bill")[0]["item_id"])


def _answer(item_id: str, **overrides) -> dict:
    payload = {"item_id": item_id, "reviewer_id": "human-1", "review_pass": "1",
               "judgement": "bill_line", "raw_head_text": "ICU ROOM RENT CHARGES",
               "canonical_head_class": "bed_charge", "row_type": "CHARGE", "amount_paise": 4500000,
               "amount_clarity": "clear", "source_verified": True}
    payload.update(overrides)
    return payload


def _post(base: str, payload: dict) -> int:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"{base}/api/label", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_the_web_interface_stamps_its_channel(server) -> None:
    base, tmp_path = server
    assert _post(base, {"queue": "bills", **_answer(_bill_item_id())}) == 200
    rows = [json.loads(line) for line in (tmp_path / "bills.log.jsonl").read_text().splitlines()]
    assert rows[0]["answer_channel"] == "web"


def test_the_cli_stamps_its_channel(tmp_path, monkeypatch) -> None:
    from bench.gold import annotate_cli, goldstore

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    payload = _answer(_bill_item_id())
    record = __import__("bench.gold.viewer", fromlist=["viewer"]).append_label(
        "bills", payload, channel="cli")
    assert record["answer_channel"] == "cli"
    assert annotate_cli is not None


def test_a_record_without_a_channel_is_not_an_answer(server) -> None:
    """The incident, replayed: well-formed, human id, item in queue — and still not an answer."""
    from bench.gold import accounting, goldstore

    base, tmp_path = server
    item_id = _bill_item_id()
    poisoned = _answer(item_id)
    poisoned.update({"recorded_at": "2026-10-04T18:53:56+0000", "reviewer_kind": "HUMAN"})
    goldstore.append_log("bills", poisoned)                    # no answer_channel

    counts = accounting.queue_accounting("bills", goldstore.read_log("bills"))
    assert counts["answered"] == 0, "a record with unknown provenance must not count as an answer"
    assert counts["provenance_unknown"] == 1
    assert counts["provenance_unknown_items"] == [item_id]
    assert counts["unanswered"] == counts["items"], "the item is still owed a real answer"


def test_an_unknown_provenance_record_never_reaches_the_gold(tmp_path, monkeypatch) -> None:
    from bench.gold import goldstore, labels

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    poisoned = _answer(_bill_item_id())
    poisoned.update({"recorded_at": "2026-10-04T18:53:56+0000", "reviewer_kind": "HUMAN"})
    goldstore.append_log("bills", poisoned)
    compiled = labels.compile_bill_gold()
    assert compiled["records"] == 0, "no gold row may be produced from a record with no channel"


def test_a_real_answer_after_the_incident_still_wins(tmp_path, monkeypatch) -> None:
    """The remediation is a human answering the item, not an edit to the log."""
    from bench.gold import accounting, goldstore, labels, viewer

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    item_id = _bill_item_id()
    poisoned = _answer(item_id)
    poisoned.update({"recorded_at": "2026-10-04T18:53:56+0000", "reviewer_kind": "HUMAN"})
    goldstore.append_log("bills", poisoned)

    viewer.append_label("bills", _answer(item_id, raw_head_text="ICU ROOM RENT 45000"),
                        channel="web")
    counts = accounting.queue_accounting("bills", goldstore.read_log("bills"))
    assert counts["answered"] == 1, "the human answer counts"
    assert counts["provenance_unknown"] == 1, "and the stray record is still visible"
    compiled = labels.compile_bill_gold()
    assert compiled["records"] == 1
    from bench.gold import goldstore as gs

    gold_rows = gs.read_dataset("bill_lines_gold") if False else None
    assert compiled["records"] == 1, "exactly one gold row, from the human answer"


def test_the_real_gold_log_holds_the_incident_record_and_no_gold() -> None:
    """The live workspace: the record is visible, and the bill gold is still empty."""
    from bench.gold import accounting, goldstore, labels

    rows = goldstore.read_log("bills")
    stray = [row for row in rows if not accounting.has_known_provenance(row)]
    if not stray:
        pytest.skip("the incident record has been resolved in this workspace")
    assert len(stray) == 1
    assert stray[0]["item_id"] == "B-BL-297935975AC134BA"
    assert stray[0]["reviewer_id"] == "human-1"
    summary = accounting.summary()
    assert summary["bills"]["answered"] == 0
    assert summary["bills"]["provenance_unknown"] == 1
    assert labels.compile_bill_gold()["records"] == 0, "the live bill gold is still empty"
