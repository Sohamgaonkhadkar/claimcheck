"""Span-based clause addressing and the collision regression (Phase 3D §14, §19 E–F).

The bug being closed: Phase 3C segmented 650 clauses and produced **267 heading-ID
collisions**, because a clause's identity was its printed heading. ``1``, ``I``, ``2`` and
``Section 6`` repeat inside a single wording. Any rule linked to "clause 4" would then have
been linked to whichever of the 80 clauses called itself 4.

These tests fix the required properties:

* same heading, same document, different span  -> different ids
* identical span                               -> same id
* modified source                              -> new document hash, and ids that move with
  it, so a verdict can never cite text that is no longer there
* existing declared rule -> clause links keep working
"""

from __future__ import annotations

import pytest

from claimcheck.ingest.addressing import (
    CollisionReport,
    address_clause,
    clause_id_for,
    collision_report,
    declared_clause_address,
    find_headings,
)


# --------------------------------------------------------------------------- identity


def test_same_heading_different_span_gives_different_ids():
    """The regression. Two clauses that both call themselves '4' are two clauses."""
    first = address_clause(document_id="doc-a", page_number=3, char_start=100, char_end=400,
                           text="4. Waiting periods ...", label="4")
    second = address_clause(document_id="doc-a", page_number=11, char_start=900, char_end=1300,
                            text="4. Exclusions ...", label="4")
    assert first.label == second.label == "4"
    assert first.clause_id != second.clause_id
    assert first.span_id != second.span_id


def test_identical_span_gives_the_same_id():
    a = clause_id_for("doc-a", 2, 10, 200, "some clause text")
    b = clause_id_for("doc-a", 2, 10, 200, "some clause text")
    assert a == b


def test_same_span_but_changed_text_gives_a_different_id():
    a = clause_id_for("doc-a", 2, 10, 200, "we shall pay")
    b = clause_id_for("doc-a", 2, 10, 200, "we shall not pay")
    assert a != b, "a one-word change to a clause must move its identity"


def test_same_heading_and_span_in_different_documents_are_distinct():
    a = clause_id_for("doc-a", 1, 0, 50, "4. text")
    b = clause_id_for("doc-b", 1, 0, 50, "4. text")
    assert a != b


def test_modified_source_moves_every_clause_id():
    """A new document hash must relocate every clause of that document."""
    before = clause_id_for("policy_wording-aaaa1111", 4, 0, 80, "4.1 Room rent ...")
    after = clause_id_for("policy_wording-bbbb2222", 4, 0, 80, "4.1 Room rent ...")
    assert before != after


def test_address_serialisation_is_text_free():
    addr = address_clause(document_id="d", page_number=1, char_start=0, char_end=12,
                          text="secret clause text", label="1", heading_path=("Definitions",))
    payload = addr.as_dict()
    assert "secret" not in str(payload)
    assert payload["located"] is True
    assert set(payload) >= {"clause_id", "document_id", "page", "char_start", "char_end",
                            "span_id", "label", "text_sha16"}


# --------------------------------------------------------------------------- collisions


def test_collision_report_counts_the_pre_fix_number():
    addresses = [
        address_clause(document_id="d", page_number=1, char_start=0, char_end=10,
                       text="1 alpha", label="1"),
        address_clause(document_id="d", page_number=2, char_start=0, char_end=10,
                       text="1 beta", label="1"),
        address_clause(document_id="d", page_number=3, char_start=0, char_end=10,
                       text="1 gamma", label="1"),
        address_clause(document_id="d", page_number=4, char_start=0, char_end=10,
                       text="2 delta", label="2"),
    ]
    rep = collision_report(addresses)
    assert rep.clauses == 4
    assert rep.label_collisions == 2, "three clauses labelled '1' collide twice"
    assert rep.id_collisions == 0, "span addressing produces no collisions"
    assert rep.distinct_ids == 4
    assert ("1", 3) in rep.worst_labels


def test_collision_report_is_zero_when_labels_are_unique():
    addresses = [address_clause(document_id="d", page_number=i, char_start=0, char_end=10,
                                text=f"{i} clause", label=str(i)) for i in range(1, 6)]
    rep = collision_report(addresses)
    assert rep.label_collisions == 0 and rep.id_collisions == 0


def test_real_shaped_wording_collisions_are_eliminated():
    """A wording that restarts numbering, as every real wording does."""
    body = ("1. Preamble text that is long enough to look like a clause body. " * 2)
    addresses = []
    for page in range(1, 6):
        for label in ("1", "2", "I", "II"):
            text = f"{label} {body}"
            addresses.append(address_clause(
                document_id="policy_wording-xyz", page_number=page, char_start=0,
                char_end=len(text), text=text, label=label))
    rep = collision_report(addresses)
    assert rep.clauses == 20
    assert rep.label_collisions == 16        # 4 labels x 5 pages, each colliding 4 times
    assert rep.id_collisions == 0
    assert rep.distinct_ids == 20


# --------------------------------------------------------------------------- declared links


def test_declared_clause_keeps_its_label_and_gains_an_address():
    addr = declared_clause_address("irdai-151-06-2020", "A1.3(i)",
                                   "The insurer shall settle within 30 days.",
                                   char_start=100, char_end=145, page_number=4)
    assert addr.label == "A1.3(i)", "the rule pack's link target must be preserved"
    assert addr.located is True
    assert addr.clause_id.startswith("CL-")
    assert addr.document_id == "irdai-151-06-2020"


def test_declared_clause_without_a_span_is_marked_unlocated():
    addr = declared_clause_address("corpus-doc", "7(b)", "Some text.", char_start=0, char_end=0)
    assert addr.located is False
    assert addr.clause_id.startswith("CL-DECL-")
    assert addr.span_id.startswith("SP-DECL-")


def test_declared_clause_addresses_are_stable_across_calls():
    a = declared_clause_address("d", "3.1", "text", 0, 0)
    b = declared_clause_address("d", "3.1", "text", 0, 0)
    assert a.clause_id == b.clause_id


def test_existing_rule_to_clause_links_still_resolve():
    """The corpus layer's declared ids are untouched by span addressing."""
    from claimcheck.corpus.model import CorpusClause

    clause = CorpusClause(clause_id="irdai-hlt-reg-cir-152-06-2020#A1.3(i)",
                          corpus_document_id="irdai-hlt-reg-cir-152-06-2020", label="A1.3(i)",
                          heading_path="A1", text="The insurer shall settle the claim within "
                                                 "thirty days from the receipt of the last "
                                                 "necessary document.", char_start=0,
                          char_end=96, position=0)
    clause.validate()                        # unchanged semantics must still hold
    addr = declared_clause_address(clause.corpus_document_id, clause.label, clause.text,
                                   clause.char_start, clause.char_end,
                                   heading_path=clause.heading_path)
    assert addr.label == "A1.3(i)"
    assert clause.clause_id.endswith("#A1.3(i)")


# --------------------------------------------------------------------------- headings


def test_find_headings_reads_labels_without_claiming_identity():
    text = ("1. PREAMBLE\nThis policy is issued.\n\n"
            "4.2 What is not covered\nCertain things are excluded.\n\n"
            "Section 6: Claim Procedure\nNotify us in writing.\n\n"
            "II. ANNEXURE\nListed items follow.\n")
    heads = find_headings(text)
    labels = [h.label for h in heads]
    assert "1" in labels and "4.2" in labels and "Section 6" in labels and "II" in labels
    starts = [h.start for h in heads]
    assert starts == sorted(starts), "headings are returned in document order"
