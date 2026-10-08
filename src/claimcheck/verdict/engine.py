"""The verdict engine: gates, findings, and the sentence the system is allowed to say.

Rules of engagement, all enforced here:

  * A finding about money is produced only from calculator output. There is no path in
    this file that computes an amount.
  * A gate that fails does not suppress the finding; it changes what the finding may
    claim. A missing document produces an ``EVIDENCE_GAP`` naming the document, never
    a money finding with a smaller number.
  * Uncertainty is never converted into a verdict: a contested rule, an unevidenced
    scope or an unresolved order produce ``UNDETERMINED`` for the affected head.
  * A deduction the documents support produces **no finding at all** — only an
    ``INFO`` line. Removing a supported outcome to make the case look stronger is the
    one failure mode this file exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from ..calc.executor import CaseContext, ExecutionResult
from ..calc.money import format_inr
from ..graph.model import CalcGraph
from ..logic import Tri
from ..reconcile.model import InsurerModel, Reconciliation
from ..rules.evaluator import RESULT_APPLIES, RESULT_CONTESTED, RESULT_INDETERMINATE, RESULT_UNVERIFIED, RuleEvaluation
from .findings import Adjudication, Citation, Finding, GateResult, State

HEAD_RATIO = "proportionate_deduction"
HEAD_ROOM = "room_rent_restriction"
HEAD_COPAY = "co_payment"
HEAD_UNEXPLAINED = "unexplained_deductions"
HEAD_PROCESS = "claims_process"
HEAD_PROVENANCE = "provenance"
HEAD_CONSISTENCY = "internally_consistent"


@dataclass
class VerdictInputs:
    policy: Any
    bill: Any
    settlement: Any
    graph: CalcGraph
    execution: ExecutionResult                         # the primary (lawful) reading
    readings: Mapping[str, int]                        # reading_id -> lawful payable
    order_case: str                                    # A | B | C
    order_material: bool
    order_reason: str
    documents_present: frozenset[str]
    documents_required: Sequence[str]
    evidence_document_ids: Mapping[str, str]           # evidence_id -> document_id
    rule_evaluation: RuleEvaluation
    rules: Mapping[str, str] = field(default_factory=dict)
    insurer: InsurerModel | None = None
    reconciliation: Reconciliation | None = None
    restoration_nodes: tuple[str, ...] = ()
    barred_heads_cut: Mapping[str, int] = field(default_factory=dict)
    barred_heads_rules: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    head_evidence: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unlawful_cut_gross_paise: int = 0
    as_of: date | None = None
    facts: Any | None = None        # the FactStore, so an unusable fact can be reported


# -- gates ------------------------------------------------------------------
def run_gates(v: VerdictInputs) -> tuple[GateResult, ...]:
    """G1 extraction, G2 document set, G3 policy support, G5 order, G6 calculation,
    G8 provenance. A gate is a question with an answer, not a score."""
    gates: list[GateResult] = []

    unusable = [f for f in _load_bearing_facts(v) if not f.get("usable", True)]
    unusable += [{"name": f.fact_id, "why": (f.quarantine_reason or f.note or f.status.value)}
                 for f in _unusable_facts(v)]
    gates.append(GateResult(
        "G1_extraction", not unusable,
        "every load-bearing amount is anchored to a verified span on its page"
        if not unusable else
        "not verified: " + "; ".join(f"{f['name']} ({f['why']})" for f in unusable),
    ))

    missing_docs = [d for d in v.documents_required if d not in v.documents_present]
    gates.append(GateResult(
        "G2_document_set", not missing_docs,
        "all four documents are present" if not missing_docs
        else "absent: " + ", ".join(missing_docs) + " — only evidence-gap findings will be issued",
    ))

    policy_clause_found = bool(v.evidence_document_ids) or getattr(v.policy, "clauses", None)
    gates.append(GateResult(
        "G3_policy_support", bool(policy_clause_found),
        "the policy wording was located and its clauses indexed" if policy_clause_found
        else "no policy wording was located for the heads in dispute",
    ))

    order_ok = not (v.order_case == "C" and v.order_material)
    gates.append(GateResult(
        "G5_graph_order", order_ok,
        f"order case {v.order_case}: {v.order_reason}" if order_ok
        else f"order case C and materially split: {v.order_reason}",
    ))

    inv = v.execution.run.invariant_status
    gates.append(GateResult(
        "G6_calculation", inv == "ok",
        "every step's invariants hold (money is conserved at every node)" if inv == "ok"
        else f"invariant failure: {inv}",
    ))

    identity_ok = bool(v.reconciliation.identity_ok) if v.reconciliation else False
    gates.append(GateResult(
        "G8_provenance", identity_ok,
        "the restoration route and the recomputation route agree to the paise"
        if identity_ok else
        (v.reconciliation.identity_note if v.reconciliation
         else "no reconciliation was performed, so no amount may be asserted"),
    ))
    return tuple(gates)


# -- findings ---------------------------------------------------------------
def adjudicate(v: VerdictInputs) -> Adjudication:
    gates = run_gates(v)
    failed = [g.gate for g in gates if not g.passed]
    adj = Adjudication(policy_id=getattr(v.policy, "policy_id", "unknown"), gates=gates)

    def nid(n: int) -> str:
        return f"F{n:03d}"

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return nid(counter)

    # ---- 1. money the rules support ---------------------------------------
    if (v.unlawful_cut_gross_paise > 0 and v.insurer is not None
            and v.reconciliation is not None and v.reconciliation.identity_ok
            and v.reconciliation.restore_recompute_paise > 0):
        barred_rules = sorted({r for rs in v.barred_heads_rules.values() for r in rs})
        cut_detail = ", ".join(
            f"{head.replace('_', ' ')} {format_inr(amount)}"
            for head, amount in sorted(v.barred_heads_cut.items())
        )
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="FINANCIAL",
            state="POTENTIALLY_INCONSISTENT",
            head=HEAD_RATIO,
            why=(f"the settlement letter removed {cut_detail} by applying a proportionate "
                 f"adjustment to heads that the applicable instruments exclude from it"),
            amount_paise=v.reconciliation.restore_recompute_paise,
            amount_gross_paise=v.unlawful_cut_gross_paise,
            amount_basis=("net of the lawful steps that still apply to the restored amount; "
                          "stated at the smallest value across the readings "
                          f"({format_inr(v.reconciliation.supported_paise)})"),
            citations=tuple(
                Citation(ref_type="rule_version", ref_id=oid, title=o.title, quote=o.quote,
                         verified=True)
                for oid in barred_rules
                if (o := v.rule_evaluation.by_id(oid))
            ) + tuple(_scope_citations(v)),
            evidence_ids=tuple(_evidence_ids(v, tuple(v.barred_heads_cut))),
            slots_extra={
                "bill": tuple(_evidence_ids(v, tuple(v.barred_heads_cut), "bill")),
                "settlement": tuple(_evidence_ids(v, tuple(v.barred_heads_cut), "settlement")),
            },
            calculation_steps=tuple(s.node_id for s in v.execution.run.trace)
            + tuple(v.restoration_nodes),
            missing=(),
            limitations=(
                "this is the amount that holds under every reading of the documents; "
                "the readings are shown separately",
                "the hospital's differential-billing status is not evidenced by any "
                "document supplied, which is reported separately",
                "no admission of liability is asserted, by the insurer or by this system",
            ),
            escalation="grievance",
            escalation_reason=("a rule-sourced difference with a computed amount: raise it "
                               "with the insurer's grievance cell, quoting the instrument "
                               "paragraph as shown"),
            notes=tuple(o.statement() for o in v.rule_evaluation.applied()),
        ))

    # ---- 2. unexplained deduction lines -----------------------------------
    if v.insurer is not None and v.insurer.unexplained_paise:
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="EVIDENCE_GAP",
            state="UNDETERMINED",
            head=HEAD_UNEXPLAINED,
            code="unexplained_deduction",
            why=(f"a deduction of {format_inr(v.insurer.unexplained_paise)} carries no "
                 f"attributed head, ratio or clause, and no computation on this bill "
                 f"reproduces it"),
            citations=(),
            evidence_ids=tuple(_settlement_ids(v)),
            slots_na=("policy", "arithmetic", "regulation", "bill"),
            slots_extra={"settlement": tuple(_settlement_ids(v))},
            missing=("the insurer's basis for the deduction, in writing, item by item",),
            limitations=("the money is neither shown to be owed nor shown to be properly "
                         "withheld: the document does not say what it is for",),
            escalation="grievance",
            escalation_reason="ask for the basis of the deduction under the reasons requirement",
        ))

    # ---- 3. procedural defects --------------------------------------------
    for rule_id, evidence in _procedural_breaches(v):
        outcome = v.rule_evaluation.by_id(rule_id)
        if outcome is None:
            continue
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="PROCEDURAL",
            state="POTENTIALLY_INCONSISTENT",
            head=HEAD_PROCESS,
            why=_procedural_why(rule_id),
            citations=(Citation(ref_type="rule_version", ref_id=rule_id, title=outcome.title,
                                quote=outcome.quote, verified=True),),
            evidence_ids=(evidence,),
            slots_extra={"settlement": (evidence,)},
            slots_na=("arithmetic", "bill", "policy"),
            limitations=("no rupee value attaches to this finding: it is about how the "
                         "decision was communicated, not about the amount"),
            escalation="grievance",
            escalation_reason="ask for the specific policy terms relied on, in writing",
        ))

    # ---- 4. contested or unverified instruments ----------------------------
    #
    # Every head state the engine reports must be carried by a finding the reader can
    # see. An UNVERIFIED rule blocks its head exactly as a contested one does, so it
    # produces exactly the same kind of finding -- with the reason this one has: the
    # instrument has not been checked against its primary source. (Before the Phase-3B
    # benchmark the state was raised with nothing to explain it.)
    for outcome in v.rule_evaluation.outcomes:
        if outcome.result not in (RESULT_CONTESTED, RESULT_UNVERIFIED):
            continue
        contested = outcome.result == RESULT_CONTESTED
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="EVIDENCE_GAP",
            state="UNDETERMINED",
            head=_head_for_rule(outcome.rule_id),
            code=("contested_instrument" if contested else "unverified_instrument"),
            why=((f"rule {outcome.rule_id} is contested: the primary sources or their "
                  f"readings do not settle it, so it is not used to assert anything")
                 if contested else
                 (f"rule {outcome.rule_id} has not been verified against its primary "
                  f"source, so it is not used to assert anything")),
            citations=(Citation(ref_type="rule_version", ref_id=outcome.rule_id,
                                title=outcome.title, quote=outcome.quote, verified=False),),
            evidence_ids=(),
            slots_na=("policy", "arithmetic", "bill", "settlement"),
            missing=(("a primary instrument that settles the contested question",)
                     if contested else
                     ("a reading of the instrument's current text against the primary "
                      "source",)),
            limitations=("both readings are computed and shown; neither is chosen for the "
                         "amount claimed",) if contested else
                        ("the rule's effect on this case is therefore reported as "
                         "undetermined, not as an accusation",),
            escalation="not_worth_it",
            escalation_reason=("no action on this ground alone: it changes what a better "
                               "source would let us say, not what can be asked for today"),
        ))

    # ---- 4b. indeterminate rules that materially affect a head -------------
    for outcome in v.rule_evaluation.outcomes:
        if outcome.result != RESULT_INDETERMINATE:
            continue
        if outcome.effect not in ("PROHIBIT_APPLICATION", "REQUIRE_SCOPE_LIMIT",
                                  "REQUIRE_CONDITION", "REQUIRE_PARAMETER_PRESENT"):
            continue
        missing_facts = [_missing_request(r) for r in outcome.reasons]
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="EVIDENCE_GAP",
            state="UNDETERMINED",
            head=_head_for_rule(outcome.rule_id),
            code="indeterminate_rule",
            gates_failed=(tuple(failed) if _head_for_rule(outcome.rule_id) == HEAD_RATIO
                          else ()),
            why=(f"rule {outcome.rule_id} could not be applied because a required fact is "
                 f"missing, so it neither permits nor forbids this deduction"),
            citations=(Citation(ref_type="rule_version", ref_id=outcome.rule_id,
                                title=outcome.title, quote=outcome.quote, verified=True),),
            evidence_ids=(),
            slots_na=("policy", "arithmetic", "bill", "settlement"),
            missing=tuple(missing_facts),
            limitations=("the deduction is neither confirmed nor challenged on this ground; "
                         "the missing fact is named so it can be obtained",),
            escalation="grievance",
            escalation_reason=("ask the insurer, in writing, for the basis of the deduction "
                               "that this rule conditions"),
        ))

    # ---- 4c. the load-bearing facts the record cannot supply ----------------
    #
    # An unusable fact is a hole in the evidence, and the hole is nameable. It produces
    # an EVIDENCE_GAP on the provenance head, never a number: the amount that would
    # depend on the fact is not asserted anywhere in this adjudication.
    for fact in _unusable_facts(v):
        reason = fact.quarantine_reason or fact.note or f"status {fact.status.value}"
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="EVIDENCE_GAP",
            state="UNDETERMINED",
            head=HEAD_PROVENANCE,
            code=("quarantined_fact" if fact.status.value == "quarantined" else "missing_fact"),
            why=(f"the load-bearing fact {fact.fact_id} is {fact.status.value} "
                 f"({reason}), so any figure that depends on it is not asserted"),
            gates_failed=tuple(failed),
            citations=(),
            evidence_ids=(),
            slots_na=("policy", "regulation", "arithmetic", "bill", "settlement"),
            missing=(f"the printed value of {fact.fact_id}",),
            limitations=("no amount is put forward on this footing: the charge that turns on "
                         "this fact is neither confirmed nor challenged here",),
            escalation="grievance",
            escalation_reason=("ask the hospital or the insurer, in writing, for the omitted "
                               "figure"),
            notes=((f"origin {fact.origin.value}",) if getattr(fact, "origin", None) else ()),
        ))

    # ---- 5. the honest summary (INFO) -------------------------------------
    if v.reconciliation is not None:
        adj.findings.append(Finding(
            finding_id=next_id(),
            type="INFO",
            state="CONSISTENT",
            head="summary",
            why=(f"the recomputation supports a payable of "
                 f"{format_inr(v.reconciliation.lawful_payable_paise)} against "
                 f"{format_inr(v.reconciliation.paid_paise)} paid; the difference that holds "
                 f"under every enumerated reading is "
                 f"{format_inr(v.reconciliation.supported_paise)}"),
            citations=tuple(_scope_citations(v)),
            evidence_ids=tuple(_load_bearing_evidence_ids(v)) + tuple(_bill_evidence_ids(v)),
            calculation_steps=tuple(s.node_id for s in v.execution.run.trace),
            slots_extra={"bill": tuple(_bill_evidence_ids(v)),
                         "settlement": tuple(_settlement_ids(v))},
            slots_na=("regulation",),
            limitations=("the amounts are computed only from the facts anchored to the "
                         "spans listed above; nothing is estimated or interpolated",),
            escalation="self_service",
            notes=tuple(f"reading {k}: {format_inr(v_)}, difference "
                        f"{format_inr(v_ - v.reconciliation.paid_paise)}"
                        for k, v_ in v.readings.items()),
        ))

    # ---- 6. split displayable vs withheld, then derive head states ---------
    displayable: list[Finding] = []
    for f in adj.findings:
        if f.displayable:
            displayable.append(f)
        else:
            adj.withheld.append(f)
    adj.findings = displayable

    blocked = {_head_for_rule(o.rule_id): "UNDETERMINED"
               for o in v.rule_evaluation.outcomes
               if o.result in (RESULT_CONTESTED, RESULT_UNVERIFIED)}
    adj.head_states = adj.recompute_head_states(extra=blocked)
    return adj


def _severity(state: str) -> int:
    return {"CONSISTENT": 0, "UNDETERMINED": 1, "POTENTIALLY_INCONSISTENT": 2}.get(state, 1)


# -- helpers ----------------------------------------------------------------
def _load_bearing_facts(v: VerdictInputs) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for name in ("sum_insured", "room_rent_limit", "co_pay_percent", "deductible",
                 "icu_limit", "room_days", "room_rate", "bill_total"):
        holder = v.policy if hasattr(v.policy, name) else (v.bill if hasattr(v.bill, name) else None)
        val = getattr(holder, name, None) if holder is not None else None
        if val is None:
            continue
        usable = True
        why = ""
        evidence_id = getattr(val, "evidence_id", None)
        if isinstance(val, (int,)) and hasattr(v.bill, name):
            evidence_id = None        # bill-level numbers are anchored by their lines
        if hasattr(val, "status") and not getattr(val.status, "usable", True):
            usable, why = False, f"status {val.status.value}"
        out.append({"name": name, "usable": usable, "why": why,
                    "evidence_id": evidence_id})
    return tuple(out)


def _unusable_facts(v: VerdictInputs) -> tuple[Any, ...]:
    """Load-bearing facts the case record cannot supply: missing or quarantined.

    A fact that is not usable is never silently dropped: every amount that depends on it
    becomes an amount the system may not assert, and the fact is named in a finding so a
    person can obtain it. (Found by the Phase-3B Tier-1 benchmark, archetype A18: the
    room-rate fact was quarantined and the case produced no finding about it at all.)
    """
    store = v.facts
    if store is None:
        return ()
    ids = store.load_bearing_ids()
    # Only facts that carry a *figure* are reported here. A condition-shaped fact that
    # cannot be read is already reported by the rule layer, which names it in an
    # INDETERMINATE-rule finding; reporting it twice would pad the adjudication. A
    # missing number is different: it is the reason an amount is not asserted at all,
    # and nothing else in this adjudication would say so. (Found by the Phase-3B Tier-1
    # benchmark, archetype A18.)
    money_shaped = ("loadbearing.money", "loadbearing.ratio", "loadbearing.count",
                    "loadbearing.duration", "loadbearing.percentage")
    return tuple(sorted((f for f in store.all()
                         if f.fact_id in ids and not getattr(f, "usable", True)
                         and f.type in money_shaped),
                        key=lambda f: f.fact_id))


def _load_bearing_evidence_ids(v: VerdictInputs) -> tuple[str, ...]:
    ids = [str(f["evidence_id"]) for f in _load_bearing_facts(v) if f.get("evidence_id")]
    return tuple(dict.fromkeys(ids))


def _scope_citations(v: VerdictInputs) -> tuple[Citation, ...]:
    """The policy clauses that define the deduction's own scope, quoted verbatim."""
    clauses = getattr(v.policy, "clauses", None) or {}
    out = []
    for clause in (clauses.values() if isinstance(clauses, Mapping) else clauses):
        text = getattr(clause, "text", "") or ""
        if "associated medical" in text.lower() or "proportionate" in text.lower():
            out.append(Citation(ref_type="policy_clause",
                                ref_id=getattr(clause, "clause_id", ""),
                                title=getattr(clause, "heading", "") or "",
                                quote=text, verified=getattr(clause, "verified", True),
                                document_id=getattr(clause, "document_id", None),
                                page=getattr(clause, "page", None)))
    return tuple(out)


