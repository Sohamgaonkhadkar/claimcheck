"""Ingestion of the shipped corpus, and the refusals that keep it honest.

Two kinds of test here:

*   **the fixture corpus** — the ten documents in ``data/corpus`` are ingested and their
    shape asserted (statuses, clause counts, offsets that round-trip);
*   **adversarial fixtures** — a document whose anchor is missing, whose anchor is
    ambiguous, or whose artifact changed after ingestion. All three must fail loudly.
"""

from __future__ import annotations

import pathlib

import pytest

from claimcheck.corpus import (
    SourceStatus,
    build_corpus,
    ingest_document,
    load_document_spec,
)
from claimcheck.errors import CorpusAnchorNotFound, CorpusImmutable, CorpusProvenanceMissing

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "data" / "corpus"


@pytest.fixture(scope="module")
def build():
    return build_corpus(CORPUS_ROOT)


# ---- the shipped corpus ----------------------------------------------------
def test_the_shipped_corpus_ingests_with_the_expected_shape(build):
    assert len(build.documents) == 10
    assert len(build.all_clauses()) == 56
    assert build.summary()["with_text"] == 8
    assert build.summary()["synthetic"] == 4
    assert build.summary()["by_status"] == {"IN_FORCE": 1, "SYNTHETIC": 4, "UNKNOWN": 1,
                                            "VERIFY": 4}
    assert set(build.summary()["by_corpus"]) == {"policies", "regulations"}


def test_every_clause_round_trips_to_its_offsets_in_the_stored_artifact(build):
    for d in build.documents:
        if not d.clauses:
            continue
        text = (CORPUS_ROOT / d.document.artifact_path).read_text(encoding="utf-8")
        for c in d.clauses:
            assert text[c.char_start:c.char_end] == c.text, c.clause_id
            assert c.text.strip() == c.text


def test_clause_ids_are_document_scoped_and_ordered(build):
    for d in build.documents:
        for i, c in enumerate(d.clauses):
            assert c.clause_id.startswith(d.document.corpus_document_id + "#")
            assert c.position == i
            assert c.corpus_document_id == d.document.corpus_document_id


def test_the_proportionate_deduction_instrument_is_indexed_paragraph_by_paragraph(build):
    d = build.by_id("irdai-hlt-reg-cir-151-06-2020")
    assert [c.label for c in d.clauses] == [str(i) for i in range(1, 11)]
    assert d.document.artifact_sha256.startswith("c8ea20c3")
    para7 = build.clause("irdai-hlt-reg-cir-151-06-2020#7")
    assert "ICU charges" in para7.text
    assert para7.clause_type.value == "prohibition"


def test_the_excerpt_that_holds_two_ranges_bounds_its_last_clause_explicitly(build):
    """Item 20 ends where Chapter III begins, because the manifest says so."""
    item20 = build.clause("irdai-hlt-cir-pro-84-5-2024#I.20")
    assert item20.text.startswith("20) Implementation of Ombudsman Award:")
    assert "shall be pa" in item20.text          # the retrieved render is cut mid-word
    assert "Chapter III" not in item20.text      # the next excerpt is not attributed to it
    repeal = build.clause("irdai-hlt-cir-pro-84-5-2024#C3.IV")
    assert repeal.clause_type.value == "repeal"
    assert "supersedes all the Guidelines/Circulars listed in Annexure-6" in repeal.text
    assert repeal.page_number == 17


def test_page_numbers_are_recorded_only_where_the_source_gave_them(build):
    mc = build.by_id("irdai-hlt-cir-pro-84-5-2024")
    by_label = {c.label: c for c in mc.clauses}
    assert by_label["I.17"].page_number == 8
    assert by_label["I.19"].page_number == 9
    p151 = build.by_id("irdai-hlt-reg-cir-151-06-2020")
    assert all(c.page_number is None for c in p151.clauses)


