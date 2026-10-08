"""Privacy, offsets and idempotent re-ingest (Phase 3D §2, §4, §16, §19).

The rule under test: raw bill text never appears in logs, test failures, benchmark summaries
or reports. Only a document id, a hash, a page number, span coordinates and a redacted reason
may cross that boundary.
"""

from __future__ import annotations

import json
import logging

import pytest

from claimcheck.ingest.model import (
    BBox,
    DocumentAsset,
    DocumentSource,
    DocumentType,
    EditOp,
    LicenceStatus,
    PageAsset,
    PageExtraction,
    PageQuality,
    PiiStatus,
    SourceType,
    TextLine,
)
from claimcheck.ingest.registry import DocumentRegistry
from claimcheck.ingest.sanitize import (
    MAX_LOG_CHARS,
    PiiLeak,
    SafeRecord,
    assert_safe,
    classify_pii,
    find_identifiers,
    find_markers,
    for_report,
    redact_reason,
    redact_text,
    safe_log,
)

# A real-shaped bill line: patient name, mobile, and an amount.
PII_LINE = ("Patient Name : Mr Ramesh Kumar   Age/Sex : 34/M   Mobile : 9876543210 "
            "UHID : A123456  BED CHARGE 1500.00 x 1.00 1500.00")


def _page(document_id: str = "d", page_number: int = 1) -> PageExtraction:
    """A page whose line offsets address its own text exactly, as a reader must produce."""
    pieces = PII_LINE.split("  ")
    text = "\n".join(pieces)
    lines = []
    cursor = 0
    for i, piece in enumerate(pieces, start=1):
        lines.append(TextLine(text=piece, bbox=BBox(0, 10 * i, 500, 10 * i + 9),
                              page_number=page_number, char_start=cursor,
                              char_end=cursor + len(piece), line_number=i,
                              words=tuple(piece.split())))
        cursor += len(piece) + 1
    return PageExtraction(document_id=document_id, page_number=page_number, reader="layout",
                          method=EditOp.LAYOUT, text=text, lines=tuple(lines))


# --------------------------------------------------------------------------- the boundary


def test_classify_pii_detects_a_patient_record():
    assert classify_pii(PII_LINE) is PiiStatus.PRESENT
    assert classify_pii("This policy wording describes the exclusions.") is not PiiStatus.PRESENT


def test_assert_safe_raises_on_raw_identifiers():
    with pytest.raises(PiiLeak):
        assert_safe(PII_LINE)


def test_assert_safe_passes_on_a_safe_summary():
    assert_safe('{"document_id": "hospital_bill-abc123", "sha16": "deadbeef", '
                '"page": 2, "span": "SP-9f2c", "reason": "reader_disagreement"}')


def test_find_identifiers_locates_each_kind():
    kinds = {k for k, _s, _e in find_identifiers(
        "Bill No INT2043376 mobile 9876543210 UHID A123456 email a.b@example.com")}
    assert {"DOCUMENT_NUMBER", "MOBILE", "PATIENT_NUMBER", "EMAIL"} <= kinds


def test_a_policy_heading_is_not_a_person_field_in_our_own_records():
    """The two questions are different, and conflating them makes the gate useless."""
    heading = '{"heading_path": ["PAN card Copies (Not mandatory)"]}'
    assert find_identifiers(heading) == [] or all(
        k.startswith("FIELD:") for k, _s, _e in find_identifiers(heading))
    assert find_identifiers(heading, include_field_hints=False) == []
    # in a document, the same words do introduce a person's data
    assert any(k.startswith("FIELD:") for k, _s, _e in find_identifiers("Address : 12 Main Road"))


def test_a_content_hash_is_not_mistaken_for_a_document_number():
    """The registry publishes sha256 values; a detector that fires on them is unusable."""
    assert find_identifiers("a3f9c1b2d4e5f60718293a4b5c6d7e8f") == []
    assert find_identifiers("9f2c1e0b8a74d3c5") == []


def test_run_metadata_timestamps_are_not_treated_as_pii():
    assert find_identifiers("generated_at 2026-10-04T18:22:01+0530 elapsed 541.2") == []
    assert find_markers("generated_at 2026-10-04T18:22:01") == []


