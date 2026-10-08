"""Rule <-> corpus linkage, provenance answers, and the era/status knobs.

This is the layer that makes rule 2 of the build ("every rule carries its source") a
checkable fact rather than a promise: for each rule the pack may carry a ``source`` block,
and *linking* re-reads the corpus snapshot to see whether the claim survives.

The two facts a reviewer needs are tested here directly:

    every rule that can decide a claim must resolve to a clause, in the snapshot, whose
    quote verifies -- and anything that cannot resolve must be visible as a gap.
"""

from __future__ import annotations

from datetime import date

import pytest

from claimcheck.corpus import (
    FilesystemCorpusRepository,
    explain_provenance,
    link_pack,
    link_rule,
    provenance_for_rule,
)
from claimcheck.rules.model import load_default_rulepack

TODAY = date(2026, 10, 3)
SNAPSHOT = "SNAP-v0.1-D685D386F98A"


@pytest.fixture(scope="module")
def repo():
    return FilesystemCorpusRepository("data/corpus").open()


@pytest.fixture(scope="module")
def pack():
    return load_default_rulepack()


@pytest.fixture(scope="module")
def report(pack, repo):
    return link_pack(pack, repo, today=TODAY)


# ---- the shape of the join -------------------------------------------------
def test_every_rule_gets_exactly_one_link_entry(report, pack):
    assert len(report.links) == len(pack.rules) == 19
    assert {link.rule_id for link in report.links} == {r.rule_id for r in pack.rules}


def test_the_linked_rules_all_resolve_with_a_verified_quote(report):
    resolved = [link for link in report.links if link.resolved]
    assert len(resolved) == 16
    assert all(link.instrument_agrees for link in resolved), \
        [link.describe() for link in report.instrument_mismatches]
    assert all(link.quote_verified for link in resolved), \
        [link.describe() for link in report.quote_failures]
    assert report.quote_failures == () and report.instrument_mismatches == ()


def test_the_rules_left_unlinked_are_the_declared_ones(report):
    """Unlinked is a decision with a name, not an omission."""
    unlinked = {link.rule_id: link for link in report.unlinked}
    assert set(unlinked) == {"PD.2024.SCOPE.CONTESTED",
                             "NP.LISTS.STRUCTURE", "SCOPE.POLICY_VS_STATE"}
    for link in unlinked.values():
        assert link.problems or link.notes     # each states why it is where it is


def test_the_risky_set_is_empty_in_the_shipped_pack(report):
    """No verdict-capable rule may be assertable while its quotation is unproducible."""
    assert report.risky == ()
    summary = report.summary()
    assert summary["assertable_but_unlinked"] == 0
    assert summary["resolved"] == summary["linked"] == 16
    assert summary["quote_failed"] == 0
    assert summary["rulepack_version"] == "2026.10.1"


def test_the_risky_detector_still_fires_on_a_constructed_gap(report, pack, repo):
    """The empty risky set above must be evidence, not an unplugged alarm.

    Take a rule that is assertable today, take away its corpus source, and the report
    must name it as the dangerous case it has become.
    """
    import dataclasses
    from claimcheck.corpus import link_pack

    target = pack.by_id("PD.ICU.BAR")
    assert target.assertable(TODAY) is True
    stripped = dataclasses.replace(target, source=None)
    wounded = dataclasses.replace(pack, rules=tuple(
        stripped if r.rule_id == target.rule_id else r for r in pack.rules))
    report_ = link_pack(wounded, repo, today=TODAY)
    assert {link.rule_id for link in report_.risky} == {"PD.ICU.BAR"}
    assert report_.summary()["assertable_but_unlinked"] == 1


def test_cl_interest_is_linked_to_a_clause_that_states_the_entitlement(report, repo):
    """CL.INTEREST was the one assertable-but-unlinked rule; it is now citable."""
    link = report.by_rule("CL.INTEREST")
    assert link.resolved and link.quote_verified is True
    assert link.source_ref.corpus_document_id == "irdai-hlt-reg-cir-152-06-2020"
    assert link.source_ref.clause_id == "irdai-hlt-reg-cir-152-06-2020#A1.3"
    assert link.instrument_agrees
    assert "2% above the bank rate" in link.clause.text
    assert "rate 2% above the bank rate" in link.quote


