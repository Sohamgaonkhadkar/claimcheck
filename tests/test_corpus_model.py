"""The corpus model: what it refuses, and what it hashes.

A provenance model is only worth its fields if it *fails* when the provenance is missing.
These tests are mostly about refusal.
"""

from __future__ import annotations

from datetime import date

import pytest

from claimcheck.corpus import (
    ClauseType,
    CorpusClause,
    CorpusDocument,
    CorpusKind,
    RetrievalMethod,
    SourceStatus,
    SourceType,
    canonical_json,
    clause_set_hash,
)
from claimcheck.corpus.hashing import Unhashable, document_hash
from claimcheck.errors import CorpusProvenanceMissing


def doc(**kw) -> CorpusDocument:
    base = dict(
        corpus_document_id="cd-test-1",
        corpus=CorpusKind.REGULATIONS,
        source_type=SourceType.GUIDELINE,
        issuer="Test Regulator",
        title="A test instrument",
        reference="TEST/1/2026",
        issued_on=date(2026, 1, 1),
        effective_from=date(2026, 2, 1),
        source_url="https://example.invalid/test",
        retrieved_at=date(2026, 10, 3),
        retrieval_method=RetrievalMethod.MIRROR,
        licence="public instrument",
        source_status=SourceStatus.IN_FORCE,
        status_basis="issued by the regulator and retrieved from a copy of its text",
        coverage="whole document",
    )
    base.update(kw)
    return CorpusDocument(**base)


def clause(**kw) -> CorpusClause:
    base = dict(clause_id="cd-test-1#1", corpus_document_id="cd-test-1", label="1",
                heading_path="para 1", text="Insurers shall do the thing.", char_start=0,
                char_end=30, position=0)
    base.update(kw)
    return CorpusClause(**base)


# ---- 1. CorpusDocument validation ------------------------------------------
def test_a_document_without_provenance_is_refused():
    d = doc(source_url="")
    with pytest.raises(CorpusProvenanceMissing) as e:
        d.validate()
    assert "source_url" in str(e.value)
    assert e.value.code == "EX-CORPUS-PROVENANCE"


def test_missing_retrieval_date_is_refused_not_defaulted():
    with pytest.raises(CorpusProvenanceMissing) as e:
        doc(retrieved_at=None).validate()
    assert "retrieved_at" in str(e.value)


def test_a_status_basis_is_mandatory():
    """'IN_FORCE' without a stated basis is an assertion, not a record."""
    with pytest.raises(CorpusProvenanceMissing):
        doc(status_basis="").validate()


def test_verify_status_must_name_at_least_one_open_gap():
    with pytest.raises(CorpusProvenanceMissing) as e:
        doc(source_status=SourceStatus.VERIFY, gaps=()).validate()
    assert "gap" in str(e.value)
    doc(source_status=SourceStatus.VERIFY, gaps=("the repeal list has not been read",)).validate()


def test_superseded_must_name_the_superseding_instrument():
    with pytest.raises(CorpusProvenanceMissing):
        doc(source_status=SourceStatus.SUPERSEDED).validate()
    d = doc(source_status=SourceStatus.SUPERSEDED, superseded_by="TEST/2/2026",
            superseded_on=date(2026, 3, 1))
    d.validate()
    # ``in_force_on`` is date arithmetic over the recorded dates; it is deliberately NOT a
    # statement of standing. A superseded_on date does not silently make the document
    # vanish, because the rule engine's own force check is where that decision belongs.
    assert d.in_force_on(date(2026, 1, 31)) is False   # before effective_from
    assert d.in_force_on(date(2026, 3, 15)) is True
    assert d.in_force_on(date(2026, 4, 1)) is True     # after superseded_on
    assert d.superseded_by == "TEST/2/2026"


def test_a_synthetic_document_must_declare_itself():
    with pytest.raises(CorpusProvenanceMissing) as e:
        doc(synthetic=True, source_status=SourceStatus.SYNTHETIC,
            licence="all rights reserved").validate()
    assert "synthetic" in str(e.value).lower()
    with pytest.raises(CorpusProvenanceMissing):
        # status SYNTHETIC without synthetic = true is also refused
        doc(source_status=SourceStatus.SYNTHETIC, licence="synthetic fixture").validate()
    doc(synthetic=True, source_status=SourceStatus.SYNTHETIC,
        licence="synthetic - written for this project").validate()