def test_a_hash_of_all_digits_is_not_read_as_an_aadhaar_number():
    """A 12-char digest can be all decimal digits. That is not a person's number."""
    assert find_identifiers('"identifier_hashes": ["196573435217"]') == []
    assert find_identifiers('"sha16": "9876543210"') == []
    # but the same digits in document text are still a mobile number
    assert any(k == "MOBILE" for k, _s, _e in find_identifiers("Mobile : 9876543210"))


def test_day_first_dates_are_markers_not_identifiers():
    """A date is not an identifier, but it is document content and may not be published."""
    assert find_identifiers("18/11/2025") == []
    assert any(k.startswith("DATE") for k, _s, _e in find_markers("Bill dated 18/11/2025"))
    assert find_markers("18 Nov 2025") and find_markers("15/11/2025")


def test_redaction_is_total_deterministic_and_shape_preserving():
    """``redact_text`` destroys content, keeps length and structure. That is the contract.

    Nothing readable survives — not the name, not the phone, and not the amounts either. A
    diagnostic that needs to say *which* token was bad says it with the token class and the
    span id (:func:`redact_reason`), never by quoting the token. The arithmetic artefacts
    carry integer paise, which is a derived quantity, not the document's text.
    """
    out = redact_text(PII_LINE).text
    assert "9876543210" not in out
    assert "Ramesh" not in out
    assert "1500.00" not in out
    assert out == redact_text(PII_LINE).text, "redaction must be deterministic"
    assert "\u2022" in out
    assert redact_text(PII_LINE).source_chars == len(PII_LINE)
    assert redact_text(PII_LINE).source_sha256_16


def test_redact_reason_never_carries_free_text():
    out = redact_reason("line", span="SP-9f2c", page=2)
    assert "Ramesh" not in out and PII_LINE not in out
    assert "SP-9f2c" in out, "a span id is produced by this system and is safe to publish"
    assert "page=2" in out


def test_redact_reason_masks_a_value_under_an_untrusted_key():
    out = redact_reason("line", label="Patient Name : Mr Ramesh Kumar")
    assert "Ramesh" not in out
    assert out.startswith("line label=")


def test_report_projection_drops_anything_not_on_the_allowlist():
    fields = for_report({"document_id": "d-1", "page": 2, "text_masked": PII_LINE,
                         "reason": "reader_disagreement", "sha16": "a" * 16},
                        allow=("document_id", "page", "reason", "sha16"))
    assert "text_masked" not in fields
    assert set(fields) == {"document_id", "page", "reason", "sha16"}
    assert_safe(json.dumps(fields))


def test_safe_log_is_the_only_sanctioned_way_to_emit_a_line(caplog):
    rec = SafeRecord(event="line.quarantined", document_id="hospital_bill-ab12", page=2,
                     span=(10, 40), reason="reader_disagreement",
                     numbers={"as_read_paise": 100, "readers": 2})
    with caplog.at_level(logging.INFO):
        safe_log(logging.getLogger("claimcheck.ingest"), rec)
    assert "hospital_bill-ab12" in caplog.text
    assert "1500" not in caplog.text
    assert "Ramesh" not in caplog.text


def test_page_extraction_repr_is_safe_to_log(caplog):
    page = _page()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("claimcheck.ingest").debug("read page %r", page)
    assert "Ramesh" not in caplog.text
    assert "9876543210" not in caplog.text


def test_registry_json_is_loggable():
    reg = DocumentRegistry()
    asset = DocumentAsset.from_bytes(
        b"bytes", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "hf://x",
                       licence_status=LicenceStatus.NO_LICENCE),
        pii_status=PiiStatus.PRESENT)
    reg.add_document(asset)
    reg.add_page(PageAsset(document_id=asset.document_id, page_number=1, width=595.0,
                           height=842.0, text_layer_present=False, ocr_required=True,
                           used_ocr=True, image_count=1, text_block_count=1, char_count=250,
                           quality=PageQuality(text_density=2.0, digit_density=0.2,
                                               image_ratio=0.9, scan_likelihood=0.9,
                                               flags=("NO_TEXT_LAYER",))))
    blob = reg.to_json()
    assert not find_identifiers(blob)             # nothing identifier-shaped anywhere
    assert ": 1500" not in blob and "Ramesh" not in blob
    json.loads(blob)