def test_a_rule_whose_quote_is_not_in_the_corpus_is_flagged_not_silently_kept(repo, pack):
    """Feed the linker a corrupt quote and it must say so."""
    import dataclasses
    from claimcheck.corpus import clause_ids_in
    from claimcheck.corpus.link import link_rule as _link

    rule = pack.by_id("PD.ICU.BAR")
    broken = dataclasses.replace(rule, quote=rule.quote.replace("not permitted", "permitted"))
    link = _link(broken, repo, today=TODAY)
    assert link.resolved and link.quote_verified is False
    assert link.ok is False
    assert "quote" in " ".join(link.problems).lower() or link.quote_check.found is False
    assert clause_ids_in([link]) == ("irdai-hlt-reg-cir-151-06-2020#7",)


def test_a_rule_pointing_at_the_wrong_clause_is_flagged(repo, pack):
    import dataclasses
    wrong = dataclasses.replace(pack.by_id("PD.ICU.BAR"),
                                source=dataclasses.replace(pack.by_id("PD.ICU.BAR").source,
                                                           clause_id="irdai-hlt-reg-cir-151-06-2020#12"))
    link = link_rule(wrong, repo, today=TODAY)
    assert link.resolved is False
    assert link.problems
    assert "12" in link.describe()


# ---- the citations rule 4 asks for ----------------------------------------
def test_every_resolved_link_cites_snapshot_document_and_clause(report):
    for link in report.links:
        if not link.resolved:
            continue
        assert link.clause.corpus_document_id == link.document.corpus_document_id
        assert link.clause.clause_id.startswith(link.document.corpus_document_id + "#")
        assert link.source_ref.corpus_document_id == link.document.corpus_document_id
        assert link.source_ref.clause_id == link.clause.clause_id
        assert link.source_ref.corpus_snapshot_id == SNAPSHOT
        assert link.document.artifact_path and link.document.artifact_sha256


def test_the_snapshot_cited_is_the_published_one(report, repo):
    assert repo.snapshot_id == SNAPSHOT
    assert all(link.source_ref.corpus_snapshot_id == repo.snapshot_id
               for link in report.links if link.source_ref is not None)


# ---- the provenance answer -------------------------------------------------
def test_provenance_for_a_rule_carries_its_letter_of_the_corpus(repo, pack):
    payload = provenance_for_rule("PD.AME.DEFINE", pack, repo, today=TODAY)
    assert payload["found"] is True and payload["rule_id"] == "PD.AME.DEFINE"
    assert payload["rule_status"] == "verified" and payload["rule_assertable_today"] is True
    assert payload["corpus_document"]["corpus_document_id"] == "irdai-hlt-reg-cir-151-06-2020"
    assert payload["corpus_document"]["source_status"] == "VERIFY"
    assert payload["clause"]["clause_id"] == "irdai-hlt-reg-cir-151-06-2020#3"
    assert payload["clause"]["char_span"] == [706, 1041]
    assert "(D V S Ramesh)" not in payload["clause"]["text"]
    assert payload["quote"]["verified"] is True and payload["quote"]["method"] == "fuzzy"
    assert payload["quote"]["text"].startswith("Where as part of product design")
    assert payload["corpus_snapshot"]["corpus_snapshot_id"] == SNAPSHOT
    assert payload["instrument"]["ref"] == "IRDAI/HLT/REG/CIR/151/06/2020"
    assert payload["corpus_document"]["gaps"]        # the reason the document is VERIFY


def test_provenance_is_honest_about_an_unlinked_rule(repo, pack):
    text = explain_provenance("SCOPE.POLICY_VS_STATE", pack, repo, today=TODAY)
    assert "no [rule.source] declared" in text
    assert "no quote to verify" in text or "carries no quote" in text


def test_provenance_text_names_the_capture_defect_it_found(repo, pack):
    text = explain_provenance("PD.AME.DEFINE", pack, repo, today=TODAY)
    assert "capture" in text and "diff" in text


