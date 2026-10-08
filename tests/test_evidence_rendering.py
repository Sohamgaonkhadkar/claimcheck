"""Is the evidence a reviewer sees actually the original page, at review quality?

A gold label is only as good as the evidence behind it. These tests pin the properties that make the
page image usable and trustworthy: it is rendered from the source PDF at 300 DPI or better, it is a
lossless PNG, it is the page it claims to be, a line crop shows the row it claims to show, and the
one thing that must never happen — a redrawn, enhanced or invented page — cannot happen quietly.

They render real pages from the rehydrated corpus, so they are slower than the unit tests and are
skipped when the sources are not present (`.cache/` is not part of the saved workspace).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest


def _sources_present() -> bool:
    try:
        from bench.datasets import store
        from bench.gold import source

        documents = {d["document_id"]: d for d in
                     store.read_parquet(store.DATASETS / "real" / "documents")}
        return any(source.source_path(d) for d in documents.values()
                   if d["document_type"] == "hospital_bill")
    except Exception:                                               # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _sources_present(),
                                reason="raw sources not rehydrated (run scripts/rehydrate_v1.py)")


def _a_bill_item():
    from bench.gold import items

    rows = items.load_items("bill")
    assert rows, "the bill queue must exist"
    return rows[0]


def test_pages_render_at_review_quality_as_lossless_png() -> None:
    """300 DPI floor, PNG, and big enough to read a digit — measured, not assumed."""
    from bench.gold import viewer

    item = _a_bill_item()
    payload = viewer.render_page(item["document_id"], item["page_number"])
    meta = viewer.page_meta(item["document_id"], item["page_number"])

    assert payload[:8] == b"\x89PNG\r\n\x1a\n", "evidence must be a PNG"
    assert meta["format"] == "PNG", "evidence must be lossless, never JPEG"
    assert meta["dpi"] >= 300, f"{meta['dpi']} DPI is below the review floor"
    assert meta["pixel_width"] >= 2000, "an A4 page at 300 DPI is ~2480 px wide"
    assert meta["bytes"] > 30_000, "a real page is not a thumbnail"
    assert len(meta["sha256"]) == 64


def test_the_low_quality_default_cannot_come_back() -> None:
    """The defect that ruined the first session: a 115 DPI raster stretched to fill the pane."""
    from bench.gold import viewer

    assert viewer.DEFAULT_DPI >= 300
    assert viewer.CROP_DPI >= 300
    assert min(viewer.DPI_TIERS) >= 110 and max(viewer.DPI_TIERS) == 600
    # a legacy `scale` of 1.6 (the old default) must snap up to a review-quality tier
    assert viewer._resolve_dpi(scale=1.6) >= 200
    # and the page is never stretched to a container in CSS any more
    assert "img{width:100%" not in viewer.PAGE.replace(" ", "")
    assert "max-width:100%" in viewer.PAGE.replace(" ", "")


def test_the_rendered_page_is_the_page_it_claims_to_be() -> None:
    """The render carries the text the ingest read from that same page — the wrong page fails.

    The comparison needs an anchor: the cached reading of the page (`.cache/gold/bill_reads/`, built
    by `make gold-build`) or, for pages with a text layer, the PDF's own text. Neither survives a
    workspace snapshot, so this skips with a reason rather than failing when the cache is cold — and
    `make verify-items` is the command that reports the cache state.
    """
    from bench.gold import items, page_quality

    if not items.load_reads():
        pytest.skip("page reading cache is cold (rebuild with: make gold-build, ~20 min)")

    payload = page_quality.run(write=False)
    checked = [page for page in payload["pages"]
               if page.get("correspondence", {}).get("checked")]
    assert checked, "at least one representative page must be identifiable"
    for page in checked:
        assert page["correspondence"]["passes"], (
            f"{page['kind']} failed its page-identity check: {page['correspondence']}")


def test_a_line_crop_shows_the_row_it_claims_to_show() -> None:
    """The crop is a window onto the original raster, and reading it back finds the amounts."""
    from bench.gold import viewer

    item = _a_bill_item()
    needle = (item.get("parser_hint") or {}).get("raw_label") or ""
    if not needle:
        pytest.skip("this bill item carries no parser reading to locate")
    try:
        payload, meta = viewer.crop_png(item["document_id"], item["page_number"], needle=needle)
    except LookupError as error:
        pytest.skip(f"page region not locatable for this item: {error}")
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["geometry_source"] in ("pdf_text_layer", "ocr_line_boxes")
    assert meta["pixel_width"] > 20 and meta["pixel_height"] > 8
    if meta["geometry_source"] == "ocr_line_boxes":
        assert meta["match_ratio"] >= viewer.CROP_MATCH_MIN, "a weak match must refuse, not guess"


def test_an_unlocatable_line_refuses_rather_than_guessing() -> None:
    """No crop at all is better than a crop of the wrong row."""
    from bench.gold import viewer

    item = _a_bill_item()
    with pytest.raises(LookupError):
        viewer.crop_png(item["document_id"], item["page_number"],
                        needle="a line that does not appear anywhere on this page 999.99 xyz")


def test_crop_geometry_refuses_ambiguous_and_weak_matches() -> None:
    """Two equally good candidates must refuse: the reviewer is shown the row, not a coin flip."""
    from bench.gold import viewer

    lines = [{"text": "TOTAL AMOUNT PAYABLE 5,000.00", "left": 0, "top": 0, "right": 10, "bottom": 4},
             {"text": "TOTAL AMOUNT PAYABLE 5,000.00", "left": 0, "top": 8, "right": 10, "bottom": 12}]
    assert viewer._match_ocr_line("TOTAL AMOUNT PAYABLE 5,000.00", lines) is None
    assert viewer._match_ocr_line("TOTAL AMOUNT PAYABLE 5,000.00", lines[:1]) is not None
    assert viewer._match_ocr_line("something else entirely here", lines) is None


def test_nothing_in_the_render_path_redraws_or_enhances_a_page() -> None:
    """The evidence path must contain no image model, no denoise, no re-encode of the content.

    Checked by walking the module's calls rather than its prose: comments are allowed to say
    "no enhancement", code is not allowed to do one.
    """
    import ast
    import inspect

    from bench.gold import viewer

    tree = ast.parse(inspect.getsource(viewer))
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name:
                called.add(name.lower())
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.name.split(".")[0].lower())
    forbidden = {"enhance", "imagenhance", "denoise", "super_resolution", "superresolution",
                 "upscale", "sharpen", "generate_image", "diffusion", "cv2", "torch", "tensorflow"}
    assert not (called & forbidden), f"the render path calls {called & forbidden}"
    assert not (imported & forbidden), f"the render path imports {imported & forbidden}"
    assert 'format="PNG"' in inspect.getsource(viewer)


def test_original_pdf_is_served_byte_identical_when_enabled() -> None:
    """The fallback: the reviewer can open the source itself, and it is the source file, not a copy."""
    import hashlib
    import threading
    from http.server import ThreadingHTTPServer

    from bench.datasets import store
    from bench.gold import source, viewer

    item = _a_bill_item()
    documents = {d["document_id"]: d for d in
                 store.read_parquet(store.DATASETS / "real" / "documents")}
    path = source.source_path(documents[item["document_id"]])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = (f"http://127.0.0.1:{httpd.server_address[1]}/api/source.pdf"
               f"?document_id={item['document_id']}")
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read()
            assert response.headers["Content-Type"] == "application/pdf"
            assert response.headers["Content-Disposition"].startswith("inline")
            assert response.headers["X-Original-SHA256"]
        assert body[:5] == b"%PDF-", "the served original must be a PDF"
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest(), (
            "the served original must be byte-identical to the source file")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_page_endpoint_reports_its_own_provenance() -> None:
    """Fidelity is auditable from the response itself, without trusting the UI."""
    import threading
    from http.server import ThreadingHTTPServer

    from bench.gold import viewer

    item = _a_bill_item()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        url = (f"{base}/api/page.png?document_id={item['document_id']}"
               f"&page={item['page_number']}&dpi=300")
        with urllib.request.urlopen(url, timeout=60) as response:
            assert response.headers["Content-Type"] == "image/png"
            assert response.headers["X-Render-DPI"] == "300"
            assert response.headers["X-Render-FORMAT"] == "PNG"
            assert response.headers["X-Source-Page"] == str(item["page_number"])
            sha = response.headers["X-Render-SHA256"]
            body = response.read()
        import hashlib

        assert hashlib.sha256(body).hexdigest() == sha, "the hash must describe what was served"
        with urllib.request.urlopen(f"{url}&probe=1", timeout=60) as response:
            meta = json.loads(response.read())
        assert meta["dpi"] == 300 and meta["pixel_width"] >= 2000
        assert meta["source_pdf"], "the render must name the file it came from"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_missing_source_is_an_error_not_a_blank_image() -> None:
    """A broken image the reviewer cannot distinguish from a hard page is how bad gold happens."""
    import threading
    from http.server import ThreadingHTTPServer

    from bench.gold import viewer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = (f"http://127.0.0.1:{httpd.server_address[1]}/api/page.png"
               "?document_id=does-not-exist&page=1&dpi=300")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url, timeout=30)
        assert error.value.code == 404
        assert "error" in json.loads(error.value.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
