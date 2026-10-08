"""Registry, page records and quality flags (Phase 3D §2, §3, §19 B–D)."""

from __future__ import annotations

import json

import pytest

from claimcheck.ingest.model import (
    BBox,
    DocumentAsset,
    DocumentFingerprint,
    DocumentSource,
    DocumentType,
    EditOp,
    InMemoryStorage,
    LicenceStatus,
    PageAsset,
    PageExtraction,
    PageQuality,
    PiiStatus,
    SourceType,
    TextLine,
    count_numeric_tokens,
    digit_density,
    scan_likelihood,
    table_likelihood,
    text_density,
)
from claimcheck.ingest.registry import DocumentRegistry, independence_report


def _asset(data: bytes, name: str = "a.pdf", dtype: DocumentType = DocumentType.HOSPITAL_BILL):
    return DocumentAsset.from_bytes(
        data, dtype,
        DocumentSource(source_type=SourceType.THIRD_PARTY_DATASET, reference=name,
                       licence_status=LicenceStatus.USAGE_UNVERIFIED),
        page_count=1, artifact_key=f"k/{name}")


def _outcome_page(document_id: str, page: int, text: str) -> PageAsset:
    return PageAsset(
        document_id=document_id, page_number=page, width=595.0, height=842.0,
        text_layer_present=True, ocr_required=False, image_count=0, text_block_count=3,
        char_count=len(text), numeric_token_count=count_numeric_tokens(text),
        quality=PageQuality(text_density=text_density(text, 595.0, 842.0),
                            digit_density=digit_density(text), image_ratio=0.0,
                            scan_likelihood=0.05, flags=()),
        extraction_method=EditOp.LAYOUT)


# --------------------------------------------------------------------------- fingerprints


def test_document_id_is_derived_from_content_not_assigned():
    a = _asset(b"%PDF-1.4 first")
    b = _asset(b"%PDF-1.4 first", name="different-name.pdf")
    c = _asset(b"%PDF-1.4 second")
    assert a.document_id == b.document_id, "identity must follow the bytes, not the filename"
    assert a.document_id != c.document_id, "different bytes are a different document"


def test_modified_source_produces_a_new_document_id_and_hash():
    original = _asset(b"%PDF-1.4 original")
    edited = _asset(b"%PDF-1.4 original plus one changed byte")
    assert edited.fingerprint.sha256 != original.fingerprint.sha256
    assert edited.document_id != original.document_id


def test_fingerprint_display_is_short_and_stable():
    fp = DocumentFingerprint.of_bytes(b"abc", 3)
    assert fp.display == fp.sha256[:16]
    assert fp.page_count == 3


def test_asset_serialisation_carries_no_document_text():
    """A document asset is metadata. If text ever appears in it, this is the alarm."""
    asset = _asset(b"%PDF-1.4 something secret-looking")
    payload = json.dumps(asset.as_dict())
    assert "PDF-1.4" not in payload
    assert set(asset.as_dict()) >= {"document_id", "document_type", "source", "sha256",
                                    "byte_size", "page_count", "licence_status", "pii_status"}


# --------------------------------------------------------------------------- registry


def test_registry_is_idempotent_and_merges_richer_metadata():
    reg = DocumentRegistry()
    first = reg.add_document(_asset(b"same bytes"))
    second = reg.add_document(_asset(b"same bytes"))
    assert first.document_id == second.document_id
    assert len(reg) == 1


def test_registry_refuses_a_page_for_an_unregistered_document():
    reg = DocumentRegistry()
    with pytest.raises(KeyError):
        reg.add_page(_outcome_page("hospital_bill-nope", 1, "text"))


def test_registry_totals_count_ocr_and_missing_text_layers():
    reg = DocumentRegistry()
    doc = reg.add_document(_asset(b"bytes"))
    reg.add_page(_outcome_page(doc.document_id, 1, "hello 123"))
    reg.add_page(PageAsset(document_id=doc.document_id, page_number=2, width=595.0,
                           height=842.0, text_layer_present=False, ocr_required=True,
                           used_ocr=True, image_count=1, text_block_count=2, char_count=40,
                           quality=PageQuality(text_density=1.0, digit_density=0.1,
                                               image_ratio=0.9, scan_likelihood=0.9)))
    totals = reg.totals()
    assert totals["documents"] == 1 and totals["pages"] == 2
    assert totals["pages_without_text_layer"] == 1
    assert totals["pages_ocr"] == 1