def test_metadata_only_documents_have_no_clauses_and_say_so(build):
    for doc_id in ("irdai-hlt-reg-cir-193-07-2020", "irdai-hlt-reg-cir-150-07-2016"):
        d = build.by_id(doc_id)
        assert d.clauses == ()
        assert d.artifact_sha256 is None
        assert "metadata only" in d.document.coverage
        assert d.document.gaps, "an unread document must name what is missing"
    assert build.by_id("irdai-hlt-reg-cir-193-07-2020").document.source_status \
        is SourceStatus.VERIFY
    assert build.by_id("irdai-hlt-reg-cir-150-07-2016").document.source_status \
        is SourceStatus.UNKNOWN


def test_the_repeal_gap_is_recorded_where_the_repeal_clause_is_recorded(build):
    """The corpus states that a repeal list exists AND that we have not read it."""
    mc = build.by_id("irdai-hlt-cir-pro-84-5-2024").document
    assert mc.source_status is SourceStatus.IN_FORCE
    assert any("Annexure-6" in g for g in mc.gaps)
    assert any("OV-1" in g for g in mc.gaps)
    p151 = build.by_id("irdai-hlt-reg-cir-151-06-2020").document
    assert p151.source_status is SourceStatus.VERIFY
    assert any("OV-1" in g for g in p151.gaps)


def test_the_competing_timelines_are_both_in_the_corpus(build):
    """OV-2: both texts, both instruments, neither chosen."""
    slow = build.clause("irdai-hlt-reg-cir-152-06-2020#A1.3")
    fast = build.clause("irdai-pp-gr-cir-misc-117-9-2024#CL.HEALTH.TAT")
    assert "within 30 days fromthe date of receipt of last necessary document" in slow.text
    assert "within 45 days" in slow.text
    assert "fifteen days from submission of claim" in fast.text
    assert slow.text != fast.text


def test_the_moratorium_relation_is_machine_readable(build):
    clause = build.clause("irdai-hlt-reg-cir-152-06-2020#A1.12")
    relations = [dict(r) for r in clause.relations]
    assert relations and relations[0]["type"] == "restated_by"
    assert relations[0]["clause_id"] == "irdai-hlt-cir-pro-84-5-2024#I.13"
    assert "eightcontinuous years" in clause.text      # the capture's missing space, kept
    assert "60 months" in build.clause("irdai-hlt-cir-pro-84-5-2024#I.13").text


# ---- the synthetic policy fixtures ----------------------------------------
def test_the_synthetic_policy_fixtures_are_labelled_and_indexed(build):
    expected = {
        "synth-policy-01-room-rent-cap": ["S1", "1", "2.1", "2.2", "3"],
        "synth-policy-02-copay-deductible": ["S1", "1", "4", "5", "6"],
        "synth-policy-03-proportionate-deduction": ["S1", "2.1", "4", "5.1", "5.2"],
        "synth-policy-04-parameter-absent": ["S1", "5.1", "6"],
    }
    for doc_id, labels in expected.items():
        d = build.by_id(doc_id)
        assert d is not None, doc_id
        assert d.document.synthetic is True
        assert d.document.source_status is SourceStatus.SYNTHETIC
        assert "synthetic" in d.document.licence.lower()
        assert d.document.authoritative is False
        assert [c.label for c in d.clauses] == labels
        artifact = (CORPUS_ROOT / d.document.artifact_path).read_text(encoding="utf-8")
        assert "SYNTHETIC" in artifact          # a human opening the file sees the label
        assert artifact.splitlines()[0].startswith("SYNTHETIC POLICY WORDING")


def test_the_absent_parameter_fixture_really_lacks_the_definition(build):
    """The fixture exists to produce an *absence*, so the absence is asserted here."""
    d = build.by_id("synth-policy-04-parameter-absent")
    text = " ".join(c.text for c in d.clauses).lower()
    assert "proportionate adjustment" in text
    assert "does not include pharmacy" not in text
    assert "means nursing charges" not in text
    defined = build.by_id("synth-policy-03-proportionate-deduction")
    defined_text = " ".join(c.text for c in defined.clauses).lower()
    assert "means nursing charges" in defined_text


def test_the_room_rent_fixture_states_the_narrow_scope_itself(build):
    d = build.by_id("synth-policy-01-room-rent-cap")
    narrow = [c for c in d.clauses if c.label == "2.2"][0]
    assert "No category of expense other than the room rent itself is reduced" in narrow.text


