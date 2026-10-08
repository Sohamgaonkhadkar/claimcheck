"""The review tool over real HTTP.

The rules that keep gold honest are enforced in three places — the browser, the server, and the
compiler — and only the server can be trusted, because the browser is not under our control. These
tests start the real handler on an ephemeral port, post answers to it, and check what reached disk.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from bench.gold import goldstore, viewer

    monkeypatch.setattr(goldstore, "ANNOTATION_LOGS", tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _post(base: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(f"{base}/api/label", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _a_real_candidate_id() -> str:
    from bench.gold import goldstore

    rows = goldstore.read_dataset("retrieval_candidates")
    assert rows, "the candidate pool must exist for the review session"
    return rows[0]["candidate_id"]


def test_a_valid_answer_is_accepted_and_written(server) -> None:
    from bench.gold import goldstore

    status, body = _post(server, {"queue": "retrieval", "item_id": _a_real_candidate_id(),
                                  "reviewer_id": "human-1", "review_pass": "1",
                                  "judgement": "skip"})
    assert status == 200 and body["saved"] is True
    rows = goldstore.read_log("retrieval")
    assert len(rows) == 1
    assert rows[0]["reviewer_kind"] == "HUMAN"
    assert rows[0]["judgement"] == "skip"


def test_a_machine_reviewer_id_is_refused_with_an_explanation(server) -> None:
    status, body = _post(server, {"queue": "retrieval", "item_id": _a_real_candidate_id(),
                                  "reviewer_id": "ai-1", "review_pass": "1",
                                  "judgement": "skip"})
    assert status == 400
    assert "human-" in body["error"]


def test_an_item_from_another_queue_is_refused(server) -> None:
    """The exact mis-filing that happened once: a bill item posted to the OCR queue."""
    from bench.gold import items

    bill_item = items.load_items("bill")[0]["item_id"]
    status, body = _post(server, {"queue": "ocr", "item_id": bill_item, "reviewer_id": "human-1",
                                  "review_pass": "1", "judgement": "ocr_transcription",
                                  "transcription": "TOTAL 250.00"})
    assert status == 400
    assert "not in the 'ocr' queue" in body["error"]


def test_the_wrong_judgement_for_a_queue_is_refused(server) -> None:
    status, body = _post(server, {"queue": "ocr", "item_id": _a_real_candidate_id(),
                                  "reviewer_id": "human-1", "review_pass": "1",
                                  "judgement": "relevance", "relevance_label": "RELEVANT"})
    assert status == 400
    assert "accepts judgements" in body["error"]


def test_an_unanswered_conditional_is_refused_over_http(server) -> None:
    status, body = _post(server, {"queue": "retrieval", "item_id": _a_real_candidate_id(),
                                  "reviewer_id": "human-1", "review_pass": "1",
                                  "judgement": "relevance", "relevance_label": "CONDITIONAL"})
    assert status == 400
    assert "note is required" in body["error"]


def test_the_summary_reports_answered_skipped_rejected_and_unanswered(server) -> None:
    _post(server, {"queue": "retrieval", "item_id": _a_real_candidate_id(),
                   "reviewer_id": "human-1", "review_pass": "1", "judgement": "skip"})
    with urllib.request.urlopen(f"{server}/api/summary", timeout=10) as response:
        summary = json.loads(response.read())
    retrieval = summary["retrieval"]
    assert retrieval["skipped"] == 1
    assert retrieval["answered"] == 0, "a skip is not progress"
    assert retrieval["items"] == retrieval["unanswered"] + retrieval["skipped"] \
        + retrieval["answered"]
