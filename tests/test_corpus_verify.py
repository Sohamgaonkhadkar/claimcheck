"""Quote verification against stored source text.

The property that matters: a quote that is *not* in the source is reported as absent, and
a quote that is present but captured imperfectly is reported as present **with the defect
named**. Neither outcome is allowed to shade into the other.
"""

from __future__ import annotations

import os

import pathlib

import pytest

from claimcheck.corpus import (
    FilesystemCorpusRepository,
    check_quote,
    check_quote_in_clause,
    explain_differences,
)
from claimcheck.schema import VerifyMethod

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def repo():
    return FilesystemCorpusRepository(ROOT / "data" / "corpus").open()


# ---- the four matching levels ---------------------------------------------
def test_an_exact_quote_is_reported_exact():
    text = "Insurers shall not recover any expenses towards proportionate deductions."
    check = check_quote("shall not recover any expenses towards proportionate deductions", text)
    assert check.found and check.method is VerifyMethod.EXACT and check.score == 1.0
    assert text[check.char_start:check.char_end] == "shall not recover any expenses towards proportionate deductions"


def test_a_normalised_quote_is_reported_normalised_not_exact():
    text = "Insurers shall not recover any \u201cexpenses\u201d  towards deductions."
    check = check_quote('shall not recover any "expenses" towards deductions', text)
    assert check.found and check.method is VerifyMethod.NORMALISED
    assert check.is_exact is False


def test_a_quote_that_is_absent_is_not_found():
    check = check_quote("the insurer shall refund the premium in full", "Something else entirely.")
    assert check.found is False
    assert check.mismatch


def test_a_quote_that_appears_twice_is_ambiguous_not_a_coin_toss():
    text = "Coverage is not available during the grace period. Nothing else. Coverage is not available during the grace period."
    check = check_quote("Coverage is not available during the grace period.", text)
    assert check.found is False
    assert check.method is VerifyMethod.AMBIGUOUS
    assert "2 times" in (check.mismatch or "")


# ---- the corpus's own clauses ---------------------------------------------
def test_the_proportionate_deduction_quote_verifies_against_the_stored_source(repo):
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#3")
    quote = ("Where as part of product design insurers propose proportionate deduction of the "
             "'associated medical expenses' when a policyholder chooses a higher room category "
             "than the category that is eligible as per terms and conditions of the policy, "
             "insurers shall define 'associated medical expenses' in the terms and conditions "
             "of policy contract.")
    check = check_quote_in_clause(quote, clause)
    assert check.found
    assert check.method is VerifyMethod.FUZZY and check.score > 0.95
    # the capture lost a character and several spaces; that is reported, not hidden
    assert check.artefact_note and "capture" in check.artefact_note
    assert check.notes and "artifact coordinates" in " ".join(check.notes)


def test_a_quote_whose_numbers_differ_is_refused_even_when_it_looks_close(repo):
    """The numeric-signature guard: a figure is never a matter of degree."""
    clause = repo.clause("irdai-hlt-reg-cir-152-06-2020#A1.3")
    wrong = ("The Company shall settle or reject a claim, as the case may be, within 45 days "
             "from the date of receipt of last necessary document.")
    check = check_quote_in_clause(wrong, clause)
    assert check.found is False
    assert check.artefact_note is None          # a different figure is not an artefact


def test_a_paraphrase_is_not_accepted_as_a_quotation(repo):
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#5")
    paraphrase = "Insurers shall never recover any expenses towards proportionate deductions whatsoever."
    check = check_quote_in_clause(paraphrase, clause)
    assert check.found is False
    assert check.artefact_note is None


def test_a_whitespace_only_difference_is_named_as_a_capture_artefact(repo):
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#7")
    quote = ("Insurers are not permitted to apply proportionate deduction for 'ICU charges' "
             "as different categories of ICU are not there.")
    check = check_quote_in_clause(quote, clause)
    assert check.found
    assert "whitespace" in (check.artefact_note or "")
    assert check.differences                      # the diff is shown, not summarised away


