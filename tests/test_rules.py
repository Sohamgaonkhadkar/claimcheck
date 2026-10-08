"""The rule engine, including the seven tests the rule pack itself names.

    test_pd_define_absent_definition_blocks      PD.AME.DEFINE
    test_pd_exclusions_barred_heads              PD.AME.EXCLUSIONS
    test_pd_no_extra_breach                      PD.SCOPE.NO_EXTRA
    test_pd_no_differential_indeterminate        PD.NO_DIFFERENTIAL
    test_pd_icu_bar_breach                       PD.ICU.BAR
    test_partial_reasons_procedural              CL.PARTIAL.REASONS
    test_tat_evidence_gap                        CL.TAT.* / CL.INTEREST

A rule pack that names its own tests and then does not have them is worse than no
test list at all, so these names are load-bearing.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from claimcheck.cases.golden_001 import build_case
from claimcheck.logic import Tri
from claimcheck.pipeline import run_case
from claimcheck.rules.evaluator import (
    RESULT_APPLIES,
    RESULT_CONTESTED,
    RESULT_INDETERMINATE,
    RESULT_INFORM_ONLY,
    RESULT_NOT_APPLICABLE,
    RESULT_OUT_OF_FORCE,
    RESULT_UNVERIFIED,
    RuleContext,
    check_ratio_scope,
    evaluate_pack,
    evaluate_rule,
    rule_rows,
)
from claimcheck.rules.items import (
    ConditionContext,
    ItemState,
    NonPayableItem,
    evaluate_item,
)
from claimcheck.rules.model import RuleStatus, load_default_rulepack, pack_summary

PACK = load_default_rulepack()
TODAY = date(2026, 10, 3)
CLAIM = date(2026, 7, 14)


def base_ctx(**facts) -> RuleContext:
    ctx = RuleContext(claim_date=CLAIM)
    ctx.set_many({
        "policy.era_in_scope": True,
        "policy.ame_definition_present": True,
        "claim.proportionate_deduction_applied": True,
        "claim.applied_scope_heads": ["nursing", "surgeon", "anaesthesia", "ot_charges",
                                      "icu", "pharmacy", "diagnostics"],
        "claim.partially_disallowed": True,
        "settlement.cites_specific_policy_terms": False,
    })
    ctx.set_many(facts)
    return ctx


# ---- the pack itself --------------------------------------------------------
def test_the_pack_is_dated_sourced_and_versioned():
    assert PACK.version == "2026.10.1"
    assert PACK.released_on == date(2026, 10, 3)
    assert len(PACK.rules) == 19
    for r in PACK.rules:
        assert r.instrument.ref and r.instrument.title
        assert r.citation
        assert r.effective_from
        assert r.verified_on is not None
        if r.status is RuleStatus.VERIFIED and r.effect in ("PROHIBIT_APPLICATION",
                                                            "REQUIRE_SCOPE_LIMIT"):
            assert r.quote, f"{r.rule_id} is verdict-capable but has no verbatim quote"


def test_only_verified_rules_may_assert_anything():
    rows = {r["rule_id"]: r for r in rule_rows(PACK, TODAY)}
    assert rows["PD.ICU.BAR"]["assertable_today"] is True
    assert rows["NP.LISTS.STRUCTURE"]["assertable_today"] is False   # OV-5: unverified
    assert rows["PD.2024.SCOPE.CONTESTED"]["assertable_today"] is False  # OV-3: contested
    assert pack_summary(PACK) == {"verified": 16, "unverified": 1, "contested": 2,
                                  "gated": 0, "total": 19}


def test_the_freshness_gate_degrades_a_stale_pack():
    later = date(2027, 6, 1)          # beyond verified_on + 180 days
    out = evaluate_pack(PACK, base_ctx(), later, claim_date=CLAIM)
    assert out.by_id("PD.ICU.BAR").result == RESULT_UNVERIFIED
    assert any("verification lapsed" in n for n in out.notes)


def test_a_rule_is_not_applied_before_it_was_in_force():
    pd_icu = PACK.by_id("PD.ICU.BAR")
    old = evaluate_rule(pd_icu, base_ctx(), TODAY, claim_date=date(2020, 1, 1))
    assert old.result == RESULT_OUT_OF_FORCE or old.result == RESULT_NOT_APPLICABLE
    new = evaluate_rule(pd_icu, base_ctx(), TODAY, claim_date=CLAIM)
    assert new.result == RESULT_APPLIES


def test_a_contested_rule_never_asserts():
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    assert out.by_id("PD.2024.SCOPE.CONTESTED").result == RESULT_CONTESTED
    assert out.by_id("CL.TAT.POPI.15D").result == RESULT_CONTESTED


def test_an_unverified_rule_never_asserts():
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    assert out.by_id("NP.LISTS.STRUCTURE").result == RESULT_UNVERIFIED


def test_inform_only_rules_are_recorded_and_decide_nothing():
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    assert out.by_id("PD.ERA.WORDING").result == RESULT_INFORM_ONLY
    assert "PD.ERA.WORDING" not in [o.rule_id for o in out.countable()]


def test_the_era_gate_keys_on_dates_not_on_the_uin():
    """The 2020 instruments were implemented by re-issuing wordings, UIN unchanged."""
    case = build_case()
    case.policy.renewal_date = date(2020, 11, 1)
    ctx = RuleContext(claim_date=CLAIM)
    ctx.set_many({"policy.era_in_scope": False,
                  "claim.proportionate_deduction_applied": True})
    out = evaluate_pack(PACK, ctx, TODAY, claim_date=CLAIM)
    assert out.by_id("PD.AME.DEFINE").result == RESULT_NOT_APPLICABLE


# ---- the seven named tests --------------------------------------------------
def test_pd_define_absent_definition_blocks():
    """No definition in the wording means the ratio has no lawful scope anywhere."""
    ctx = base_ctx(**{"policy.ame_definition_present": False})
    rule = PACK.by_id("PD.AME.DEFINE")
    assert evaluate_rule(rule, ctx, TODAY, claim_date=CLAIM).result == RESULT_APPLIES
    scope = check_ratio_scope(
        applied_heads=["nursing", "surgeon", "icu"],
        policy_defined_heads=[],                      # nothing defined: nothing in scope
        rule_barred_heads=["icu", "pharmacy", "diagnostics", "consumables",
                           "implants", "medical_devices"],
    )
    assert scope.indeterminate is True
    assert scope.barred == ("icu",)
    assert all("PD.AME.DEFINE" in scope.rules_for(h) for h in scope.applied)


def test_pd_exclusions_barred_heads():
    scope = check_ratio_scope(
        applied_heads=["nursing", "pharmacy", "consumables", "diagnostics"],
        policy_defined_heads=["nursing", "pharmacy", "consumables", "diagnostics"],
        rule_barred_heads=["pharmacy", "consumables", "implants", "medical_devices",
                           "diagnostics", "icu"],
    )
    assert set(scope.rule_barred) == {"pharmacy", "consumables", "diagnostics"}
    assert scope.allowed == ("nursing",)
    assert "PD.AME.EXCLUSIONS" in scope.rules_for("pharmacy")


def test_pd_no_extra_breach():
    """A head the policy itself does not define cannot be inside the ratio."""
    scope = check_ratio_scope(
        applied_heads=["nursing", "surgeon", "icu"],
        policy_defined_heads=["nursing", "surgeon"],
        rule_barred_heads=["icu"],
    )
    assert scope.outside_definition == ("icu",)
    assert "PD.SCOPE.NO_EXTRA" in scope.rules_for("icu")
    assert scope.allowed == ("nursing", "surgeon")


def test_pd_no_differential_indeterminate():
    """The hospital's billing practice is a fact no document in the case supplies."""
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    o = out.by_id("PD.NO_DIFFERENTIAL")
    assert o.result == RESULT_INDETERMINATE
    assert "hospital.follows_differential_billing" in o.reasons[0]
    # ...and becomes decidable the moment the fact is supplied
    ctx = base_ctx(**{"hospital.follows_differential_billing": True})
    assert evaluate_rule(PACK.by_id("PD.NO_DIFFERENTIAL"), ctx, TODAY,
                         claim_date=CLAIM).result in (RESULT_APPLIES, RESULT_NOT_APPLICABLE)


