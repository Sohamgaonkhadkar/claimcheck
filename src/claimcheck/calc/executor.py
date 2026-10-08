"""Graph execution: the only place a payable is produced.

Semantics (the part that has to be exactly right)
-------------------------------------------------
Every node has a *base* and produces an *output* from a closed-set operation. What
the node does to the running payable depends on its **step type**, because step
types carry meaning and operations only carry arithmetic:

``reduces_running``  (NON_PAYABLE_DEDUCTION, ROOM_RENT_LIMIT, ICU_LIMIT,
                      PROPORTIONATE_ADJUSTMENT, SUBLIMIT_CAP, PACKAGE_CAP)
    * base is a head or head set  -> the payable loses ``base - output``
      (the head is capped at what the policy allows);
    * base is the running amount  -> the payable becomes ``output``.

``withholds_fraction`` (COPAY, DEDUCTIBLE, RECOMPUTE_DOWNSTREAM)
    * ``output`` is the amount withheld from the payable on that base;
      the payable loses it and it is recorded as the patient's share.

``adds_back`` (PROPORTIONATE_CLAWBACK)
    * ``output`` is computed from a *delta* and added back to the payable
      (used to restore a cut that a rule did not permit).

``replaces_running`` (SUM_INSURED_CEILING)
    * the payable becomes ``min(running, ceiling)``.

Accounting buckets are kept separate so the conservation invariant is meaningful:

    gross_bill = payable + reductions + patient_share

where ``reductions`` are amounts removed from the claim and ``patient_share`` is
what the patient bears (room-rate excess, co-pay, deductible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Sequence

from ..errors import (
    CalcGraphInvalid,
    CalcInvariantViolation,
    CalcMissingBase,
    CalcMissingParam,
)
from ..graph.model import BaseRef, CalcGraph, CalcNode, StepType
from ..logic import FactProvider, Tri, evaluate_predicate
from ..schema import (
    BillFacts,
    CanonicalCategory,
    FactStore,
    Money,
    OrderProvenance,
    PolicyFacts,
    ReadingId,
    SettlementFacts,
)
from .engine import CalcRun, CalcStep, apply_operation, assert_invariants
from .money import format_inr

# ---------------------------------------------------------------------------
# Step-type semantics
# ---------------------------------------------------------------------------
_REDUCES_RUNNING = {
    StepType.NON_PAYABLE_DEDUCTION,
    StepType.ROOM_RENT_LIMIT,
    StepType.ICU_LIMIT,
    StepType.PROPORTIONATE_ADJUSTMENT,
    StepType.SUBLIMIT_CAP,
    StepType.PACKAGE_CAP,
}
_WITHHOLDS_FRACTION = {StepType.COPAY, StepType.DEDUCTIBLE, StepType.RECOMPUTE_DOWNSTREAM}
_ADDS_BACK = {StepType.PROPORTIONATE_CLAWBACK}
_REPLACES_RUNNING = {StepType.SUM_INSURED_CEILING}

_HEAD_BASES = {"head_set", "room_charge_actual", "icu_charge_actual"}


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------
@dataclass
class CaseContext:
    """Everything the executor may read. No model, no network, no document bytes."""

    bill: BillFacts
    policy: PolicyFacts
    settlement: SettlementFacts | None = None
    facts: FactStore = field(default_factory=FactStore)
    page_quality: Mapping[str, float] = field(default_factory=dict)
    named: dict[str, object] = field(default_factory=dict)

    def provider(self) -> FactProvider:
        return FactProvider(self.named, store=self.facts)

    def category_sum(self, categories: Sequence[CanonicalCategory]) -> Money:
        return self.bill.sum_of(categories)


@dataclass(frozen=True)
class NodeOutcome:
    node_id: str
    status: str  # 'applied' | 'skipped'
    reason: str | None
    output_paise: int
    delta_paise: int = 0
    step: CalcStep | None = None


@dataclass
class ExecutionResult:
    run: CalcRun
    outcomes: tuple[NodeOutcome, ...]
    gross_paise: int
    reductions_paise: int
    payable_paise: int
    patient_share_paise: int
    assumptions: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    deltas: Mapping[str, int] = field(default_factory=dict)

    def step(self, node_id: str) -> CalcStep | None:
        return self.run.step(node_id)

    def delta_of(self, node_id: str) -> int:
        return int(self.deltas.get(node_id, 0))

    def node(self, node_id: str) -> NodeOutcome | None:
        for o in self.outcomes:
            if o.node_id == node_id:
                return o
        return None


# ---------------------------------------------------------------------------
# Base resolution
# ---------------------------------------------------------------------------
def resolve_base(base: BaseRef, ctx: CaseContext, running: int,
                 produced: Mapping[str, int], deltas: Mapping[str, int],
                 params: Mapping[str, object] | None = None,
                 running_after: Mapping[str, int] | None = None) -> int:
    if base.kind == "gross_bill":
        return ctx.bill.bill_total
    if base.kind == "head_set":
        heads = _head_override(base, params)
        return ctx.category_sum(heads)
    if base.kind == "room_charge_actual":
        return ctx.category_sum((CanonicalCategory.ROOM,))
    if base.kind == "icu_charge_actual":
        return ctx.category_sum((CanonicalCategory.ICU,))
    if base.kind == "claimed":
        if ctx.settlement is None:
            raise CalcMissingBase("claimed amount requires a settlement letter")
        return ctx.settlement.claimed_amount
    if base.kind == "sum_insured":
        if ctx.policy.sum_insured is None:
            raise CalcMissingBase("sum insured missing from the policy schedule")
        return ctx.policy.sum_insured.as_money()
    if base.kind == "running":
        return running
    if base.kind == "node_output":
        if base.node_id not in produced:
            raise CalcMissingBase(f"node {base.node_id} produced no value")
        return produced[base.node_id]
    if base.kind == "node_delta":
        if base.node_id not in deltas:
            raise CalcMissingBase(f"node {base.node_id} produced no delta")
        return deltas[base.node_id]
    if base.kind == "running_after":
        if not running_after or base.node_id not in running_after:
            raise CalcMissingBase(f"node {base.node_id} has no running total recorded")
        return running_after[base.node_id]
    raise CalcMissingBase(f"unknown base kind {base.kind!r}")


def _head_override(base: BaseRef, params: Mapping[str, object] | None) -> tuple[CanonicalCategory, ...]:
    """A named reading may substitute the head set (e.g. the room-rent-only reading)."""
    if params and "head_set_override" in params:
        raw = params["head_set_override"]
        return tuple(CanonicalCategory(str(v)) for v in raw)  # type: ignore[arg-type]
    return base.head_set


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------
def evaluate_applicability(node: CalcNode, ctx: CaseContext) -> tuple[bool, str]:
    if node.applicability is None:
        return True, "no condition"
    result = evaluate_predicate(node.applicability, ctx.provider())
    if result.value is Tri.TRUE:
        return True, result.explain()
    if result.value is Tri.FALSE:
        return False, f"condition false: {result.explain()}"
    return False, f"condition INDETERMINATE: {result.explain()}"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
def execute_graph(
    graph: CalcGraph,
    ctx: CaseContext,
    *,
    reading_id: str = ReadingId.PRIMARY.value,
    run_id: str | None = None,
    initial_running_paise: int | None = None,
    conservation_base_paise: int | None = None,
    check_conservation: bool = True,
) -> ExecutionResult:
    problems = graph.validate()
    if problems:
        raise CalcGraphInvalid("graph failed validation: " + "; ".join(problems),
                               graph_id=graph.graph_id, problems=list(problems))

    ordered = graph.ordered_nodes(reading_id)
    produced: dict[str, int] = {}
    deltas: dict[str, int] = {}
    running_after: dict[str, int] = {}
    outcomes: list[NodeOutcome] = []
    trace: list[CalcStep] = []
    assumptions: list[str] = []
    flags: list[str] = []
    reductions = 0
    patient_share = 0
    running = initial_running_paise if initial_running_paise is not None else ctx.bill.bill_total
    start_running = running
    seq = 0

    for node in ordered:
        if node.order_provenance in (OrderProvenance.INFERRED, OrderProvenance.IMPLIED):
            assumptions.append(f"{node.node_id}: position {node.order_provenance.value}")
        if node.order_provenance is OrderProvenance.DEMO_ONLY:
            flags.append(f"{node.node_id}: DEMO-ONLY ordering, not for production cases")

        applicable, why = evaluate_applicability(node, ctx)
        if not applicable:
            indeterminate = "INDETERMINATE" in why
            if indeterminate:
                flags.append(f"{node.node_id}: applicability indeterminate")
            produced[node.node_id] = running
            deltas[node.node_id] = 0
            running_after[node.node_id] = running
            outcomes.append(NodeOutcome(node.node_id, "skipped", why, running, 0))
            continue

        for p in node.params:
            if not p.is_trusted_input:
                raise CalcMissingParam(
                    f"{node.node_id}.{p.name} is sourced from the settlement letter; a "
                    "reconciliation input may never drive the recomputation",
                    node_id=node.node_id, param=p.name,
                )

        params = graph.effective_params(node, reading_id)
        base_value = resolve_base(node.input_base, ctx, running, produced, deltas, params,
                                  running_after)
        before = running
        rounding = None

        if node.step_type in _REPLACES_RUNNING:
            ceiling = _param_money(node, params, "ceiling_paise")
            output, _ = apply_operation("ceiling", base_paise=base_value,
                                        params={"ceiling_paise": ceiling})
            if base_value > output:
                lost = base_value - output
                reductions += lost
                patient_share += lost
            running = output

        elif node.step_type in _ADDS_BACK:
            # A clawback is parameterised by a *negative* amount: "-1800000" means
            # "restore 1800000 paise". The shift is the amount restored, never the
            # new running total.
            amount = _param_money(node, params, "amount_paise")
            shift = -amount
            if shift < 0:
                raise CalcMissingParam(
                    f"{node.node_id}: a clawback may only add to the payable",
                    node_id=node.node_id,
                )
            running = running + shift
            reductions -= shift
            output = shift

        elif node.step_type in _WITHHOLDS_FRACTION:
            output, rounding = _apply_node_op(node, params, base_value)
            charge = output
            running = running - charge
            if node.step_type is StepType.RECOMPUTE_DOWNSTREAM:
                reductions -= 0  # a re-applied lawful charge is a patient share, not a reduction
            patient_share += charge

        elif node.step_type in _REDUCES_RUNNING:
            output, rounding = _apply_node_op(node, params, base_value)
            if node.input_base.kind in _HEAD_BASES:
                lost = base_value - output
                running = running - lost
                if node.step_type in (StepType.NON_PAYABLE_DEDUCTION,
                                      StepType.PROPORTIONATE_ADJUSTMENT,
                                      StepType.SUBLIMIT_CAP,
                                      StepType.PACKAGE_CAP):
                    reductions += lost
                else:  # room/ICU rate restriction: the excess is the patient's share
                    patient_share += lost
            else:
                lost = base_value - output
                running = output
                reductions += lost
        else:
            raise CalcMissingParam(f"{node.node_id}: unhandled step type {node.step_type}")

        delta = running - before
        deltas[node.node_id] = delta
        produced[node.node_id] = output
        running_after[node.node_id] = running

        step = CalcStep(
            sequence=seq,
            node_id=node.node_id,
            step_type=node.step_type.value,
            label=node.label,
            inputs_named={"base_paise": base_value, "base": node.input_base.describe()},
            operation=node.operation,
            params={k: _render(v) for k, v in params.items()},
            output_paise=output,
            param_sources={p.name: str(getattr(p.source, "value", p.source)) for p in node.params},
            note=node.notes,
        )
        trace.append(step)
        seq += 1
        outcomes.append(NodeOutcome(node.node_id, "applied", why, output, delta, step))

    si = ctx.policy.sum_insured.as_money() if ctx.policy.sum_insured else None
    base_for_conservation = (conservation_base_paise if conservation_base_paise is not None
                             else ctx.bill.bill_total)
    violations = assert_invariants(
        payable_paise=running,
        gross_paise=base_for_conservation,
        reductions_paise=reductions,
        steps=trace,
        si_paise=None if check_conservation is False else si,
        patient_share_paise=patient_share,
        check_conservation_flag=check_conservation,
    )
    run = CalcRun(
        run_id=run_id or f"RUN-{graph.graph_id}-{reading_id}",
        graph_id=graph.graph_id,
        reading_id=reading_id,
        payable_paise=running,
        trace=trace,
        invariant_status="violation" if violations else "ok",
        violations=violations,
        total_reductions_paise=reductions,
    )
    return ExecutionResult(
        run=run,
        outcomes=tuple(outcomes),
        gross_paise=base_for_conservation,
        reductions_paise=reductions,
        payable_paise=running,
        patient_share_paise=patient_share,
        assumptions=tuple(assumptions),
        flags=tuple(sorted(set(flags))),
        deltas=deltas,
    )


def execute_or_abort(graph: CalcGraph, ctx: CaseContext, **kwargs) -> ExecutionResult:
    """Execute and hard-fail on invariant violation (a defect, never a finding)."""
    result = execute_graph(graph, ctx, **kwargs)
    if result.run.violations:
        raise CalcInvariantViolation(
            "calculation invariants violated: " + "; ".join(result.run.violations),
            graph_id=graph.graph_id, violations=list(result.run.violations),
        )
    return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _apply_node_op(node: CalcNode, params: Mapping[str, object], base: int) -> tuple[int, object]:
    if node.operation in ("identity", "threshold"):
        return base, None
    if node.operation == "subtract":
        amount = _param_money(node, params, "amount_paise")
        if node.step_type in _WITHHOLDS_FRACTION:
            # The contract above: for a withholding step, ``output`` is the amount
            # withheld from the payable, never what remains of it. A deductible of
            # 4,000,000 paise on a 10,000,000 paise payable withholds 4,000,000; it does
            # not leave 4,000,000. (Found by the Phase-3B Tier-1 benchmark, archetype
            # A04: with operation="subtract" the payable came out equal to the
            # deductible itself.)
            return min(amount, base), None
        return base - amount, None
    if node.operation == "cap":
        if "limit_paise" in params:
            limit = _param_money(node, params, "limit_paise")
        elif "rate_per_day_paise" in params and "days" in params:
            limit = _param_money(node, params, "rate_per_day_paise") * int(params["days"])
        else:
            raise CalcMissingParam(f"{node.node_id}: cap needs limit_paise or rate_per_day_paise + days")
        return apply_operation("cap", base_paise=base, params={"limit_paise": limit})
    if node.operation == "floor":
        return apply_operation("floor", base_paise=base,
                               params={"minimum_paise": _param_money(node, params, "minimum_paise")})
    if node.operation == "ceiling":
        return apply_operation("ceiling", base_paise=base,
                               params={"ceiling_paise": _param_money(node, params, "ceiling_paise")})
    if node.operation == "multiply_ratio":
        ratio = params.get("ratio")
        if ratio is None or isinstance(ratio, str):
            raise CalcMissingParam(f"{node.node_id}: ratio must be an exact Fraction")
        return apply_operation("multiply_ratio", base_paise=base,
                               params={"ratio": ratio,
                                       "rounding": params.get("rounding", "CARRY_EXACT_FINAL_ROUND")})
    if node.operation == "percentage":
        pct = params.get("percent")
        if pct is None:
            raise CalcMissingParam(f"{node.node_id}: percent is required")
        if isinstance(pct, str):
            raise CalcMissingParam(f"{node.node_id}: percent must be exact, not a string")
        return apply_operation("percentage", base_paise=base,
                               params={"percent": pct,
                                       "rounding": params.get("rounding", "CARRY_EXACT_FINAL_ROUND")})
    raise CalcMissingParam(f"{node.node_id}: unsupported operation {node.operation!r}")


def _param_money(node: CalcNode, params: Mapping[str, object], name: str) -> int:
    if name not in params:
        raise CalcMissingParam(f"{node.node_id}: missing parameter {name}", node_id=node.node_id)
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalcMissingParam(f"{node.node_id}.{name} must be integer paise, got {value!r}")
    return value


def _render(value: object) -> object:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    return value


def describe_money(paise: int) -> str:
    return format_inr(paise)