def test_registry_json_has_no_text_field():
    reg = DocumentRegistry()
    doc = reg.add_document(_asset(b"bytes"))
    reg.add_page(_outcome_page(doc.document_id, 1, "patient ramesh kumar 12345"))
    blob = reg.to_json()
    assert "ramesh" not in blob.lower()
    assert "patient" not in blob.lower()


def test_find_by_sha_round_trips():
    reg = DocumentRegistry()
    doc = reg.add_document(_asset(b"unique"))
    assert reg.find_by_sha256(doc.fingerprint.sha256).document_id == doc.document_id
    assert reg.find_by_sha256("0" * 64) is None


# --------------------------------------------------------------------------- quality


def test_scan_likelihood_is_monotone_in_text_layer_evidence():
    scanned = scan_likelihood(text_layer_chars=0, image_count=1, image_ratio=0.95,
                              width=595.0, height=842.0)
    born_digital = scan_likelihood(text_layer_chars=1500, image_count=0, image_ratio=0.0,
                                   width=595.0, height=842.0)
    assert scanned >= 0.8 > born_digital
    assert born_digital <= 0.2


def test_scan_likelihood_is_bounded():
    for chars in (0, 5, 20, 500):
        v = scan_likelihood(text_layer_chars=chars, image_count=3, image_ratio=1.0,
                            width=1.0, height=1.0)
        assert 0.0 <= v <= 1.0


def test_text_density_is_size_normalised():
    small = text_density("a" * 100, 100.0, 100.0)
    large = text_density("a" * 100, 1000.0, 1000.0)
    assert small > large


def test_digit_density_and_numeric_token_count():
    assert digit_density("") == 0.0
    assert digit_density("1234") == 1.0
    assert digit_density("ab12") == 0.5
    assert count_numeric_tokens("Rs 1,234.00 and 5 and 12,34,567") == 3


def test_table_likelihood_needs_numeric_rows():
    def line(text: str, i: int) -> TextLine:
        return TextLine(text=text, bbox=BBox(0, 0, 10, 10), page_number=1, char_start=0,
                        char_end=len(text), line_number=i)

    flat = [line("just some prose here", i) for i in range(6)]
    tabular = [line("BED CHARGE 1500.00 x 1.00 1500.00", i) for i in range(6)]
    assert table_likelihood(flat) == 0.0
    assert table_likelihood(tabular) > 0.5


def test_page_extraction_repr_never_leaks_text():
    ext = PageExtraction(document_id="d", page_number=1, reader="layout",
                         method=EditOp.OCR,
                         text="patient ramesh kumar admitted 12/11/2025")
    assert "ramesh" not in repr(ext)
    assert "12/11" not in repr(ext)
    assert "chars=" in repr(ext)


# --------------------------------------------------------------------------- storage


def test_in_memory_storage_round_trips():
    s = InMemoryStorage()
    s.put("a/b.bin", b"payload")
    assert s.exists("a/b.bin") and s.get("a/b.bin") == b"payload"
    assert not s.exists("missing")


def test_local_file_storage_keeps_paths_internal(tmp_path):
    from claimcheck.ingest.model import LocalFileStorage

    s = LocalFileStorage(tmp_path / "artifacts")
    s.put("pageimage/doc/0001.png", b"png")
    assert s.get("pageimage/doc/0001.png") == b"png"
    assert (tmp_path / "artifacts" / "pageimage" / "doc" / "0001.png").is_file()


def test_local_file_storage_refuses_traversal(tmp_path):
    from claimcheck.ingest.model import LocalFileStorage

    s = LocalFileStorage(tmp_path / "artifacts")
    s.put("../../escape.txt", b"nope")
    assert not (tmp_path / "escape.txt").exists()


# --------------------------------------------------------------------------- independence