def test_synthetic_documents_are_never_authoritative():
    d = doc(synthetic=True, source_status=SourceStatus.SYNTHETIC,
            licence="synthetic fixture", issued_on=None)
    d.validate()
    assert d.authoritative is False
    assert doc().authoritative is True


def test_dates_that_contradict_each_other_are_refused():
    with pytest.raises(CorpusProvenanceMissing):
        doc(issued_on=date(2026, 5, 1), effective_from=date(2026, 1, 1)).validate()
    with pytest.raises(CorpusProvenanceMissing):
        doc(effective_from=date(2026, 5, 1), effective_to=date(2026, 1, 1)).validate()


def test_effective_date_arithmetic_uses_the_recorded_dates_only():
    d = doc(effective_from=date(2026, 2, 1), effective_to=date(2026, 6, 30))
    assert d.in_force_on(date(2026, 1, 31)) is False
    assert d.in_force_on(date(2026, 2, 1)) is True
    assert d.in_force_on(date(2026, 6, 30)) is True
    assert d.in_force_on(date(2026, 7, 1)) is False
    # a document with no effective_from is not silently "in force forever"
    assert doc(effective_from=None).effective_from is None


def test_documents_round_trip_through_their_payload():
    d = doc(gaps=("one gap",), notes="n", provenance={"retrieved_by": "test"})
    assert CorpusDocument.from_payload(d.to_payload()) == d


# ---- 2. CorpusClause validation --------------------------------------------
def test_a_clause_must_belong_to_its_document():
    with pytest.raises(CorpusProvenanceMissing) as e:
        clause(clause_id="other-doc#1").validate()
    assert "belong" in str(e.value)


def test_a_clause_needs_text_and_offsets_that_agree():
    with pytest.raises(CorpusProvenanceMissing):
        clause(text="   ").validate()
    with pytest.raises(CorpusProvenanceMissing):
        clause(char_start=10, char_end=10).validate()


def test_clause_hash_is_content_addressed_not_identity_addressed():
    a = clause()
    b = clause(char_start=100, char_end=130, position=7)   # same text, different location
    assert a.text_sha256 == b.text_sha256
    assert a.payload() != b.payload()      # location matters to the document hash
    c = clause(text="Insurers shall do the other thing.")
    assert c.text_sha256 != a.text_sha256


def test_clause_set_hash_does_not_depend_on_order():
    a, b = clause(), clause(clause_id="cd-test-1#2", text="A second clause.", position=1)
    assert clause_set_hash([a.payload(), b.payload()]) == clause_set_hash([b.payload(), a.payload()])
    changed = clause(clause_id="cd-test-1#2", text="A second clause, amended.", position=1)
    assert clause_set_hash([a.payload(), changed.payload()]) != clause_set_hash(
        [a.payload(), b.payload()])


# ---- 3/4. deterministic hashing -------------------------------------------
def test_canonical_json_is_sorted_compact_and_unicode_preserving():
    payload = {"b": 1, "a": "₹5,000", "c": [1, 2]}
    assert canonical_json(payload) == '{"a":"₹5,000","b":1,"c":[1,2]}'


def test_floats_cannot_be_hashed():
    """Money never passes through a float, and neither does a snapshot id."""
    with pytest.raises(Unhashable):
        canonical_json({"amount": 1.5})
    with pytest.raises(Unhashable):
        canonical_json({"nested": {"ratio": 0.1}})
    with pytest.raises(Unhashable):
        canonical_json({"set": {1, 2}})


def test_document_hash_changes_with_metadata_clauses_or_artifact():
    d = doc()
    c = clause()
    base = document_hash(d.to_payload(), [c.payload()], {"source_text": "aa"})
    assert base == document_hash(d.to_payload(), [c.payload()], {"source_text": "aa"})
    assert base != document_hash(d.to_payload(), [c.payload()], {"source_text": "ab"})
    assert base != document_hash(doc(title="A different title").to_payload(), [c.payload()],
                                 {"source_text": "aa"})
    amended = clause(text="Insurers shall do the thing, amended.")
    assert base != document_hash(d.to_payload(), [amended.payload()], {"source_text": "aa"})


def test_clause_types_are_a_closed_set():
    for value in ("obligation", "prohibition", "definition", "procedure", "applicability",
                  "governance", "repeal", "general"):
        assert ClauseType(value)
    with pytest.raises(ValueError):
        ClauseType("rumour")