# ---- era and status --------------------------------------------------------
def test_status_vocabulary_is_preserved_in_the_corpus_even_where_rules_are_unresolved(repo):
    statuses = {d.corpus_document_id: d.source_status.value for d in repo.documents()}
    assert statuses["irdai-hlt-reg-cir-151-06-2020"] == "VERIFY"     # OV-1 open
    assert statuses["irdai-hlt-reg-cir-152-06-2020"] == "VERIFY"
    assert statuses["irdai-hlt-cir-pro-84-5-2024"] == "IN_FORCE"
    assert statuses["irdai-hlt-reg-cir-150-07-2016"] == "UNKNOWN"
    assert {s.value for s in {d.source_status for d in repo.documents()}} == \
        {"IN_FORCE", "VERIFY", "UNKNOWN", "SYNTHETIC"}


def test_the_competing_instruments_stay_competing(repo, pack):
    """OV-2 and OV-3: the corpus holds both readings, the pack refuses to pick."""
    report_ = link_pack(pack, repo, today=TODAY)
    contested = [link for link in report_.links
                 if link.status.value == "contested" and link.resolved]
    assert {link.rule_id for link in contested} == {"CL.TAT.POPI.15D"}
    # the other contested rule has no corpus text behind it at all: the 2024 Master
    # Circular was read in full and contains no proportionate-deduction provision, so
    # there is nothing to cite and the system computes both readings instead
    pd = report_.by_rule("PD.2024.SCOPE.CONTESTED")
    assert pd.resolved is False and pd.quote_verified is None and pd.quote == ""
    rule = pack.by_id("PD.2024.SCOPE.CONTESTED")
    assert "OV-3" in rule.notes and "BOTH readings" in rule.notes


def test_the_era_gate_reads_dates_not_uin_alone(repo):
    """The same UIN spans two regimes; applicability keys on the policy's own dates."""
    import dataclasses

    from claimcheck.pipeline import _era_in_scope

    from claimcheck.cases.golden_001 import build_case

    case = build_case()
    assert _era_in_scope(case) is True                     # renewed 2025-06-01
    older = dataclasses.replace(case, policy=dataclasses.replace(
        case.policy, inception_date=date(2020, 1, 1), renewal_date=date(2021, 3, 31)))
    assert _era_in_scope(older) is False
    unstated = dataclasses.replace(case, policy=dataclasses.replace(
        case.policy, inception_date=None, renewal_date=None))
    assert _era_in_scope(unstated) is None                 # unknown, never "in scope"
    # the instrument that carries the dates is in the corpus, paragraph by paragraph
    clause = repo.clause("irdai-hlt-reg-cir-151-06-2020#8")
    elided = "".join(ch for ch in clause.text if not ch.isspace())
    assert "onorafter01stOctober,2020" in elided        # capture lost a space, not a date
    assert "renewalfrom01stApril,2021onwards" in elided


def test_linking_is_deterministic(repo, pack):
    first = link_pack(pack, repo, today=TODAY)
    second = link_pack(pack, repo, today=TODAY)
    assert first.summary() == second.summary()
    assert [link.as_dict() for link in first.links] == [link.as_dict() for link in second.links]


def test_linking_never_reads_anything_outside_the_snapshot(tmp_path):
    """A repository rooted at an empty tree cannot link: no fallback to the live network."""
    from claimcheck.corpus.repository import FilesystemCorpusRepository as Repo
    from claimcheck.rules.model import load_default_rulepack

    pack = load_default_rulepack()
    empty = Repo(tmp_path)
    try:
        report = link_pack(pack, empty, today=TODAY)
    except Exception as e:                                  # pragma: no cover
        pytest.fail(f"linking an empty repository should report, not raise: {e!r}")
    summary = report.summary()
    assert summary["resolved"] == 0                       # nothing could be read
    assert summary["linked"] == 16                        # ... but 16 rules still name a clause
    assert summary["unlinked"] == 3 and summary["unresolved"] == 16
    assert summary["quote_failed"] == 0                   # no quote is judged against nothing
    assert report.snapshot_id == "(no snapshot published)"
    assert all("no snapshot is published" in " ".join(link.problems)
               for link in report.unresolved)
    assert report.quote_failures == ()