# ---- adversarial fixtures --------------------------------------------------
def _manifest(tmp_path: pathlib.Path, body: str, text: str | None = "Text body here.\n"):
    d = tmp_path / "regulations" / "cd-adv"
    d.mkdir(parents=True)
    (d / "document.toml").write_text(body, encoding="utf-8")
    if text is not None:
        (d / "source.txt").write_text(text, encoding="utf-8")
    return d / "document.toml"


HEAD = '''
[document]
corpus_document_id = "cd-adv"
corpus = "regulations"
source_type = "guideline"
issuer = "Test Regulator"
title = "Adversarial fixture"
reference = "TEST/ADV/1"
issued_on = "2026-01-01"
source_url = "https://example.invalid/adv"
retrieved_at = "2026-10-03"
licence = "public instrument"
source_status = "IN_FORCE"
status_basis = "constructed for the test suite"
coverage = "whole fixture"
artifact = "source.txt"
'''


def test_a_missing_anchor_is_a_refusal_not_a_guess(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "text that is not in the file"
''')
    with pytest.raises(CorpusAnchorNotFound) as e:
        ingest_document(load_document_spec(manifest))
    assert "not found" in str(e.value)
    assert e.value.code == "EX-CORPUS-ANCHOR"


def test_an_ambiguous_anchor_is_a_refusal(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
''', text="Text body here.\nText body again.\n")
    with pytest.raises(CorpusAnchorNotFound) as e:
        ingest_document(load_document_spec(manifest))
    assert "times" in str(e.value)


def test_a_changed_artifact_cannot_be_absorbed_into_an_existing_snapshot(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
''')
    ingested = ingest_document(load_document_spec(manifest))
    recorded = ingested.artifact_sha256
    (manifest.parent / "source.txt").write_text("Text body here, edited.\n", encoding="utf-8")
    with pytest.raises(CorpusImmutable) as e:
        ingest_document(load_document_spec(manifest), expect_artifact_sha256=recorded)
    assert "changed" in str(e.value)
    # and without the expectation, the new source simply ingests as a *different* document
    again = ingest_document(load_document_spec(manifest))
    assert again.artifact_sha256 != recorded
    assert again.document_hash != ingested.document_hash


def test_declared_clauses_without_an_artifact_are_a_refusal(tmp_path):
    manifest = _manifest(tmp_path, HEAD.replace('artifact = "source.txt"', 'artifact = ""') + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
''', text=None)
    with pytest.raises(CorpusProvenanceMissing) as e:
        ingest_document(load_document_spec(manifest))
    assert "artifact" in str(e.value)


def test_a_manifest_missing_mandatory_provenance_is_a_refusal(tmp_path):
    manifest = _manifest(tmp_path, HEAD.replace('issuer = "Test Regulator"\n', "")
                         + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
''')
    with pytest.raises(CorpusProvenanceMissing) as e:
        load_document_spec(manifest)
    assert "issuer" in str(e.value)


def test_duplicate_clause_labels_are_refused(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text"

[[clause]]
label = "1"
heading_path = "para 1 again"
anchor = "body"
''')
    with pytest.raises(CorpusAnchorNotFound):
        load_document_spec(manifest)


def test_an_end_anchor_that_does_not_exist_is_a_refusal(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
end_anchor = "no such marker"
''')
    with pytest.raises(CorpusAnchorNotFound):
        ingest_document(load_document_spec(manifest))


def test_an_end_anchor_bounds_a_clause_in_a_multi_excerpt_artifact(tmp_path):
    manifest = _manifest(tmp_path, HEAD + '''
[[clause]]
label = "1"
heading_path = "para 1"
anchor = "Text body"
end_anchor = "Second excerpt"

[[clause]]
label = "2"
heading_path = "para 2"
anchor = "Second excerpt"
''', text="Text body here.\n\nSecond excerpt begins.\n")
    ingested = ingest_document(load_document_spec(manifest))
    assert [c.text for c in ingested.clauses] == ["Text body here.", "Second excerpt begins."]
