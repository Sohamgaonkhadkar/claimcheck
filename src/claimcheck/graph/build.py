"""Graph assembly: turning a policy's words into that policy's arithmetic.

The assembler is **deterministic code** that reads structured policy facts (each of
which carries evidence) and emits nodes. It never invents a step the policy does not
support, and it never imports a model. Where the policy is silent on a base or an
order, the assembler emits an ``UNRESOLVED`` provenance or a variant — it does not
fall back to a market convention.

There is deliberately **no universal cascade** in this module. If you are looking for
"the" order of insurance deductions, it does not exist here: there is only
``build_lawful_graph`` for one policy at a time.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Mapping, Sequence

from ..logic import FactProvider
from ..schema import (
    CanonicalCategory,
    Money,
    OrderProvenance,
    PolicyFacts,
    PolicyClause,
    ReadingId,
    SettlementFacts,
)
from .model import (
    BaseRef,
    CalcGraph,
    CalcNode,
    Edge,
    GraphVariant,
    Param,
    StepType,
)

# Categories that rules bar from the proportionate-deduction scope. Sourced from
# the rule pack (PD.AME.EXCLUSIONS, PD.ICU.BAR) — kept here as the *default* scope
# the assembler applies, always recorded on the node as a rule-sourced parameter.
DEFAULT_BARRED_FROM_RATIO: tuple[CanonicalCategory, ...] = (
    CanonicalCategory.PHARMACY,
    CanonicalCategory.CONSUMABLES,
    CanonicalCategory.IMPLANTS,
    CanonicalCategory.MEDICAL_DEVICES,
    CanonicalCategory.DIAGNOSTICS,
    CanonicalCategory.ICU,
)


def _money_param(name: str, value: Money, *, evidence: Sequence[str] = (),
                 source: str = "policy_document", note: str | None = None) -> Param:
    return Param(name=name, value=int(value), unit="inr_paise", source=source,  # type: ignore[arg-type]
                 evidence_ids=tuple(evidence), note=note)


def _ratio_param(name: str, value: Fraction, *, source: str = "derived",
                 evidence: Sequence[str] = (), note: str | None = None) -> Param:
    return Param(name=name, value=value, unit="ratio", source=source,  # type: ignore[arg-type]
                 evidence_ids=tuple(evidence), note=note)


# ---------------------------------------------------------------------------
# Lawful graph (computation B)
# ---------------------------------------------------------------------------
def _link(graph: CalcGraph, src: str, dst: str, kind: str = "feeds",
          note: str | None = None) -> None:
    """Add an edge only when both of its endpoints exist.

    A step may be absent for a lawful reason: no non-payable items, no room-rent
    limit, no co-payment, no deductible. A graph must not carry an edge whose source
    was never emitted — ``CalcGraph.validate()`` refuses such a graph and the run
    aborts before any arithmetic. The chain therefore links the steps that are
    actually present. (Found by the Phase-3B Tier-1 benchmark: a case with a
    sum-insured ceiling but no co-payment produced the edge CP1 -> SI1 with no CP1.)
    """
    if graph.node(src) is None or graph.node(dst) is None:
        return
    graph.add_edge(src, dst, kind, note)


def build_lawful_graph(
    policy: PolicyFacts,
    *,
    room_days: int,
    room_rate_actual_paise: Money,
    non_payable_total_paise: Money = 0,
    non_payable_evidence: Sequence[str] = (),
    ame_heads: Sequence[CanonicalCategory] | None = None,
    barred_heads: Sequence[CanonicalCategory] = DEFAULT_BARRED_FROM_RATIO,
    graph_id: str | None = None,
    corpus_snapshot_id: str | None = None,
    rulepack_version: str | None = None,
    materiality_paise: int = 100_000,
    order_provenance: OrderProvenance | None = None,
) -> CalcGraph:
    """Build the graph that computes what *this policy* supports on *this bill*.

    ``ame_heads`` defaults to the policy's own definition. Heads barred by rule are
    removed from the ratio base: a wording that purports to include them is a rule
    breach, not a licence (rule PD.AME.EXCLUSIONS / PD.ICU.BAR / SCOPE.POLICY_VS_STATE).
    """
    gid = graph_id or f"GRAPH-{policy.policy_id}"
    ame = tuple(ame_heads if ame_heads is not None else policy.ame_heads)
    barred = set(barred_heads)
    lawful_ame = tuple(h for h in ame if h not in barred)

    provenance = order_provenance or (
        OrderProvenance.STATED if policy.stated_order else OrderProvenance.UNRESOLVED
    )

    graph = CalcGraph(
        graph_id=gid,
        policy_id=policy.policy_id,
        order_provenance=provenance,
        corpus_snapshot_id=corpus_snapshot_id,
        rulepack_version=rulepack_version,
        materiality_paise=materiality_paise,
    )

    position = 10

    # 1. non-payable items -------------------------------------------------
    if non_payable_total_paise > 0:
        graph.add_node(CalcNode(
            node_id="NP1",
            step_type=StepType.NON_PAYABLE_DEDUCTION,
            label="Non-payable items removed",
            input_base=BaseRef(kind="gross_bill"),
            operation="subtract",
            params=(_money_param("amount_paise", non_payable_total_paise,
                                 evidence=non_payable_evidence, source="policy_document",
                                 note="items the policy document lists as not payable"),),
            order_provenance=OrderProvenance.STATED,
            position=position,
            evidence_ids=tuple(non_payable_evidence),
            produced_by="assembler",
            notes="Base is the gross bill. Provenance: the policy's own annexure.",
        ))
        position += 10

    # 2. room-rent restriction ---------------------------------------------
    if policy.room_rent_limit is not None:
        limit = policy.room_rent_limit
        ev = (limit.evidence_id,) if limit.evidence_id else ()
        graph.add_node(CalcNode(
            node_id="RR1",
            step_type=StepType.ROOM_RENT_LIMIT,
            label="Room-rent restriction (eligible rate x days)",
            input_base=BaseRef(kind="room_charge_actual", stated_in=limit.evidence_id,
                               inference_note=limit.inference_note),
            operation="cap",
            params=(
                _money_param("rate_per_day_paise", limit.as_money(), evidence=ev),
                Param(name="days", value=int(room_days), unit="days", source="derived",
                      note="stay duration from the bill/discharge dates"),
            ),
            order_provenance=OrderProvenance.STATED,
            position=position,
            evidence_ids=ev,
            clause_ref="schedule",
            notes="The room rent itself is capped at the eligible rate; the excess is the patient's share.",
        ))
        _link(graph, "NP1", "RR1", "precedes")
        position += 10

    # 3. proportionate adjustment -------------------------------------------
    ratio = _room_adjustment_ratio(limit.as_money() if policy.room_rent_limit else None,
                                   room_rate_actual_paise)
    if ratio is not None and policy.ame_definition_present is not False:
        ev = (policy.ame_definition_evidence,) if policy.ame_definition_evidence else ()
        graph.add_node(CalcNode(
            node_id="PD1",
            step_type=StepType.PROPORTIONATE_ADJUSTMENT,
            label="Proportionate adjustment on associated medical expenses",
            input_base=BaseRef(kind="head_set", head_set=lawful_ame,
                               stated_in=policy.ame_definition_evidence,
                               inference_note=(None if ev else
                                               "scope taken from the policy's definition field")),
            operation="multiply_ratio",
            params=(
                _ratio_param("ratio", ratio, source="derived",
                             note="eligible rate / actual room rate (exact rational)"),
                Param(name="rounding", value="CARRY_EXACT_FINAL_ROUND", unit="",
                      source="derived",
                      note="engine default: one rounding event at the end. The policy states "
                           "the order of steps, not a rounding convention, so the default is "
                           "used and disclosed in the money-flow sheet."),
            ),
            contested=bool(barred & set(ame)) or not policy.calc_method_note,
            order_provenance=provenance,
            position=position,
            evidence_ids=ev,
            clause_ref="definition:associated medical expenses",
            notes=("Base is the head set the policy defines, minus heads a rule bars. "
                   f"Lawful AME heads: {[h.value for h in lawful_ame]}."),
        ))
        _link(graph, "RR1", "PD1")
        position += 10

    # 4. co-payment (base = running admissible) ------------------------------
    if policy.co_pay_percent is not None:
        pct = policy.co_pay_percent
        ev = (pct.evidence_id,) if pct.evidence_id else ()
        base_ref = (BaseRef(kind="running", stated_in=pct.evidence_id)
                    if policy.co_pay_base_stated
                    else BaseRef(kind="running",
                                 inference_note="co-pay base not stated in the policy; "
                                                "order is a named reading"))
        graph.add_node(CalcNode(
            node_id="CP1",
            step_type=StepType.COPAY,
            label="Co-payment",
            input_base=base_ref,
            operation="percentage",
            params=(Param(name="percent", value=pct.as_ratio() if hasattr(pct, "as_ratio") else pct.value,
                          unit="percent", source="policy_document", evidence_ids=ev),),
            order_provenance=OrderProvenance.STATED if policy.co_pay_base_stated
            else OrderProvenance.INFERRED,
            position=position,
            evidence_ids=ev,
            clause_ref="schedule:co-payment",
            notes=("Base is the running admissible amount after the steps before it. "
                   + ("Stated by the policy." if policy.co_pay_base_stated else
                      "NOT stated by the policy - position recorded as an assumption.")),
        ))
        _link(graph, "PD1", "CP1")
        _link(graph, "RR1", "CP1")
        position += 10

    # 5. deductible ---------------------------------------------------------
    if policy.deductible is not None and policy.deductible.as_money() > 0:
        d = policy.deductible
        ev = (d.evidence_id,) if d.evidence_id else ()
        graph.add_node(CalcNode(
            node_id="DD1",
            step_type=StepType.DEDUCTIBLE,
            label="Deductible",
            input_base=BaseRef(kind="running", stated_in=d.evidence_id),
            operation="subtract",
            params=(_money_param("amount_paise", d.as_money(), evidence=ev),),
            order_provenance=OrderProvenance.STATED if policy.stated_order
            else OrderProvenance.UNRESOLVED,
            position=position,
            evidence_ids=ev,
        ))
        position += 10

    # 6. sum-insured ceiling -------------------------------------------------
    if policy.sum_insured is not None:
        si = policy.sum_insured
        ev = (si.evidence_id,) if si.evidence_id else ()
        graph.add_node(CalcNode(
            node_id="SI1",
            step_type=StepType.SUM_INSURED_CEILING,
            label="Sum-insured ceiling",
            input_base=BaseRef(kind="running"),
            operation="ceiling",
            params=(_money_param("ceiling_paise", si.as_money(), evidence=ev),),
            order_provenance=OrderProvenance.STATED,
            position=position + 100,
            evidence_ids=ev,
        ))
        _link(graph, "CP1", "SI1")

    # ---- variants ---------------------------------------------------------
    _attach_scope_variants(graph, policy, lawful_ame, ame, ratio)
    _attach_order_variants(graph, policy)
    return graph


def _room_adjustment_ratio(eligible_rate: Money | None, actual_rate: Money) -> Fraction | None:
    if not eligible_rate or actual_rate <= 0:
        return None
    ratio = Fraction(eligible_rate, actual_rate)
    return ratio if ratio < 1 else None


def _attach_scope_variants(
    graph: CalcGraph,
    policy: PolicyFacts,
    lawful_ame: tuple[CanonicalCategory, ...],
    declared_ame: tuple[CanonicalCategory, ...],
    ratio: Fraction | None,
) -> None:
    """Named readings of the contested scope question (OV-3).

    R0 = the policy's own definition (minus rule-barred heads).
    R1 = the narrow reading some secondary sources attribute to the 2024 regime:
         the ratio may only touch the room-rent line.
    """
    if graph.node("PD1") is None:
        return
    if not lawful_ame:
        return
    if not policy.calc_method_note:
        graph.order_sensitivity = True
    graph.variants[ReadingId.PRIMARY.value] = GraphVariant(
        reading_id=ReadingId.PRIMARY.value,
        label="Policy definition of associated medical expenses",
        description=("The ratio applies to the heads this policy defines, minus heads "
                     "that rules bar from the definition."),
        authoritative=True,
    )
    graph.variants[ReadingId.ALT_SCOPE_NARROW.value] = GraphVariant(
        reading_id=ReadingId.ALT_SCOPE_NARROW.value,
        label="Room-rent-only reading (contested, PD.2024.SCOPE)",
        description=("Reading attributed by secondary sources to the 2024 regime: the "
                     "ratio may reduce only the room-rent line, and associated medical "
                     "expenses are paid at actuals. Not supported by the primary text "
                     "read; enumerated so the difference is visible, never chosen."),
        param_overrides={
            # Under this reading the ratio has no work beyond the room-rent restriction
            # already applied: associated medical expenses are paid at actuals, so the
            # payable share is 1 (a ratio of 5/8 is what the insurer used).
            "PD1": {"ratio": Fraction(1)},
        },
    )


def _attach_order_variants(graph: CalcGraph, policy: PolicyFacts) -> None:
    """When the policy does not state the co-pay base, the order is UNRESOLVED.

    No reading is invented: an unenumerable ambiguity is not something to guess at,
    so the graph is flagged and the verdict engine returns UNDETERMINED for the
    affected amount (Phase-2 §11.5, Case C: do not pick one).
    """
    if graph.node("CP1") is None or policy.co_pay_base_stated:
        return
    graph.order_sensitivity = True
    graph.notes.append(
        "the policy does not state the base of the co-payment; the affected amount is "
        "reported as UNDETERMINED rather than computed under a guessed order"
    )


# ---------------------------------------------------------------------------
# Restoration graph (the honest net ask)
# ---------------------------------------------------------------------------
def build_restoration_graph(
    policy: PolicyFacts,
    *,
    paid_paise: Money,
    barred_cut_paise: Money,
    copay_percent: Fraction | None,
    copay_evidence: Sequence[str] = (),
    si_evidence: Sequence[str] = (),
    graph_id: str | None = None,
    materiality_paise: int = 100_000,
    rulepack_version: str | None = None,
) -> CalcGraph:
    """Restore cuts that were not permitted, then re-apply the lawful downstream steps.

    This is the mechanism behind "ask for the net amount, not the gross cut": adding
    back an unlawfully cut amount raises the admissible base, on which the lawful
    co-payment is properly due. Executed with ``initial_running_paise = paid``.

    The identity that must hold — and is asserted in the verdict's G8 gate — is:

        restoration_payable + unexplained_deductions == lawful_payable
    """
    graph = CalcGraph(
        graph_id=graph_id or f"RESTORE-{policy.policy_id}",
        policy_id=policy.policy_id,
        order_provenance=OrderProvenance.STATED,
        materiality_paise=materiality_paise,
        rulepack_version=rulepack_version,
        notes=["Restoration graph: starts from the amount paid and adds back only what a "
               "rule, not a convention, permitted."],
    )

    # CB1: add back the rule-barred cut. A negative amount means "restore".
    graph.add_node(CalcNode(
        node_id="CB1",
        step_type=StepType.PROPORTIONATE_CLAWBACK,
        label="Add back cuts a rule did not permit",
        input_base=BaseRef(kind="running"),
        operation="subtract",
        params=(Param(name="amount_paise", value=-int(barred_cut_paise), unit="inr_paise",
                      source="rule",
                      note="negative amount == restoration; the amount is proven by the "
                           "rule-barred scope check, never by the settlement letter"),),
        order_provenance=OrderProvenance.STATED,
        position=20,
        produced_by="assembler",
        notes="Rule-sourced (PD.AME.EXCLUSIONS / PD.ICU.BAR), not policy-sourced.",
    ))

    position = 30
    last = "CB1"
    if copay_percent is not None and barred_cut_paise > 0:
        graph.add_node(CalcNode(
            node_id="RC1",
            step_type=StepType.RECOMPUTE_DOWNSTREAM,
            label="Re-apply the lawful co-payment to the restored amount",
            input_base=BaseRef(kind="node_delta", node_id="CB1"),
            operation="multiply_ratio",
            params=(
                _ratio_param("ratio", copay_percent / 100, source="policy_document",
                             evidence=copay_evidence,
                             note="the lawful co-payment percentage, applied to the restored "
                                  "delta only (bounded, single pass)"),
                Param(name="rounding", value="CARRY_EXACT_FINAL_ROUND", unit="", source="derived"),
            ),
            order_provenance=OrderProvenance.STATED,
            position=position,
            produced_by="assembler",
            notes="Downstream step re-applied to the restored base.",
        ))
        _link(graph, "CB1", "RC1", "compensates",
              note="restoration couples to the lawful downstream co-payment")
        last = "RC1"
        position += 10

    if policy.sum_insured is not None:
        graph.add_node(CalcNode(
            node_id="SI1",
            step_type=StepType.SUM_INSURED_CEILING,
            label="Sum-insured ceiling",
            input_base=BaseRef(kind="running_after", node_id=last),
            operation="ceiling",
            params=(_money_param("ceiling_paise", policy.sum_insured.as_money(),
                                 evidence=si_evidence, source="policy_document"),),
            order_provenance=OrderProvenance.STATED,
            position=position,
            produced_by="assembler",
        ))
    return graph