def _evidence_ids(v: VerdictInputs, heads: Sequence[str],
                  kind: str | None = None) -> tuple[str, ...]:
    out: list[str] = []
    for head in heads:
        slots = v.head_evidence.get(head, {})
        if kind is None:
            out.extend(slots.values())
        elif kind in slots:
            out.append(slots[kind])
    return tuple(dict.fromkeys(out))


def _bill_evidence_ids(v: VerdictInputs) -> tuple[str, ...]:
    lines = getattr(v.bill, "lines", ()) or ()
    return tuple(dict.fromkeys(l.evidence_id for l in lines if getattr(l, "evidence_id", None)))


def _settlement_ids(v: VerdictInputs) -> tuple[str, ...]:
    did = getattr(v.settlement, "document_id", None)
    return (did,) if did else ()


def _procedural_breaches(v: VerdictInputs) -> tuple[tuple[str, str], ...]:
    """Which conduct rules were breached by the settlement document, and on what evidence."""
    out: list[tuple[str, str]] = []
    outcome = v.rule_evaluation.by_id("CL.PARTIAL.REASONS")
    if outcome is not None and outcome.result == RESULT_APPLIES:
        ev = _settlement_evidence(v)
        out.append(("CL.PARTIAL.REASONS", f"{ev}:settlement"))
    ombudsman = v.rule_evaluation.by_id("CL.GRIEVANCE.OMBUDSMAN")
    if ombudsman is not None and ombudsman.result == RESULT_APPLIES:
        out.append(("CL.GRIEVANCE.OMBUDSMAN", f"{_settlement_evidence(v)}:settlement"))
    return tuple(out)