def test_pd_icu_bar_breach():
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    o = out.by_id("PD.ICU.BAR")
    assert o.result == RESULT_APPLIES
    assert o.effect == "PROHIBIT_APPLICATION"
    run = _golden_run()
    assert "icu" in run.insurer.implied_scope
    assert run.insurer.barred_cut_paise == 1_800_000


def test_partial_reasons_procedural():
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    o = out.by_id("CL.PARTIAL.REASONS")
    assert o.result == RESULT_APPLIES
    assert o.effect == "REQUIRE_COMMUNICATION"
    run = _golden_run()
    procedural = [f for f in run.adjudication.findings if f.type == "PROCEDURAL"]
    assert procedural and procedural[0].state == "POTENTIALLY_INCONSISTENT"
    assert any(c.ref_id == "CL.PARTIAL.REASONS" for c in procedural[0].citations)


def test_tat_evidence_gap():
    """Turnaround time cannot be decided without the decision dates: a named gap, not a guess."""
    out = evaluate_pack(PACK, base_ctx(), TODAY, claim_date=CLAIM)
    for rule_id in ("CL.TAT.NO_INVESTIGATION", "CL.TAT.INVESTIGATION", "CL.INTEREST"):
        assert out.by_id(rule_id).result == RESULT_INDETERMINATE
    gaps = [f for f in _golden_run().adjudication.findings if f.type == "EVIDENCE_GAP"]
    assert any("CL.TAT" in r for f in gaps for c in f.citations for r in [c.ref_id])