def test_a_page_asset_has_no_field_for_text():
    import dataclasses

    names = {f.name for f in dataclasses.fields(PageAsset)}
    assert not (names & {"text", "raw_text", "content", "ocr_text", "lines"})


def test_a_document_asset_has_no_field_for_text():
    import dataclasses

    names = {f.name for f in dataclasses.fields(DocumentAsset)}
    assert not (names & {"text", "raw_text", "content", "first_page_text"})


def test_failing_test_output_would_not_contain_bill_text():
    """`repr` is what pytest prints when an assertion fails; it must be safe."""
    page = _page()
    line = page.lines[0]
    for obj in (page, line, line.bbox):
        blob = repr(obj)
        assert "Ramesh" not in blob and "9876543210" not in blob


# --------------------------------------------------------------------------- offsets


def test_char_offsets_are_exact_and_consistent_with_the_page_text():
    page = _page()
    for line in page.lines:
        assert page.text[line.char_start:line.char_end] == line.text


def test_line_at_finds_the_line_containing_an_offset():
    page = _page()
    target = page.lines[2]
    found = page.line_at(target.char_start + 1)
    assert found is not None and found.line_number == target.line_number
    assert page.line_at(-1) is None
    assert page.line_at(len(page.text) + 5) is None


def test_line_at_maps_a_span_across_lines_to_the_starting_line():
    page = _page()
    start = page.lines[1].char_start
    end = page.lines[3].char_end
    found = page.line_at(start)
    assert found.line_number == 2
    assert end > start


def test_numeric_tokens_of_a_page_are_listed():
    page = _page()
    assert "1500.00" in page.numeric_tokens()


def test_page_validate_accepts_a_sound_page_and_reports_a_drifted_one():
    assert _page().validate() == ()
    drifted = PageExtraction(
        document_id="d", page_number=1, reader="layout", method=EditOp.LAYOUT,
        text="the offsets no longer line up",
        lines=(TextLine(text="something else", bbox=BBox(0, 0, 1, 1), page_number=1,
                        char_start=0, char_end=14, line_number=1),))
    problems = drifted.validate()
    assert problems and "span does not reproduce" in problems[0]


def test_every_line_carries_a_bbox_with_nonzero_area():
    page = _page()
    for line in page.lines:
        assert line.bbox.width > 0 and line.bbox.height > 0
        x0, y0, x1, y1 = line.bbox.as_tuple()
        assert (x1, y1) >= (x0, y0)


# --------------------------------------------------------------------------- idempotency


def test_re_ingesting_identical_bytes_yields_the_same_document_id():
    first = DocumentAsset.from_bytes(
        b"%PDF-1.4 identical", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "hf://x",
                       licence_status=LicenceStatus.NO_LICENCE))
    second = DocumentAsset.from_bytes(
        b"%PDF-1.4 identical", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "hf://x",
                       licence_status=LicenceStatus.NO_LICENCE))
    assert first.document_id == second.document_id


def test_a_re_download_with_identical_content_is_the_same_document():
    """Re-acquisition must not fork the registry when the bytes are unchanged."""
    reg = DocumentRegistry()
    for _ in range(3):
        asset = DocumentAsset.from_bytes(
            b"%PDF-1.4 stable", DocumentType.POLICY_WORDING,
            DocumentSource(SourceType.REGULATOR, "https://irdai.gov.in/x",
                           licence_status=LicenceStatus.METADATA_ONLY))
        reg.add_document(asset)
    assert len(reg) == 1
    assert reg.totals()["documents"] == 1


