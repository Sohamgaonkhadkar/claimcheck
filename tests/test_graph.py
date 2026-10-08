"""The calculator and the graph: fixtures with known answers, checked to the paise.

The calculator is a first-class component and is tested **independently of any AI**:
every test here constructs its own facts and asserts an exact integer paise result.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from claimcheck.cases.golden_001 import build_case
from claimcheck.calc.executor import execute_graph
from claimcheck.calc.money import format_inr
from claimcheck.calc.readings import analyse_order
from claimcheck.errors import CalcGraphInvalid
from claimcheck.graph.build import build_lawful_graph, build_restoration_graph
from claimcheck.graph.model import CalcNode, OrderProvenance, StepType
from claimcheck.pipeline import CaseContext  # re-exported for tests
from claimcheck.schema import CanonicalCategory

CASE = build_case()


def _np_evidence() -> str:
    return next(i.evidence_id for i in CASE.policy.non_payable_items if i.evidence_id)


def _copay_evidence() -> str:
    return CASE.policy.co_pay_percent.evidence_id


def _si_evidence() -> str:
    return CASE.policy.sum_insured.evidence_id


def ctx_for(case=CASE) -> CaseContext:
    return CaseContext(bill=case.bill, policy=case.policy, settlement=case.settlement,
                       facts=case.facts)


# ---- the policy's own graph -------------------------------------------------
def test_graph_is_built_from_the_policy_not_from_a_template():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000)
    assert [n.node_id for n in g.nodes] == ["NP1", "RR1", "PD1", "CP1", "SI1"]
    pd = g.node("PD1")
    assert pd.input_base.kind == "head_set"
    assert set(pd.input_base.head_set) == {"nursing", "surgeon", "anaesthesia", "ot_charges"}
    # ...and the ratio is the policy's own: eligible rate over actual rate
    assert g.node("PD1").params[0].value == Fraction(5, 8)


def test_barred_heads_are_removed_from_the_ratio_base():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           ame_heads=(CanonicalCategory.NURSING, CanonicalCategory.SURGEON,
                                      CanonicalCategory.ICU, CanonicalCategory.PHARMACY))
    assert set(g.node("PD1").input_base.head_set) == {"nursing", "surgeon"}


def test_execution_reproduces_the_hand_computation_to_the_paise():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    run = execute_graph(g, ctx_for())
    trace = {s.node_id: s.output_paise for s in run.run.trace}
    assert trace["NP1"] == 19_800_000          # 2,00,000 - 2,000
    assert trace["RR1"] == 2_500_000           # 5 days x 5,000
    assert trace["PD1"] == 5_937_500           # 95,000 x 5/8
    assert trace["CP1"] == 1_473_750           # 10% of 1,47,375
    assert run.payable_paise == 13_263_750     # Rs. 1,32,637.50
    assert format_inr(run.payable_paise) == "₹1,32,637.50"
    assert run.run.invariant_status == "ok"


def test_money_is_conserved_at_every_step():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    run = execute_graph(g, ctx_for())
    assert run.run.violations == ()
    # gross = payable + reductions + patient share
    patient = 1_500_000 + 1_473_750            # room excess + co-payment
    assert run.payable_paise + run.reductions_paise + run.patient_share_paise == CASE.bill.bill_total


def test_a_policy_that_states_nothing_gets_no_invented_ratio():
    """No room-rent sub-limit and no stated ratio: no proportionate step exists."""
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=500_000,
                           non_payable_total_paise=0)
    assert g.node("PD1") is None or g.node("RR1") is None


# ---- order ------------------------------------------------------------------
def test_stated_order_is_case_a_and_is_used_without_averaging():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    amb = analyse_order(g, ctx_for())
    assert amb.case == "A"
    assert amb.selected_reading == "R0"
    assert [r.reading_id for r in amb.readings] == ["R0", "R1"]


def test_an_unstated_order_is_unresolved_not_guessed():
    case = build_case()
    case.policy.co_pay_base_stated = False
    case.policy.stated_order = ()
    g = build_lawful_graph(case.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),),
                           order_provenance=OrderProvenance.UNRESOLVED)
    amb = analyse_order(g, ctx_for(case))
    assert amb.case == "C"
    assert amb.selected_reading is None
    assert g.order_sensitivity is True


def test_readings_are_enumerable_and_the_difference_is_visible():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    amb = analyse_order(g, ctx_for())
    payables = {r.reading_id: r.payable_paise for r in amb.readings}
    assert payables["R0"] == 13_263_750
    assert payables["R1"] == 16_470_000        # the contested narrow reading
    assert amb.spread_paise == 3_206_250
    assert amb.material is False               # the *order* is not in doubt...


def test_a_contested_scope_cannot_be_used_to_choose_the_larger_refund():
    """R1 is larger and is *not* selected; the claim uses the smallest reading."""
    from claimcheck.reconcile.model import reconcile, reconstruct_insurer_model
    insurer = reconstruct_insurer_model(
        CASE.settlement, CASE.bill,
        barred_heads=[h.value for h in
                      __import__("claimcheck.graph.build", fromlist=["x"]).DEFAULT_BARRED_FROM_RATIO],
        candidate_head_sets=CASE.candidate_head_sets,
        policy_items=tuple((i.canonical_name, i.aliases) for i in CASE.policy.non_payable_items))
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    amb = analyse_order(g, ctx_for())
    restoration = build_restoration_graph(CASE.policy, paid_paise=11_409_750,
                                          barred_cut_paise=1_800_000,
                                          copay_percent=Fraction(10),
                                          copay_evidence=(_copay_evidence(),),
                                          si_evidence=(_si_evidence(),))
    r = execute_graph(restoration, ctx_for(), initial_running_paise=11_409_750,
                      check_conservation=False)
    rec = reconcile(insurer, lawful_payable_paise=13_263_750,
                    restoration_payable_paise=r.payable_paise,
                    readings=tuple((x.reading_id, x.payable_paise) for x in amb.readings))
    assert rec.supported_paise == 1_620_000    # the minimum, never 5,060,250
    assert rec.identity_ok


# ---- the restoration route --------------------------------------------------
def test_restoration_is_paid_plus_barred_cuts_less_the_lawful_copay():
    g = build_restoration_graph(CASE.policy, paid_paise=11_409_750,
                                barred_cut_paise=1_800_000, copay_percent=Fraction(10),   # a percentage, not a fraction
                                copay_evidence=(_copay_evidence(),),
                                si_evidence=(_si_evidence(),))
    run = execute_graph(g, ctx_for(), initial_running_paise=11_409_750,
                        check_conservation=False)
    assert [s.node_id for s in run.run.trace] == ["CB1", "RC1", "SI1"]
    assert run.run.trace[2].output_paise == 13_029_750   # 1,14,097.50 + 18,000 - 1,800
    assert run.payable_paise - 11_409_750 == 1_620_000


def test_a_negative_restoration_is_refused():
    """A clawback of a negative amount would *reduce* the payable: the engine refuses."""
    from claimcheck.graph.model import BaseRef, CalcNode, Param
    g = build_restoration_graph(CASE.policy, paid_paise=11_409_750,
                                barred_cut_paise=0, copay_percent=None,
                                si_evidence=(_si_evidence(),))
    g.add_node(CalcNode(
        node_id="BAD", step_type=StepType.PROPORTIONATE_CLAWBACK, label="bad clawback",
        input_base=BaseRef(kind="running"), operation="subtract",
        params=(Param(name="amount_paise", value=1_000, unit="inr_paise", source="rule"),),
        order_provenance=OrderProvenance.STATED, position=25, produced_by="test"))
    with pytest.raises(Exception) as err:
        execute_graph(g, ctx_for(), initial_running_paise=11_409_750,
                      check_conservation=False)
    assert "clawback" in str(err.value)


# ---- structural refusals ----------------------------------------------------
def test_a_policy_parameter_without_evidence_fails_validation():
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000,
                           non_payable_total_paise=200_000,
                           non_payable_evidence=(_np_evidence(),))
    node = g.node("SI1")
    stripped = node.params[0].__class__(name=node.params[0].name, value=node.params[0].value,
                                        unit=node.params[0].unit,
                                        source=node.params[0].source, evidence_ids=())
    g.nodes = [n if n.node_id != "SI1" else CalcNode(**{**n.__dict__, "params": (stripped,)})
               for n in g.nodes]
    with pytest.raises(CalcGraphInvalid):
        execute_graph(g, ctx_for())


def test_the_settlement_letter_can_never_be_a_graph_parameter_source():
    """Anti-circularity: the letter describes a computation; it may not supply one."""
    from claimcheck.graph.model import BaseRef, CalcNode, Param
    g = build_lawful_graph(CASE.policy, room_days=5, room_rate_actual_paise=800_000)
    g.add_node(CalcNode(
        node_id="XX", step_type=StepType.ROOM_RENT_LIMIT, label="from the letter",
        input_base=BaseRef(kind="running"), operation="cap",
        params=(Param(name="rate_per_day_paise", value=500_000, unit="inr_paise",
                      source="settlement_letter"),),
        order_provenance=OrderProvenance.STATED, position=99, produced_by="test"))
    with pytest.raises(CalcGraphInvalid) as err:
        execute_graph(g, ctx_for())
    assert "settlement letter" in str(err.value)