# ---- items: conditional, never boolean --------------------------------------
def test_an_item_with_an_unstated_condition_is_indeterminate_not_payable():
    item = NonPayableItem(item_id="X", canonical_name="Attendant Charges",
                          aliases=("attendant",), stance="NOT_PAYABLE",
                          source="standard_list", status=RuleStatus.VERIFIED,
                          conditions=("claimed_as_separate_line",))
    v = evaluate_item(item, "Attendant Charges", "L10", ConditionContext({}))
    assert v.state is ItemState.INDETERMINATE
    assert "claimed_as_separate_line" in v.reasons[0]
    v2 = evaluate_item(item, "Attendant Charges", "L10",
                       ConditionContext({"claimed_as_separate_line": True}))
    assert v2.state is ItemState.NOT_PAYABLE


def test_an_item_from_an_unverified_list_cannot_decide_payability():
    item = NonPayableItem(item_id="Y", canonical_name="Slippers", aliases=("slippers",),
                          stance="NOT_PAYABLE", source="standard_list",
                          status=RuleStatus.UNVERIFIED)
    assert evaluate_item(item, "Slippers", "L1", ConditionContext({})).state is ItemState.INDETERMINATE


def test_an_item_source_is_recorded_with_the_verdict():
    item = NonPayableItem(item_id="Z", canonical_name="Attendant Charges", aliases=(),
                          stance="NOT_PAYABLE", source="policy_document",
                          source_version="SG-LAB-0001", status=RuleStatus.VERIFIED,
                          evidence_id="EV-1", quote="attendant charges ... not payable")
    v = evaluate_item(item, "Attendant Charges", "L10", ConditionContext({}))
    assert v.source == "policy_document" and v.evidence_id == "EV-1" and v.quote


def _golden_run():
    return run_case(build_case())
