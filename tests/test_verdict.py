"""The verdict: gates change what may be claimed, not how much is claimed.

The three failure modes this file exists to catch:

  1. a missing document producing a *smaller* number instead of an evidence gap;
  2. a broken provenance identity producing any number at all;
  3. a supported outcome disappearing because a case looks better without it.
"""

from __future__ import annotations

import dataclasses as dc
from datetime import date

import pytest

from claimcheck.cases.golden_001 import build_case
from claimcheck.calc.money import format_inr
from claimcheck.pipeline import run_case
from claimcheck.verdict.findings import Citation, Finding

CASE = build_case()


def test_a_complete_case_passes_every_gate_and_produces_the_milestone_numbers():
    run = run_case(CASE)
    adj = run.adjudication
    assert [g.gate for g in adj.gates] == ["G1_extraction", "G2_document_set",
                                           "G3_policy_support", "G5_graph_order",
                                           "G6_calculation", "G8_provenance"]
    assert all(g.passed for g in adj.gates), [ (g.gate, g.detail) for g in adj.gates if not g.passed ]
    assert adj.worst_state() == "POTENTIALLY_INCONSISTENT"
    assert adj.supported_total_paise() == 1_620_000
    assert format_inr(adj.supported_total_paise()) == "₹16,200.00"


def test_a_missing_document_produces_a_gap_not_a_smaller_number():
    case = build_case()
    without_bill = dict(case.documents)
    del without_bill["bill"]
    case.documents = without_bill
    run = run_case(case)
    gates = {g.gate: g for g in run.adjudication.gates}
    assert gates["G2_document_set"].passed is False
    assert "bill" in gates["G2_document_set"].detail
    # the recalculation is unchanged (it never depended on the letter), and the report says
    # what is missing rather than quietly becoming more conservative
    assert run.lawful_payable == 13_263_750
    assert any("bill" in f.why or "bill" in " ".join(f.missing) for f in
               run.adjudication.findings + run.adjudication.withheld)


def test_a_broken_provenance_identity_withholds_the_amount():
    """G8 fails -> the money finding is not emitted at all; a gap is."""
    run = run_case(CASE)
    assert run.reconciliation.identity_ok is True
    from claimcheck.reconcile.model import reconcile
    rec = reconcile(run.insurer, lawful_payable_paise=run.lawful_payable,
                    restoration_payable_paise=run.restoration.payable_paise + 1,
                    readings=tuple(sorted(run.readings.items())))
    assert rec.identity_ok is False
    # the same reconciliation is what the verdict engine consults
    from claimcheck.verdict.engine import VerdictInputs, run_gates
    v = VerdictInputs(
        policy=CASE.policy, bill=CASE.bill, settlement=CASE.settlement, graph=run.graph,
        execution=run.execution, readings=run.readings, order_case=run.ambiguity.case,
        order_material=run.ambiguity.material, order_reason=run.ambiguity.reason,
        documents_present=frozenset(CASE.documents), documents_required=("bill",),
        evidence_document_ids={}, rule_evaluation=run.rules,
        insurer=run.insurer, reconciliation=rec)
    gates = {g.gate: g for g in run_gates(v)}
    assert gates["G8_provenance"].passed is False


def test_every_money_finding_has_a_complete_six_slot_bundle():
    run = run_case(CASE)
    assert run.adjudication.withheld == []
    for f in run.adjudication.findings:
        assert f.bundle_complete, (f.finding_id, f.missing_slots())
        if f.amount_paise:
            assert f.calculation_steps, f.finding_id
            assert f.citations, f.finding_id
            assert f.limitations, f.finding_id


def test_a_finding_missing_a_slot_is_withheld_and_never_shown_with_a_caveat():
    thin = Finding(finding_id="F999", type="FINANCIAL", state="POTENTIALLY_INCONSISTENT",
                   head="proportionate_deduction", why="an amount with no arithmetic",
                   amount_paise=1_000)
    assert thin.bundle_complete is False
    assert thin.displayable is False
    assert set(thin.missing_slots()) >= {"policy", "arithmetic", "limitations"}


