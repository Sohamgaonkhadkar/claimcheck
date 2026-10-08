"""The deterministic pipeline for one case, from structured facts to a verdict.

This is step 16 of the build order and it contains **no model, no OCR and no
retrieval**. Its input is the structured form that document readers must eventually
produce (and that the golden fixture produces by hand); its output is the report data
and the adjudication.

    policy + bill + settlement + rules  ->  graph -> calculator -> A/B/C order
    ->  insurer reconstruction  ->  reconciliation  ->  verdict

Each arrow is a typed call, and the whole run is reproducible from the case record:
same inputs, same numbers, same words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from .calc.executor import CaseContext, ExecutionResult, execute_graph
from .calc.money import format_inr
from .calc.readings import OrderAmbiguity, analyse_order
from .evidence.spans import PageIndex
from .graph.build import (
    DEFAULT_BARRED_FROM_RATIO,
    build_lawful_graph,
    build_restoration_graph,
)
from .graph.model import CalcGraph
from .logic import FactProvider
from .reconcile.model import InsurerModel, Reconciliation, reconstruct_insurer_model, reconcile
from .rules.evaluator import (
    RuleContext,
    RuleEvaluation,
    ScopeCheck,
    check_ratio_scope,
    evaluate_pack,
)
from .rules.items import ConditionContext, evaluate_items
from .rules.model import RulePack, load_default_rulepack
from .schema import BillFacts, CanonicalCategory, Evidence, FactStore, Page, PolicyFacts, SettlementFacts
from .verdict.engine import VerdictInputs, adjudicate
from .verdict.findings import Adjudication

#: which instrument bars which head from the ratio. Policy-independent: it is the rule,
#: not the wording, that decides this, and the rule is cited by id.
BARRED_HEAD_RULES: Mapping[str, tuple[str, ...]] = {
    "pharmacy": ("PD.AME.EXCLUSIONS",),
    "consumables": ("PD.AME.EXCLUSIONS",),
    "implants": ("PD.AME.EXCLUSIONS",),
    "medical_devices": ("PD.AME.EXCLUSIONS",),
    "diagnostics": ("PD.AME.EXCLUSIONS",),
    "icu": ("PD.ICU.BAR",),
}

REQUIRED_DOCUMENTS = ("policy_wording", "policy_schedule", "bill", "settlement")


@dataclass
class StructuredCase:
    """A case as facts, before any document intelligence is involved."""

    case_id: str
    origin: str                              # synthetic | consented | public
    policy: PolicyFacts
    bill: BillFacts
    settlement: SettlementFacts
    facts: FactStore
    pages: PageIndex
    documents: Mapping[str, str]             # role -> document_id
    evidence: Mapping[str, Evidence]
    corpus_snapshot_id: str = ""             # immutable snapshot the documents came from
    candidate_head_sets: Mapping[str, Sequence[str]] = field(default_factory=dict)
    barred_heads: Sequence[CanonicalCategory] = DEFAULT_BARRED_FROM_RATIO
    as_of: date | None = None
    history_notes: tuple[str, ...] = ()
    # Persisted-input snapshot revision, populated only by the application adapter.
    input_revision: int | None = None


@dataclass
class CaseRun:
    case: StructuredCase
    ctx: CaseContext
    graph: CalcGraph
    ambiguity: OrderAmbiguity
    execution: ExecutionResult
    readings: Mapping[str, int]
    insurer: InsurerModel
    restoration: ExecutionResult
    reconciliation: Reconciliation
    rules: RuleEvaluation
    adjudication: Adjudication
    non_payable_paise: int = 0
    undecided_items: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def paid(self) -> int:
        return int(self.case.settlement.final_payable)

    @property
    def lawful_payable(self) -> int:
        return int(self.execution.payable_paise)


# ---------------------------------------------------------------------------
def run_case(case: StructuredCase,
             pack: RulePack | None = None,
             *,
             as_of: date | None = None) -> CaseRun:
    pack = pack or load_default_rulepack()
    as_of = as_of or case.as_of or date.today()

    ctx = CaseContext(
        bill=case.bill,
        policy=case.policy,
        settlement=case.settlement,
        facts=case.facts,
        named={
            "policy.sum_insured": case.policy.sum_insured.as_money()
            if case.policy.sum_insured else 0,
            "bill.room_days": case.bill.room_days or 0,
            "bill.room_rate_actual": case.bill.room_rate or 0,
        },
    )

    snapshot = case.corpus_snapshot_id or f"SNAP-{case.case_id}"

    # ---- items: conditional, sourced, and never silently assumed -----------
    non_payable, undecided = _item_totals(case)

    graph = build_lawful_graph(
        case.policy,
        room_days=case.bill.room_days or 0,
        room_rate_actual_paise=case.bill.room_rate or 0,
        non_payable_total_paise=non_payable,
        non_payable_evidence=tuple(
            i.evidence_id for i in case.policy.non_payable_items if i.evidence_id
        ),
        ame_heads=case.policy.ame_heads,
        barred_heads=case.barred_heads,
        rulepack_version=pack.version,
        corpus_snapshot_id=snapshot,
    )

    ambiguity = analyse_order(graph, ctx)
    selected = ambiguity.selected_reading or "R0"
    execution = execute_graph(graph, ctx, reading_id=selected)
    readings = {r.reading_id: int(r.payable_paise) for r in ambiguity.readings}

    insurer = reconstruct_insurer_model(
        case.settlement, case.bill,
        barred_heads=[h.value for h in case.barred_heads],
        candidate_head_sets=case.candidate_head_sets,
        policy_items=tuple((i.canonical_name, (i.canonical_name, *i.aliases))
                           for i in case.policy.non_payable_items),
    )

    rule_ctx = _rule_context(case, insurer, as_of)
    rules = evaluate_pack(pack, rule_ctx, as_of, claim_date=case.policy.claim_date)

    # Which heads the ratio had no lawful scope over: decided by the rules and the
    # policy's own definition, never by what the letter happened to label.
    scope = check_ratio_scope(
        applied_heads=insurer.implied_scope,
        policy_defined_heads=[h.value for h in case.policy.ame_heads],
        rule_barred_heads=[h.value for h in case.barred_heads],
    )
    barred_cuts = {h.head: h.cut_paise for h in insurer.head_cuts if scope.is_barred(h.head)}
    restricted = tuple(
        (h.head, h.cut_paise, scope.rules_for(h.head))
        for h in insurer.head_cuts if scope.is_barred(h.head)
    )
    restoration = _restoration(case, pack, ctx,
                               paid=insurer.net_payable_paise,
                               barred_cut=insurer.barred_cut_paise)
    # Every fractional step the restoration route re-applies rounds once, half-up, on the
    # restored amount -- a *different* base from the one the recomputation rounded on.
    # Each such step can move the two routes apart by one paisa, so the slack is their
    # count and nothing more. (The count grew when the Tier-1 benchmark showed A10, whose
    # only fractional step is the proportionate clawback, still failing by one paisa.)
    rounding_slack = sum(1 for step in restoration.run.trace
                         if step.step_type in ("COPAY", "RECOMPUTE_DOWNSTREAM", "DEDUCTIBLE",
                                               "PROPORTIONATE_CLAWBACK"))
    reconciliation = reconcile(
        insurer,
        lawful_payable_paise=execution.payable_paise,
        restoration_payable_paise=restoration.payable_paise,
        readings=tuple(sorted(readings.items())),
        rounding_slack_paise=rounding_slack,
    )

    adjudication = adjudicate(VerdictInputs(
        policy=case.policy,
        bill=case.bill,
        settlement=case.settlement,
        graph=graph,
        execution=execution,
        readings=readings,
        order_case=ambiguity.case,
        order_material=ambiguity.material,
        order_reason=ambiguity.reason,
        documents_present=frozenset(k for k in case.documents),
        documents_required=REQUIRED_DOCUMENTS,
        evidence_document_ids={e.evidence_id: e.document_id for e in case.evidence.values()},
        rule_evaluation=rules,
        insurer=insurer,
        facts=case.facts,
        reconciliation=reconciliation,
        restoration_nodes=tuple(s.node_id for s in restoration.run.trace),
        barred_heads_cut=barred_cuts,
        barred_heads_rules={h: rids for h, _amt, rids in restricted},
        head_evidence=_head_evidence_with_ratio(case, insurer, tuple(barred_cuts)),
        unlawful_cut_gross_paise=insurer.barred_cut_paise,
        as_of=as_of,
    ))

    return CaseRun(
        case=case,
        ctx=ctx,
        graph=graph,
        ambiguity=ambiguity,
        execution=execution,
        readings=readings,
        insurer=insurer,
        restoration=restoration,
        reconciliation=reconciliation,
        rules=rules,
        adjudication=adjudication,
        non_payable_paise=non_payable,
        undecided_items=undecided,
        notes=tuple(case.history_notes) + tuple(graph.notes)
        + tuple(insurer.notes) + tuple(rules.notes) + _scope_notes(scope),
    )


# ---------------------------------------------------------------------------
def _scope_notes(scope: ScopeCheck) -> tuple[str, ...]:
    out: list[str] = []
    if scope.indeterminate:
        out.append("the policy defines no associated medical expenses, so the ratio has no "
                   "lawful scope anywhere (rule PD.AME.DEFINE)")
    for head in scope.outside_definition:
        out.append(f"{head}: the ratio was applied to a head the policy's own definition "
                   f"does not include (rule PD.SCOPE.NO_EXTRA)")
    for head in scope.rule_barred:
        out.append(f"{head}: an instrument bars the ratio from this head "
                   f"({', '.join(scope.rules_for(head))})")
    return tuple(out)


def _head_evidence_with_ratio(case: StructuredCase, insurer, heads: Sequence[str],
                              ) -> Mapping[str, Mapping[str, str]]:
    by_head = {k: dict(v) for k, v in _head_evidence(case, heads).items()}
    for head in heads:
        by_head.setdefault(head, {})
    _attach_ratio_evidence(case, insurer, by_head)
    return {k: v for k, v in by_head.items()}


def _ratio_line_evidence(case: StructuredCase) -> str | None:
    for ded in case.settlement.deductions:
        if ded.ratio_stated and ded.evidence_id:
            return ded.evidence_id
    return None


def _head_evidence(case: StructuredCase, heads: Sequence[str]) -> Mapping[str, Mapping[str, str]]:
    """The bill and settlement spans behind each cut head, by evidence id."""
    by_head: dict[str, dict[str, str]] = {}
    for line in case.bill.lines:
        head = line.category.value
        if head in heads and line.evidence_id:
            by_head.setdefault(head, {})["bill"] = line.evidence_id
    for ded in case.settlement.deductions:
        head = (ded.head or "").lower()
        if head in heads and ded.evidence_id:
            by_head.setdefault(head, {})["settlement"] = ded.evidence_id
    return {k: v for k, v in by_head.items()}


def _attach_ratio_evidence(case: StructuredCase, insurer, by_head: dict) -> None:
    """A single settlement line can cover several heads; anchor each to its own span."""
    for line in insurer.lines:
        if line.attributed_by != "implied_scope":
            continue
        for head in insurer.implied_scope:
            if line.cut_paise and head in by_head:
                by_head[head].setdefault("settlement", _ratio_line_evidence(case))


def _item_totals(case: StructuredCase) -> tuple[int, tuple[str, ...]]:
    """Sum the items the policy document itself excludes; name what stays undecided."""
    total = 0
    undecided: list[str] = []
    for line in case.bill.lines:
        verdicts = evaluate_items(case.policy.non_payable_items,
                                  f"{line.raw_description} {line.category.value}",
                                  ConditionContext({"prescribed": None}))
        for v in verdicts:
            if v.is_non_payable:
                total += int(line.amount)
            elif v.state.value == "INDETERMINATE":
                undecided.append(v.canonical_name)
    return total, tuple(dict.fromkeys(undecided))


def _rule_context(case: StructuredCase, insurer: InsurerModel, as_of: date) -> RuleContext:
    """Facts the rule pack may test, set explicitly. Anything absent stays UNKNOWN."""
    ctx = RuleContext(claim_date=case.policy.claim_date)
    ctx.set_many({
        "policy.era_in_scope": _era_in_scope(case),
        "policy.ame_definition_present": case.policy.ame_definition_present,
        "claim.proportionate_deduction_applied": bool(insurer.ratio_cut),
        "claim.applied_scope_heads": list(insurer.implied_scope),
        "claim.partially_disallowed": bool(case.settlement.partial_disallowance),
        "claim.claim_date": case.policy.claim_date,
        "settlement.cites_specific_policy_terms": case.settlement.cites_specific_policy_terms,
        "settlement.demands_documents_from_policyholder":
            case.settlement.demands_documents_from_policyholder,
        "settlement.includes_ombudsman_details": case.settlement.includes_ombudsman_details,
        "policy.non_payable_items_present": bool(case.policy.non_payable_items),
    })
    # Facts the *case record* may itself state, read from the fact store with the
    # store's own three-valued semantics: a fact the documents establish is passed on
    # with its evidence behind it; a fact no document states stays absent, and the
    # rule that needs it is reported as INDETERMINATE rather than defaulted.
    #
    # (Before the Phase-3B benchmark this function withheld all of these outright.
    # A generated case whose bill states the hospital's billing practice and whose
    # letter states its decision then produced EVIDENCE_GAP findings for facts the
    # record plainly carried -- and, because a claims-process head was left
    # UNDETERMINED, no case could ever be internally consistent. The store is the
    # mechanism designed for this; the pipeline now uses it.)
    for fact_id in ("hospital.follows_differential_billing",
                    "claim.repudiated",
                    "claim.tat_bound_known",
                    "claim.decision_within_30_days",
                    "claim.decision_within_45_days",
                    "claim.investigation_warranted",
                    "claim.is_cashless",
                    "claim.grievance_raised",
                    "claim.repudiation_ground_non_disclosure"):
        fact = case.facts.get(fact_id)
        if fact is not None and fact.usable:
            ctx.set(fact_id, fact.value)
    return ctx


def _era_in_scope(case: StructuredCase) -> bool | None:
    """Products filed on/after 01.10.2020, existing contracts on renewal from 01.04.2021.

    Tested on the policy's own inception/renewal dates. Never on the UIN: the 2020
    instruments were implemented by re-issuing wordings without changing the UIN.
    """
    reference = case.policy.renewal_date or case.policy.inception_date
    if reference is None:
        return None
    return reference >= date(2021, 4, 1)


def _restoration(case: StructuredCase, pack: RulePack, ctx: CaseContext,
                 *, paid: int, barred_cut: int):
    copay = None
    if case.policy.co_pay_percent is not None and case.policy.co_pay_percent.value is not None:
        copay = case.policy.co_pay_percent.as_ratio()
    graph = build_restoration_graph(
        case.policy,
        paid_paise=paid,
        barred_cut_paise=barred_cut,
        copay_percent=copay,
        copay_evidence=((case.policy.co_pay_percent.evidence_id,)
                        if case.policy.co_pay_percent and case.policy.co_pay_percent.evidence_id
                        else ()),
        si_evidence=((case.policy.sum_insured.evidence_id,)
                     if case.policy.sum_insured and case.policy.sum_insured.evidence_id else ()),
        rulepack_version=pack.version,
    )
    return execute_graph(graph, ctx, initial_running_paise=paid, check_conservation=False)
