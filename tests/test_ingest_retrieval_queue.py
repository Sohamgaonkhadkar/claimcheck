"""Span addressing of the retrieval review queue (Phase 3D §15).

These tests use synthetic page texts, not the real corpus: the point is the *rule* by which a
clause gets an address, and every branch of it is exercised here so the real run's numbers can
be read against a tested rule.
"""

from __future__ import annotations

import json

import pytest

from bench.data_eda.retrieval_queue import (
    MIN_MATCH_RATIO,
    address_for,
    locate_in_pages,
    normalise,
)

CLAUSE = ("4. Waiting period. The Company shall not be liable to make any payment under "
          "this Policy in respect of any claim arising out of a Pre-existing Disease until "
          "48 months of continuous coverage have elapsed since the inception of the Policy.")
PAGES = [
    "POLICY WORDING PAGE 1 Running header\n"
    "4. Waiting period. The Company shall not be liable to make any payment under this Policy "
    "in respect of any claim arising out of a Pre-existing Disease until 48 months of "
    "continuous coverage have elapsed since the inception of the Policy.\n"
    "5. Exclusions follow.",
    "POLICY WORDING PAGE 2 Running header\nNothing relevant here.",
]


def test_an_exact_match_gives_a_verified_address_with_the_page_number():
    found = locate_in_pages(CLAUSE, PAGES)
    assert found["located"] is True
    assert found["method"] == "exact"
    assert found["page"] == 1
    assert normalise(PAGES[0])[found["char_start"]:found["char_end"]] == normalise(CLAUSE)


def test_page_furniture_around_a_clause_still_verifies_by_alignment():
    """The real case: the snapshot quote carries a header the re-read page does not."""
    quote = "Policy Wording Complete Healthcare UIN : UNIHLIP23006V032223 " + CLAUSE
    found = locate_in_pages(quote, PAGES)
    assert found["located"] is True
    assert found["method"] == "aligned"
    assert found["match_ratio"] >= MIN_MATCH_RATIO
    assert found["page"] == 1


def test_a_clause_that_cannot_be_located_reports_how_close_it_came():
    quote = ("48. A clause about something that was never printed in this wording at all, "
             "with entirely different vocabulary and no shared sequence of any length.")
    found = locate_in_pages(quote, PAGES)
    assert found["located"] is False
    assert found["closest_ratio"] < MIN_MATCH_RATIO


def test_a_clause_appearing_twice_is_ambiguous_and_gets_no_address():
    pages = [normalise(PAGES[0]) + "\n" + normalise(CLAUSE)]
    found = locate_in_pages(CLAUSE, pages)
    assert found["located"] is False
    assert found["ambiguous"] is True


def test_two_pages_aligning_almost_equally_is_ambiguous():
    pages = [normalise(PAGES[0]), "different header\n" + normalise(CLAUSE)]
    found = locate_in_pages(CLAUSE, pages)
    assert found["located"] is False
    assert "competing" in found


def test_empty_quote_is_not_locatable():
    assert locate_in_pages("", PAGES) is None


# --------------------------------------------------------------------------- address_for


def test_address_of_a_missing_source_says_so_and_asserts_no_page():
    addr = address_for("not-on-disk.pdf", CLAUSE, 100, 400, {})
    assert addr["document_id"] is None
    assert addr["page"] is None
    assert addr["address_basis"] == "snapshot_only_source_not_present"
    assert "404" in addr["address_note"]
    assert addr["char_start"] == 100 and addr["char_end"] == 400
    assert addr["span_id"].startswith("SP-SNAP-")


def test_address_of_a_present_source_is_derived_from_the_file_and_the_span():
    addr = address_for("wording.pdf", CLAUSE, 100, 400, {"wording.pdf": ("a" * 64, PAGES)})
    assert addr["document_id"] == "policy_wording-aaaaaaaaaaaa"
    assert addr["page"] == 1
    assert addr["address_basis"] == "verified_against_source"
    assert addr["clause_id"].startswith("CL-")
    assert addr["span_id"].startswith("SP-")
    assert addr["matched_text_sha16"]


def test_the_address_id_changes_when_the_document_changes():
    a = address_for("wording.pdf", CLAUSE, 0, 0, {"wording.pdf": ("a" * 64, PAGES)})
    b = address_for("wording.pdf", CLAUSE, 0, 0, {"wording.pdf": ("b" * 64, PAGES)})
    assert a["clause_id"] != b["clause_id"]
    assert a["span_id"] != b["span_id"]


def test_the_address_is_stable_for_the_same_inputs():
    a = address_for("wording.pdf", CLAUSE, 0, 0, {"wording.pdf": ("a" * 64, PAGES)})
    b = address_for("wording.pdf", CLAUSE, 0, 0, {"wording.pdf": ("a" * 64, PAGES)})
    assert a == b


def test_an_unlocated_clause_reports_the_closest_alignment_it_saw():
    addr = address_for("wording.pdf", "Z" * 200, 0, 0, {"wording.pdf": ("a" * 64, PAGES)})
    assert addr["address_basis"] == "source_present_quote_not_relocated"
    assert addr["closest_alignment"] is not None
    assert addr["page"] is None


def test_the_address_never_carries_clause_text():
    addr = address_for("wording.pdf", CLAUSE, 0, 0, {"wording.pdf": ("a" * 64, PAGES)})
    blob = json.dumps(addr)
    assert "Pre-existing Disease" not in blob
    assert "Waiting period" not in blob


# --------------------------------------------------------------------------- the artefact


def test_every_published_row_is_machine_proposed_and_span_addressed():
    """Read the real queue if it exists; the rule must hold for every row."""
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "bench" / "results" / "retrieval" / \
        "review_queue.jsonl"
    if not path.exists():
        pytest.skip("review queue not built in this checkout")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 270, "the Phase 3C pair count is preserved"
    for row in rows:
        assert row["status"] == "MACHINE_PROPOSED"
        assert row["review_status"] == "machine-proposed-unreviewed"
        span = row["positive"]["span"]
        assert span["span_id"] and span["text_sha16"]
        assert span["address_basis"] in {
            "verified_against_source", "verified_against_source_aligned",
            "snapshot_only_source_not_present", "source_present_quote_not_relocated",
            "source_present_address_ambiguous"}
        if span["address_basis"].startswith("verified"):
            assert span["page"] is not None and span["document_id"]
        else:
            assert span["page"] is None, "no page may be asserted for an unverified address"
        assert row["hard_negative_spans"], "every pair keeps its hard negatives"
    assert not any("gold" in json.dumps(r).lower().replace("gold set", "") for r in rows if
                   r["status"] != "MACHINE_PROPOSED")