def test_a_finding_with_a_quote_but_no_rule_still_cannot_claim_a_regulation():
    f = Finding(
        finding_id="F998", type="FINANCIAL", state="POTENTIALLY_INCONSISTENT",
        head="proportionate_deduction", why="x", amount_paise=100,
        citations=(Citation(ref_type="policy_clause", ref_id="c5.1", quote="...",
                            verified=True),),
        evidence_ids=("EV-1",), calculation_steps=("PD1",),
        limitations=("it does not show intent",),
    )
    # a policy clause is not a regulation: the slot stays empty and the finding is withheld
    assert "regulation" in f.missing_slots()
    assert f.displayable is False


def test_head_state_is_the_worst_of_its_findings():
    from claimcheck.verdict.findings import Adjudication, GateResult
    adj = Adjudication(policy_id="p", gates=(GateResult("G1_extraction", True, "ok"),))
    def mk(fid: str, state: str):
        return Finding(finding_id=fid, type="INFO", state=state, head="h", why="w",
                       citations=(Citation("policy_clause", "c", quote="q", verified=True),),
                       calculation_steps=("N",), limitations=("l",))
    adj.findings = [mk("a", "CONSISTENT"), mk("b", "UNDETERMINED")]
    adj.head_states = adj.recompute_head_states()
    assert adj.worst_state() == "UNDETERMINED"
    adj.findings = [mk("a", "CONSISTENT"), mk("b", "POTENTIALLY_INCONSISTENT")]
    adj.head_states = adj.recompute_head_states()
    assert adj.worst_state() == "POTENTIALLY_INCONSISTENT"
    # a head that is only represented by a withheld finding is not thereby settled
    adj.findings = []
    adj.withheld = [mk("c", "POTENTIALLY_INCONSISTENT")]
    adj.head_states = adj.recompute_head_states()
    assert adj.worst_state() == "POTENTIALLY_INCONSISTENT"


def test_a_wholly_lawful_settlement_produces_no_money_finding():
    """Constraint 10: removing the supported outcome is not allowed, so a lawful
    settlement must come out lawful — with the procedural gaps still reported."""
    case = build_case()
    lawful_cut = 3_562_500                     # 95,000 x 3/8, the ratio on the lawful heads
    case.settlement = dc.replace(
        case.settlement,
        deductions=(
            case.settlement.deductions[0],                                 # attendant
            case.settlement.deductions[1],                                 # room restriction
            dc.replace(case.settlement.deductions[2], amount=lawful_cut),
            dc.replace(case.settlement.deductions[3], amount=1_473_750),   # co-pay on the lawful base
        ),
        final_payable=13_263_750,
    )
    run = run_case(case)
    assert run.lawful_payable == 13_263_750
    assert run.insurer.barred_cut_paise == 0
    assert run.insurer.unexplained_paise == 0
    assert [f for f in run.adjudication.findings if f.type == "FINANCIAL"] == []
    assert run.adjudication.supported_total_paise() == 0
    # the procedural defect in how it was communicated is still reported
    assert any(f.type == "PROCEDURAL" for f in run.adjudication.findings)


def test_an_unverified_rule_leaves_its_head_undetermined():
    run = run_case(CASE)
    assert "standardised_item_lists" in run.adjudication.head_states
    assert run.adjudication.head_states["standardised_item_lists"] == "UNDETERMINED"


def test_the_ratio_head_is_flagged_not_verdict_ed():
    run = run_case(CASE)
    state = run.adjudication.head_states["proportionate_deduction"]
    assert state == "POTENTIALLY_INCONSISTENT"
    assert state not in ("BREACH", "UNLAWFUL", "GUILTY")     # the vocabulary is deliberate


def test_every_finding_that_carries_a_number_names_its_evidence():
    run = run_case(CASE)
    for f in run.adjudication.findings:
        if f.amount_paise:
            assert f.evidence_ids, f.finding_id
            assert f.amount_basis, f.finding_id