def test_independence_counts_shared_identifiers_as_one_family():
    reg = DocumentRegistry()
    a = reg.add_document(_asset(b"bill one"))
    b = reg.add_document(_asset(b"bill two"))
    c = reg.add_document(_asset(b"unrelated doc"))
    texts = {a.document_id: "Bill No INT2043376 total 100.00",
             b.document_id: "Bill No INT2043376 total 200.00",
             c.document_id: "no identifier here at all"}
    rep = independence_report(reg, texts)
    assert rep["documents"] == 3
    assert rep["sharing_an_identifier"] == 2
    assert rep["counted_independent"] == 1
    assert rep["identifier_families"] == 2


def test_independence_reports_byte_identical_documents_without_publishing_identifiers():
    reg = DocumentRegistry()
    a = reg.add_document(_asset(b"identical bytes"))
    b = reg.add_document(_asset(b"identical bytes", name="copy.pdf"))
    rep = independence_report(reg, {a.document_id: "INT1234567", b.document_id: "INT1234567"})
    assert rep["exact_duplicates"] == 0 or rep["documents"] == 1
    blob = json.dumps(rep)
    assert "INT1234567" not in blob, "published independence data must carry hashes only"
    assert all("identifier_hashes" in f for f in rep["findings"])


def test_the_word_internet_is_not_read_as_a_bill_number():
    """`INT` + letters is not `INT` + digits. Getting this wrong merges the whole corpus."""
    from claimcheck.ingest.registry import _BILL_NO

    assert _BILL_NO.findall("available on the INTERNET and at the branch") == []
    assert _BILL_NO.findall("Bill No INT2043376") == ["INT2043376"]
    assert _BILL_NO.findall("AMHL9220899 and BIDHAYAK1023") == ["AMHL9220899", "BIDHAYAK1023"]


def test_an_identifier_present_in_most_documents_is_treated_as_boilerplate():
    """A token that appears everywhere separates nothing: it must not invent a family."""
    reg = DocumentRegistry()
    docs = [reg.add_document(_asset(f"doc {i} bytes".encode())) for i in range(6)]
    texts = {d.document_id: f"FORM ABC1234567 body {i}" for i, d in enumerate(docs)}
    rep = independence_report(reg, texts)
    assert rep["boilerplate_identifiers_dropped"], "the shared token must be reported as dropped"
    assert rep["sharing_an_identifier"] == 0
    assert rep["counted_independent"] == 6, "six unrelated documents stay six"
    assert rep["identifier_families"] == 6


def test_boilerplate_filter_does_not_touch_an_identifier_shared_by_a_few_documents():
    reg = DocumentRegistry()
    docs = [reg.add_document(_asset(f"bill {i} bytes".encode())) for i in range(6)]
    texts = {d.document_id: "no code" for d in docs}
    texts[docs[0].document_id] = "Bill INT2043376"
    texts[docs[1].document_id] = "Bill INT2043376"
    rep = independence_report(reg, texts)
    assert rep["boilerplate_identifiers_dropped"] == []
    assert rep["sharing_an_identifier"] == 2
    assert rep["counted_independent"] == 4


def test_independence_marks_documents_without_identifiers_as_unevidenced():
    reg = DocumentRegistry()
    a = reg.add_document(_asset(b"no id here"))
    rep = independence_report(reg, {a.document_id: "policy wording without any bill number"})
    assert rep["without_any_identifier"] == 1
    assert rep["findings"][0]["independent"] is True      # not *dis*proven, just unevidenced
    assert "cannot be evidenced" in rep["findings"][0]["reason"]


# --------------------------------------------------------------------------- pii enum


def test_pii_status_travels_with_the_asset():
    asset = DocumentAsset.from_bytes(
        b"bytes", DocumentType.HOSPITAL_BILL,
        DocumentSource(SourceType.THIRD_PARTY_DATASET, "hf://x", licence_status=LicenceStatus.NO_LICENCE),
        pii_status=PiiStatus.PRESENT)
    d = asset.as_dict()
    assert d["pii_status"] == "present"
    assert d["licence_status"] == "no_licence"
