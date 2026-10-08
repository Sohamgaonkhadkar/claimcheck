"""The reviewer usability checklist, executed instead of asserted by hand.

Before the human pass resumes, each step a reviewer performs is run against the real server: open a
high-resolution page, zoom to the tiers the reviewer will use, select an answer, type a note, submit,
reload, switch queues, and confirm that the protections still hold (a bill line cannot be filed as an
OCR page, a machine answer cannot become a human one, and the original PDF is what the page claims
to be). Answers are written to a temporary log — the gold tree is never touched by a test.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
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
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.status, response.headers, response.read()


def _get_json(base: str, path: str) -> dict:
    _status, _headers, body = _get(f"{base}{path}")
    return json.loads(body)


def _post(base: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(f"{base}/api/label", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _bill_item() -> dict:
    from bench.gold import items

    return items.load_items("bill")[0]


def test_a_and_b_open_a_high_resolution_page_and_zoom_to_reading_dpi(server) -> None:
    """A: the page opens big. B: 200–400% zoom is served from a real raster, not an upscale."""
    base, _tmp = server
    item = _bill_item()
    for dpi in (200, 300, 400):
        _status, headers, body = _get(
            f"{base}/api/page.png?document_id={item['document_id']}"
            f"&page={item['page_number']}&dpi={dpi}")
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Render-DPI"] == str(dpi)
        meta = _get_json(base, f"/api/page.png?document_id={item['document_id']}"
                               f"&page={item['page_number']}&dpi={dpi}&probe=1")
        assert meta["dpi"] == dpi
        # the raster must really be that resolution: any page is at least ~8 inches wide, and at
        # the review tiers the width lands well above the 2400 px an A4 page needs at 300 DPI
        assert meta["pixel_width"] >= dpi * 8, f"{dpi} DPI gave only {meta['pixel_width']} px wide"
        if dpi >= 300:
            assert meta["pixel_width"] >= 2400, "review tiers must be full page resolution"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_c_the_page_is_bigger_than_the_viewport_so_panning_means_something(server) -> None:
    base, _tmp = server
    item = _bill_item()
    meta = _get_json(base, f"/api/page.png?document_id={item['document_id']}"
                           f"&page={item['page_number']}&dpi=300&probe=1")
    assert meta["pixel_height"] > 1200 and meta["pixel_width"] > 1200


def test_a_bill_line_crop_can_be_cut_from_the_original_page(server) -> None:
    base, _tmp = server
    item = _bill_item()
    span = item.get("line_span") or [None, None]
    needle = (item.get("parser_hint") or {}).get("raw_label") or ""
    query = (f"/api/crop.json?document_id={item['document_id']}&page={item['page_number']}"
             f"&dpi=400&char_start={span[0]}&char_end={span[1]}&needle="
             f"{urllib.parse.quote(str(needle))}")
    try:
        meta = _get_json(base, query)
    except urllib.error.HTTPError as error:
        assert error.code == 409, "a crop that cannot be located must refuse with a reason"
        assert "error" in json.loads(error.read())
        pytest.skip("no locatable line for this item; refusal is the correct behaviour")
    assert meta["geometry_source"] in ("pdf_text_layer", "ocr_line_boxes")
    assert meta["pixel_width"] > 20 and meta["sha256"]
    _status, headers, body = _get(
        f"{base}/api/crop.png?document_id={item['document_id']}&page={item['page_number']}"
        f"&dpi=400&char_start={span[0]}&char_end={span[1]}&needle="
        f"{urllib.parse.quote(str(needle))}")
    assert headers["Content-Type"] == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"


def test_d_e_f_g_h_i_answer_persists_and_survives_a_reload(server) -> None:
    """D/E: pick a label, type a note. F/G: submit and see it reach disk. H/I: reload and it is there."""
    base, tmp_path = server
    item = _bill_item()
    status, body = _post(base, {"queue": "bills", "item_id": item["item_id"],
                                "reviewer_id": "human-usability", "review_pass": "1",
                                "judgement": "bill_line",
                                "raw_head_text": "ICU ROOM RENT CHARGES 21/11/2025",
                                "canonical_head_class": "bed_charge", "row_type": "CHARGE",
                                "amount_paise": 4500000, "amount_clarity": "clear",
                                "source_verified": True,
                                "annotation_notes": "usability walkthrough"})
    assert status == 200 and body["saved"] is True
    log = [json.loads(line) for line in (tmp_path / "bills.log.jsonl").read_text().splitlines()]
    assert log and log[-1]["item_id"] == item["item_id"]
    assert log[-1]["answer_channel"] == "web"

    state = _get_json(base, "/api/state?queue=bills")
    assert any(row["item_id"] == item["item_id"] for row in state["labels"]), \
        "the saved answer must come back on reload"
    assert body["summary"]["bills"]["answered"] == 1


def test_j_switching_queues_shows_the_right_queue(server) -> None:
    base, _tmp = server
    for queue, expected in (("retrieval", 414), ("bills", 150), ("ocr", 20)):
        state = _get_json(base, f"/api/state?queue={queue}")
        assert len(state["items"]) == expected
        assert {row["_queue"] for row in state["items"]} == {queue}, \
            "every row must say which queue it came from, so a stale screen cannot misfile it"


def test_k_a_bill_line_cannot_be_filed_as_an_ocr_page(server) -> None:
    base, tmp_path = server
    status, body = _post(base, {"queue": "ocr", "item_id": _bill_item()["item_id"],
                                "reviewer_id": "human-usability", "review_pass": "1",
                                "judgement": "bill_line", "raw_head_text": "ICU ROOM RENT"})
    assert status == 400 and "error" in body
    assert not (tmp_path / "ocr.log.jsonl").exists(), "a refused answer must not reach the log"


def test_l_a_machine_answer_cannot_become_a_human_answer(server) -> None:
    base, tmp_path = server
    for reviewer in ("ai-1", "model-gpt", "system", "prelabel"):
        status, _body = _post(base, {"queue": "bills", "item_id": _bill_item()["item_id"],
                                     "reviewer_id": reviewer, "review_pass": "1",
                                     "judgement": "bill_line", "raw_head_text": "ICU ROOM RENT",
                                     "canonical_head_class": "bed_charge", "row_type": "CHARGE"})
        assert status == 400, f"reviewer id '{reviewer}' must be refused"
    assert not (tmp_path / "bills.log.jsonl").exists()


def test_m_the_original_pdf_is_the_source_of_truth(server) -> None:
    """The reviewer's fallback must be the actual file, and the render must name it."""
    import hashlib

    from bench.datasets import store as dataset_store
    from bench.gold import source

    base, _tmp = server
    item = _bill_item()
    documents = {d["document_id"]: d for d in
                 dataset_store.read_parquet(dataset_store.DATASETS / "real" / "documents")}
    path = source.source_path(documents[item["document_id"]])
    _status, headers, body = _get(f"{base}/api/source.pdf?document_id={item['document_id']}")
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert headers["X-Original-SHA256"], "the original carries the frozen hash"
    meta = _get_json(base, f"/api/page.png?document_id={item['document_id']}"
                           f"&page={item['page_number']}&dpi=300&probe=1")
    assert meta["source_pdf"] == path.name, "the render must say which file it came from"


def test_the_ui_keeps_the_controls_the_readiness_gate_checks(server) -> None:
    """The gates and the UI must not drift apart: these ids are what `make review-check` looks for."""
    from bench.gold import viewer

    for control in ('id="reviewer"', 'id="pass"', 'id="skip"', 'id="bar"'):
        assert control in viewer.PAGE
    # and the viewer must be able to reach every piece of evidence it displays. The crop URL is
    # built from a fragment ("/api/crop." + "png"|"json"), so the fragment is what must be present.
    for endpoint in ("/api/page.png", "/api/crop.", "/api/source.pdf", "/api/state", "/api/heads"):
        assert endpoint in viewer.PAGE, f"the UI cannot reach {endpoint}"