def test_a_re_download_with_changed_bytes_is_a_new_document_but_links_to_the_old():
    reg = DocumentRegistry()
    old = reg.add_document(DocumentAsset.from_bytes(
        b"%PDF-1.4 v1", DocumentType.POLICY_WORDING,
        DocumentSource(SourceType.REGULATOR, "https://irdai.gov.in/x",
                       licence_status=LicenceStatus.METADATA_ONLY)))
    new = reg.add_document(DocumentAsset.from_bytes(
        b"%PDF-1.4 v2", DocumentType.POLICY_WORDING,
        DocumentSource(SourceType.REGULATOR, "https://irdai.gov.in/x",
                       licence_status=LicenceStatus.METADATA_ONLY),
        notes=f"supersedes {old.document_id}"))
    assert new.document_id != old.document_id
    assert len(reg) == 2
    assert new.notes.endswith(old.document_id), "the snapshot lineage is recorded in notes"


def test_registry_round_trips_through_json_without_text():
    reg = DocumentRegistry()
    asset = reg.add_document(DocumentAsset.from_bytes(
        b"%PDF-1.4 x", DocumentType.REGULATION,
        DocumentSource(SourceType.REGULATOR, "https://irdai.gov.in/y",
                       licence_status=LicenceStatus.METADATA_ONLY),
        pii_status=PiiStatus.NONE_IDENTIFIED))
    blob = reg.to_json()
    assert asset.document_id in blob
    assert not find_identifiers(blob)
    assert "irdai.gov.in/y" in blob, "the source reference is provenance and is kept"


def test_page_records_for_one_document_do_not_collide_across_pages():
    reg = DocumentRegistry()
    asset = reg.add_document(DocumentAsset.from_bytes(
        b"%PDF-1.4 pages", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "x",
                       licence_status=LicenceStatus.NO_LICENCE)))
    for p in (1, 2, 3):
        reg.add_page(PageAsset(document_id=asset.document_id, page_number=p, width=595.0,
                               height=842.0, text_layer_present=True, ocr_required=False,
                               image_count=0, text_block_count=1, char_count=100,
                               quality=PageQuality(text_density=1.0, digit_density=0.1,
                                                   image_ratio=0.0, scan_likelihood=0.1)))
    assert len(reg.pages(asset.document_id)) == 3


def test_adding_the_same_page_twice_does_not_duplicate_the_record():
    reg = DocumentRegistry()
    asset = reg.add_document(DocumentAsset.from_bytes(
        b"%PDF-1.4 one page", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "x",
                       licence_status=LicenceStatus.NO_LICENCE)))
    page = PageAsset(document_id=asset.document_id, page_number=1, width=595.0, height=842.0,
                     text_layer_present=True, ocr_required=False, image_count=0,
                     text_block_count=1, char_count=100,
                     quality=PageQuality(text_density=1.0, digit_density=0.1, image_ratio=0.0,
                                         scan_likelihood=0.1))
    reg.add_page(page)
    reg.add_page(page)
    assert len(reg.pages(asset.document_id)) == 1


# --------------------------------------------------------------------------- artefacts


def test_no_published_phase3d_artefact_carries_a_personal_identifier():
    """Scan everything Phase 3D publishes, not just the summaries.

    This is the boundary applied to the deliverables themselves: if a future change writes a
    bill number, a phone number or a patient identifier into a result file, this test fails
    before the file is ever committed.
    """
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    artefacts = [
        root / "bench/results/data-eda-v0.1" / name
        for name in ("ingest-summary.json", "ingest-independence.json", "ingest-registry.json",
                     "corpus-candidates.jsonl", "arithmetic-self-check.jsonl",
                     "arithmetic-self-check.md", "clause-addresses.jsonl")
    ] + [root / "bench/results/retrieval" / "review_queue.jsonl"]
    missing = [p.name for p in artefacts if not p.exists()]
    assert not missing, f"artefact not built: {missing}"

    problems: list[str] = []
    for path in artefacts:
        text = path.read_text(encoding="utf-8")
        # the value-level question, not the person-field question: these files legitimately
        # contain the system's own field names and policy headings
        for kind, start, end in find_identifiers(text, include_field_hints=False):
            problems.append(f"{path.name}:{kind} near char {start}")
        if len(path.read_text(encoding="utf-8")) > 0:
            for token in text.split():
                if len(token) > 400:
                    problems.append(f"{path.name}: overlong token")
                    break
    assert not problems, problems