def _settlement_evidence(v: VerdictInputs) -> str:
    return getattr(v.settlement, "document_id", "settlement")


def _procedural_why(rule_id: str) -> str:
    return {
        "CL.PARTIAL.REASONS": ("the settlement letter disallowed part of the claim without "
                               "referring to the specific policy terms it relied on"),
        "CL.GRIEVANCE.OMBUDSMAN": ("the settlement letter does not give the Insurance "
                                   "Ombudsman's contact details, which the instrument "
                                   "requires in a grievance response"),
    }.get(rule_id, f"rule {rule_id} was breached by the settlement document")


def _head_for_rule(rule_id: str) -> str:
    if rule_id.startswith("PD."):
        return HEAD_RATIO
    if rule_id.startswith("CL."):
        return HEAD_PROCESS
    if rule_id.startswith("NP."):
        return "standardised_item_lists"
    return HEAD_CONSISTENCY


def _missing_request(reason: str) -> str:
    """A rule-evaluation reason turned into something a person can act on."""
    text = reason.strip()
    for pre, post in (("the fact this rule tests is not established: ", ""),
                      ("the circumstances that make this rule apply are not established: ", "")):
        if text.startswith(pre):
            text = text[len(pre):]
    parts = [p.strip() for p in text.split(";") if p.strip()]
    out: list[str] = []
    for p in parts:
        if p.endswith("is not stated in any document supplied"):
            fact = p[: -len(" is not stated in any document supplied")].strip()
            out.append(f"a statement of {fact}, which no document supplied")
        else:
            out.append(p)
    return "; ".join(out)