def test_offsets_are_reported_in_artifact_coordinates(repo):
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#4")
    check = check_quote_in_clause("Cost of diagnostics", clause)
    assert check.char_start >= clause.char_start
    assert check.char_end <= clause.char_end
    source = (ROOT / "data" / "corpus" /
              repo.by_id(clause.corpus_document_id).artifact_path).read_text(encoding="utf-8")
    assert "Cost of diagnostics" in source[check.char_start:check.char_end + 20]


def test_the_popi_sentence_verifies_exactly_against_the_popi_clause(repo):
    clause = repo.clause("irdai-pp-gr-cir-misc-117-9-2024#CL.HEALTH.TAT")
    check = check_quote_in_clause(
        "Settlement of claims (other than cashless) shall be settled within fifteen days "
        "from submission of claim.", clause)
    assert check.found and check.is_exact


def test_a_capture_that_glues_words_does_not_excuse_a_different_statement(repo):
    """The guard the corpus needed: "shall not" vs "shall", "do not" vs "do".

    Clause 6 of the 2020 instrument is stored with the PDF text layer's missing spaces
    ("which donot follow"). A quote that restores the spaces is the *same* statement and
    verifies; a quote that drops a negation, adds one, or turns "shall" into "may" is a
    different statement and is refused -- however close the edit distance.
    """
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#6")
    verbatim = ("Insurers shall ensure that proportionate deductions are not applied in "
                "respect of the hospitals which do not follow differential billing or for "
                "those expenses in respect of which differential billing is not adopted "
                "based on the room category. This shall be clearly specified in the policy "
                "terms and conditions.")
    accepted = check_quote_in_clause(verbatim, clause)
    assert accepted.found and accepted.method is VerifyMethod.FUZZY
    assert "whitespace" in (accepted.artefact_note or "")

    mutations = {
        "negation dropped": verbatim.replace("are not applied", "are applied"),
        "negation added": verbatim.replace("are not applied", "are not not applied"),
        "modal swapped": verbatim.replace("This shall be", "This may be"),
    }
    for label, mutated in mutations.items():
        check = check_quote_in_clause(mutated, clause)
        assert check.found is False, label
        assert "load-bearing" in (check.mismatch or ""), label
        assert check.artefact_note is None, label      # not excused as a capture defect


def test_a_fabricated_quote_is_refused_at_the_corpus_boundary(repo):
    """Rule 6 of the build: a quote that is not in the source never enters the corpus."""
    clause = repo.clause("irdai-hlt-cir-pro-84-5-2024#I.13")
    fabricated = ("The moratorium period shall be thirty-six months of continuous coverage, "
                  "after which the insurer may contest the policy for material "
                  "non-disclosure.")
    check = check_quote_in_clause(fabricated, clause)
    assert check.found is False and check.artefact_note is None
    assert clause.text.startswith("13) Policy")            # the real text, unmoved


# ---- diagnostics ----------------------------------------------------------
def test_explain_differences_lists_the_fragments():
    diffs = explain_differences("alpha BRAVO charlie", "alpha bravo delta charlie")
    assert diffs
    assert any("BRAVO" in d for d in diffs)


def test_a_check_with_no_source_text_is_not_a_pass():
    check = check_quote("anything at all", "")
    assert check.found is False
    assert check.mismatch


def test_verification_is_deterministic(repo):
    clause = repo.clause("irdai-hlt-cir-pro-84-5-2024#I.17")
    q = "Policyholder shall not be required to submit the documents."
    first = check_quote_in_clause(q, clause)
    second = check_quote_in_clause(q, clause)
    assert first == second


def test_the_corpus_layer_does_not_reach_for_a_model():
    """Rail: nothing in the corpus package may import the LLM or retrieval layers."""
    from test_golden_case import _imports_in_a_clean_interpreter

    forbidden = ("claimcheck.llm", "claimcheck.retrieve", "claimcheck.extract",
                 "claimcheck.ingest", "claimcheck.api")
    loaded = _imports_in_a_clean_interpreter("claimcheck.corpus", forbidden)
    assert not any(loaded.values()), loaded
