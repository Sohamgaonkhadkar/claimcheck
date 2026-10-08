"""The report: every number carries its source, every source is quoted verbatim.

Two renderings of one structure:

    build_report(case_report) -> dict     JSON-serialisable, the API contract
    render_markdown(report)  -> str       the human document

The money-flow sheet is the centre of the report. Where a claims document gives one
net figure, this gives the ordered steps, each naming its base, its source and the
clause or rule it came from — so a reader can disagree with one step instead of the
whole number.

Nothing here computes money. It renders what the calculator already decided.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..calc.money import format_inr
from ..verdict.findings import Adjudication, Finding
from ..pipeline import CaseRun

STATE_ORDER = {"POTENTIALLY_INCONSISTENT": 3, "UNDETERMINED": 2, "CONSISTENT": 1}


def overall_state(adj: Adjudication) -> str:
    """Worst head state wins, and an empty set is never reported as 'consistent'."""
    if not adj.head_states:
        return "UNDETERMINED"
    states = list(adj.head_states.values())
    return max(states, key=lambda s: STATE_ORDER.get(s, 0))


# ---------------------------------------------------------------------------
def build_report(run: CaseRun) -> dict[str, Any]:
    """The report as data. Every field is either a computed number or a citation."""
    case = run.case
    rev = run.reconciliation
    insurer = run.insurer
    adj = run.adjudication
    exec_ = run.execution
    return {
        "case": _case(run),
        "verdict": {
            "state": adj.worst_state(),
            "head_states": dict(adj.head_states),
            "supported_difference_paise": rev.supported_paise,
            "supported_difference_display": format_inr(rev.supported_paise),
            "gates": [{"gate": g.gate, "passed": g.passed, "detail": g.detail}
                      for g in adj.gates],
            "notes": list(run.notes),
        },
        "money_flow": {
            "graph_id": run.graph.graph_id,
            "order": {
                "case": run.ambiguity.case,
                "reason": run.ambiguity.reason,
                "selected_reading": run.ambiguity.selected_reading,
                "material": run.ambiguity.material,
                "readings_material": run.ambiguity.readings_material,
                "spread_paise": run.ambiguity.spread_paise,
            },
            "gross_bill_paise": exec_.gross_paise,
            "lawful_payable_paise": exec_.payable_paise,   # method B, this policy's own steps
            "lawful_payable_display": format_inr(exec_.payable_paise),
            "payable_paise": exec_.payable_paise,
            "payable_display": format_inr(exec_.payable_paise),
            "reductions_paise": exec_.reductions_paise,
            "patient_share_paise": exec_.patient_share_paise,
            "steps": [_step(s) for s in exec_.run.trace],
            "invariants": exec_.run.invariant_status,
            "flags": list(exec_.flags),
            "assumptions": list(exec_.assumptions),
        },
        "insurer_model": {
            "claimed_paise": insurer.claimed_paise,
            "claimed_display": format_inr(insurer.claimed_paise),
            "net_payable_paise": insurer.net_payable_paise,
            "net_payable_display": format_inr(insurer.net_payable_paise),
            "implied_base_paise": insurer.implied_base_paise,
            "implied_scope": list(insurer.implied_scope),
            "ratio_applied": (str(insurer.ratio_cut.applied_fraction)
                              if insurer.ratio_cut else None),
            "cut_fraction": (str(insurer.ratio_cut.cut_fraction)
                             if insurer.ratio_cut else None),
            "residual_paise": insurer.residual_paise,
            "unexplained_paise": insurer.unexplained_paise,
            "barred_cut_paise": insurer.barred_cut_paise,
            "head_cuts": [
                {"head": h.head, "cut_paise": h.cut_paise, "cut_display": format_inr(h.cut_paise),
                 "attributed_by": h.attributed_by, "barred_by_rule": h.barred_by_rule,
                 "bill_amount_paise": h.bill_amount_paise,
                 "ratio_applied": str(h.ratio_applied) if h.ratio_applied else None}
                for h in insurer.head_cuts
            ],
            "notes": list(insurer.notes),
        },
        "reconciliation": {
            "lawful_payable_paise": rev.lawful_payable_paise,
            "lawful_payable_display": format_inr(rev.lawful_payable_paise),
            "paid_paise": rev.paid_paise,
            "paid_display": format_inr(rev.paid_paise),
            "difference_paise": rev.difference_paise,
            "difference_display": format_inr(rev.difference_paise),
            "supported_difference_paise": rev.supported_paise,
            "supported_difference_display": format_inr(rev.supported_paise),
            "supported_paise": rev.supported_paise,          # alias, same value
            "supported_display": format_inr(rev.supported_paise),
            "unexplained_paise": rev.unexplained_paise,
            "unexplained_display": format_inr(rev.unexplained_paise),
            "restore_recompute_paise": rev.restore_recompute_paise,
            "restore_recompute_display": format_inr(rev.restore_recompute_paise),
            "identity_ok": rev.identity_ok,
            "identity_note": rev.identity_note,
            "readings": [{"reading_id": k, "payable_paise": v, "payable_display": format_inr(v),
                          "difference_paise": v - rev.paid_paise}
                         for k, v in rev.readings],
            "summary": rev.summary(),
            "notes": list(rev.notes),
        },
        "rules": [
            {"rule_id": o.rule_id, "version": o.version, "title": o.title, "kind": o.kind,
             "effect": o.effect, "status": o.status, "result": o.result,
             "instrument_ref": o.instrument_ref, "citation": o.citation, "quote": o.quote,
             "reasons": list(o.reasons), "human_escalation": o.human_escalation}
            for o in run.rules.outcomes
        ],
        "findings": [_finding(f) for f in adj.findings],
        "withheld": [
            {"finding_id": f.finding_id, "type": f.type, "head": f.head,
             "missing_from_bundle": list(f.missing_slots())}
            for f in adj.withheld
        ],
        "provenance": {
            "case_id": run.case.case_id,
            "origin": run.case.origin,
            "as_of": run.case.as_of.isoformat() if run.case.as_of else None,
            "rulepack_version": run.rules.pack_version,
            "graph_id": run.graph.graph_id,
            "corpus_snapshot_id": run.graph.corpus_snapshot_id,
            "documents": dict(run.case.documents),
            "arithmetic": "integer paise throughout; ratios as exact fractions; a single "
                          "rounding event at the end where rounding is unavoidable",
            "no_model": "no language model was used to produce any part of this report",
        },
    }


# ---------------------------------------------------------------------------
def render_markdown(run: CaseRun) -> str:
    """The human report. Nothing here computes a number; it only places them."""
    r = build_report(run)
    L: list[str] = []
    add = L.append

    add(f"# ClaimCheck report — case {r['case']['case_id']}")
    add("")
    if r["case"]["origin"] == "synthetic":
        add("*Synthetic case.* No real insurer, policy, hospital or patient is represented. "
            "This is evidence that the system computes what it says it computes — it is "
            "not evidence about any real insurer's behaviour.")
        add("")

    v = r["verdict"]
    add("## 1. Verdict")
    add("")
    add(f"**{v['state'].replace('_', ' ')}**")
    add("")
    rec = r["reconciliation"]
    add(f"The difference that holds under every reading of the documents is "
        f"**{rec['supported_display']}**: the lawful payable is "
        f"{rec['lawful_payable_display']}, the insurer paid {rec['paid_display']} "
        f"(total difference {rec['difference_display']}, of which {rec['supported_display']} "
        f"is supported by a rule and {rec['unexplained_display']} is unexplained).")
    add("")
    add("| head | state |")
    add("|---|---|")
    for head, state in v["head_states"].items():
        add(f"| {head.replace('_', ' ')} | {state.replace('_', ' ')} |")
    add("")

    c = r["case"]
    add("## 2. Case")
    add("")
    add(f"- Policy: {c['insurer']} — {c['product']} (UIN {c['uin'] or 'not stated'})")
    add(f"- Period: {c['inception_date']} onwards; claim date {c['claim_date']}")
    add(f"- Sum insured {c['sum_insured_display']}; room rent limit "
        f"{c['room_rent_limit_display'] or 'none stated'}; co-payment {c['co_pay_display'] or 'none'}")
    add(f"- Bill: {c['hospital']}, {c['admission_date']} to {c['discharge_date']}, "
        f"{c['room_days']} room days at {c['room_rate_display']} per day, "
        f"total {c['bill_total_display']}")
    add(f"- Claimed {c['claimed_display']}; paid {c['paid_display']} on {c['decision_date']}")
    add("")

    im = r["insurer_model"]
    add("## 3. What the settlement letter did, reconstructed")
    add("")
    add(f"The letter's own figures imply a proportionate deduction at "
        f"**{im['cut_fraction']}** of a base of "
        f"{format_inr(im['implied_base_paise']) if im['implied_base_paise'] else 'unknown'}"
        + (f", being: {', '.join(h.replace('_', ' ') for h in im['implied_scope'])}"
           if im["implied_scope"] else "") + ".")
    add("")
    add("| deduction head | amount | how attributed | barred by a rule? |")
    add("|---|---|---|---|")
    for h in im["head_cuts"]:
        add(f"| {h['head'].replace('_', ' ')} | {h['cut_display']} | {h['attributed_by']} | "
            f"{'yes' if h['barred_by_rule'] else 'no'} |")
    if im["unexplained_paise"]:
        add(f"| unexplained | {format_inr(im['unexplained_paise'])} | no head, ratio or clause "
            f"given | — |")
    add("")
    add(f"- The letter's arithmetic {'closes' if im['residual_paise'] == 0 else 'does NOT close'} "
        f"(residual {format_inr(im['residual_paise'])})")
    for n in im["notes"]:
        add(f"- {n}")
    add("")

    mf = r["money_flow"]
    add("## 4. Money-flow sheet — this policy's own steps, in order")
    add("")
    add(f"Graph `{mf['graph_id']}`. {mf['order']['reason']} "
        f"(order case **{mf['order']['case']}**.)")
    add("")
    add("| # | step | base | output | what it does |")
    add("|---|---|---|---|---|")
    for step in mf["steps"]:
        add(f"| {step['sequence']} | {step['label']} | {step['base_label']} | "
            f"{step['output_display']} | {step['effect']} |")
    add("")
    add(f"- Gross bill {format_inr(mf['gross_bill_paise'])} = payable "
        f"{mf['payable_display']} + reductions {format_inr(mf['reductions_paise'])} + "
        f"patient share {format_inr(mf['patient_share_paise'])}")
    add(f"- Calculator invariants: {mf['invariants']}")
    add("")
    for step in mf["steps"]:
        if step["source_note"] or step["parameter_sources"]:
            bits = [step["source_note"]] if step["source_note"] else []
            if step["parameter_sources"]:
                bits.append("parameters from " + ", ".join(
                    f"{k} ({val})" for k, val in step["parameter_sources"].items()))
            add(f"- `{step['node_id']}` — " + " ".join(bits))
    add("")

    if len(rec["readings"]) > 1:
        add("### Readings of the contested question")
        add("")
        add("| reading | lawful payable | difference from what was paid |")
        add("|---|---|---|")
        for rd in rec["readings"]:
            add(f"| {rd['reading_id']} | {rd['payable_display']} | "
                f"{format_inr(rd['difference_paise'])} |")
        add("")
        add("The difference put forward is the **smallest** of these, never the largest: a "
            "reading the sources do not support is a way to lose the argument, not to win it.")
        add("")

    add("## 5. Findings")
    add("")
    for f in r["findings"]:
        add(f"### {f['finding_id']} — {f['type']} — {f['state'].replace('_', ' ')}")
        add("")
        add(f"**{f['head'].replace('_', ' ')}** — {f['why']}")
        if f["amount_display"]:
            add("")
            add(f"Amount: **{f['amount_display']}**"
                + (f" (before the steps that still apply to the restored amount: "
                   f"{f['amount_gross_display']})" if f["amount_gross_display"] else "")
                + (f". {f['amount_basis']}" if f["amount_basis"] else ""))
        add("")
        if f["citations"]:
            add("- Citations:")
            for cit in f["citations"]:
                mark = "" if cit["verified"] else " *(not verified for use as a rule)*"
                quote = f'\n  > "{cit["quote"]}"' if cit["quote"] else ""
                add(f"  - {cit['title'] or cit['ref_id']} `{cit['ref_id']}`{mark}{quote}")
        if f["evidence"]:
            add(f"- Evidence: {', '.join(f['evidence'])}")
        add(f"- Steps: {', '.join(f['steps']) if f['steps'] else 'no arithmetic (none is claimed)'}")
        if f["notes"]:
            add("- Notes:")
            for n in f["notes"]:
                add(f"  - {n}")
        if f["missing"]:
            add("- Still missing:")
            for m in f["missing"]:
                add(f"  - {m}")
        if f["limitations"]:
            add("- What this does not show:")
            for lim in f["limitations"]:
                add(f"  - {lim}")
        add(f"- Next step: {f['escalation']}"
            + (f" — {f['escalation_reason']}" if f["escalation_reason"] else ""))
        add("")

    if r["withheld"]:
        add("### Findings withheld, and why")
        add("")
        for w in r["withheld"]:
            add(f"- `{w['finding_id']}` ({w['type']}, {w['head']}): its evidence bundle is "
                f"missing {', '.join(w['missing_from_bundle'])}. It is listed here rather than "
                f"shown with a caveat, because a caveat attached to a number is a number "
                f"the reader will believe.")
        add("")

    add("## 6. Rules evaluated")
    add("")
    add("| rule | result | instrument | citation | what it says |")
    add("|---|---|---|---|---|")
    for o in r["rules"]:
        add(f"| `{o['rule_id']}` | {o['result']} | {o['instrument_ref']} | {o['citation']} | "
            f"{o['title']} |")
    add("")
    add("Every rule above is quoted verbatim from the instrument it names; the paraphrase "
        "is only a label. Where a quote is empty, the rule is a *contested* or *unverified* "
        "position and is not used to decide anything.")
    add("")

    add("## 7. Gates")
    add("")
    for g in r["verdict"]["gates"]:
        add(f"- {'PASS' if g['passed'] else 'FAIL'} — `{g['gate']}`: {g['detail']}")
    add("")

    prov = r["provenance"]
    add("## 8. Provenance")
    add("")
    for k in ("case_id", "origin", "as_of", "rulepack_version", "graph_id",
              "corpus_snapshot_id", "arithmetic", "no_model"):
        add(f"- {k}: {prov[k]}")
    add("- documents: " + ", ".join(f"{role} = {doc}" for role, doc in prov["documents"].items()))
    add("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
def _case(run: CaseRun) -> dict[str, Any]:
    p, b, s = run.case.policy, run.case.bill, run.case.settlement
    return {
        "case_id": run.case.case_id,
        "origin": run.case.origin,
        "insurer": p.insurer,
        "product": p.product_name,
        "uin": p.uin,
        "policy_era": p.policy_era,
        "inception_date": _d(p.inception_date),
        "expiry_date": _d(getattr(p, "expiry_date", None)),
        "claim_date": _d(p.claim_date),
        "sum_insured_display": _money(p.sum_insured),
        "room_rent_limit_display": _money(p.room_rent_limit),
        "icu_limit_display": _money(p.icu_limit),
        "co_pay_display": (f"{p.co_pay_percent.value}%"
                           if p.co_pay_percent and p.co_pay_percent.value is not None else None),
        "deductible_display": _money(p.deductible),
        "hospital": b.hospital_name,
        "admission_date": _d(b.admission_date),
        "discharge_date": _d(b.discharge_date),
        "room_days": b.room_days,
        "room_rate_display": format_inr(b.room_rate) if b.room_rate else None,
        "claimed_display": format_inr(s.claimed_amount),
        "paid_display": format_inr(s.final_payable),
        "decision_date": _d(s.decision_date),
        "bill_total_display": format_inr(b.bill_total),
        "documents": dict(run.case.documents),
    }


_EFFECTS = {
    "NON_PAYABLE_DEDUCTION": "removes the part of this head the policy does not pay for",
    "ROOM_RENT_LIMIT": "keeps the room charge within the stated limit",
    "ICU_LIMIT": "keeps the ICU charge within the stated limit",
    "PROPORTIONATE_ADJUSTMENT": "reduces the associated medical expenses in the stated ratio",
    "COPAY": "withholds the stated share as the patient's",
    "DEDUCTIBLE": "withholds the stated amount as the patient's",
    "SUBLIMIT_CAP": "caps this head at the stated sub-limit",
    "PACKAGE_CAP": "caps this head at the package rate",
    "SUM_INSURED_CEILING": "caps the total at the sum insured",
    "PROPORTIONATE_CLAWBACK": "adds back a deduction no rule permits",
    "RECOMPUTE_DOWNSTREAM": "re-applies a downstream step to the restored amount",
}


def _step(step) -> dict[str, Any]:
    inputs = dict(getattr(step, "inputs_named", {}) or {})
    params = dict(getattr(step, "params", {}) or {})
    sources = dict(getattr(step, "param_sources", {}) or {})
    base = str(inputs.pop("base", "running"))
    return {
        "sequence": step.sequence,
        "node_id": step.node_id,
        "step_type": step.step_type,
        "label": step.label or step.step_type.replace("_", " ").title(),
        "base_label": _human_base(base),
        "base_paise": inputs.get("base_paise"),
        "base_display": (format_inr(inputs["base_paise"]) if isinstance(inputs.get("base_paise"), int)
                         else None),
        "parameters": {k: str(v) for k, v in params.items()},
        "parameter_sources": {k: str(v) for k, v in sources.items()},
        "output_paise": step.output_paise,
        "output_display": format_inr(step.output_paise),
        "operation": step.operation,
        "effect": _EFFECTS.get(step.step_type, ""),
        "source_note": step.note or "",
        "rounding": (None if step.rounding is None else
                     {"from": step.rounding.rounded_from, "to": step.rounding.rounded_to,
                      "policy": step.rounding.policy}),
    }


def _human_base(base: str) -> str:
    """The trace names its base in words; keep them, they are the honest description."""
    if base == "running":
        return "the running amount"
    if base == "gross_bill":
        return "the gross bill"
    if base.startswith("head_set{"):
        heads = base[len("head_set{"):-1].split(", ")
        return "the " + " + ".join(h.replace("_", " ") for h in heads) + " heads"
    if base.startswith("head{"):
        return "the " + base[5:-1].replace("_", " ") + " head"
    if base.startswith("node"):
        return "the previous step's result"
    return base.replace("_", " ")


def _money(fact) -> str | None:
    """Money facts hold integer paise (or a Fraction); render for display only."""
    if fact is None or getattr(fact, "value", None) is None:
        return None
    v = fact.value
    if getattr(fact, "type", "") == "loadbearing.ratio":
        return f"{v}%"
    if getattr(fact, "type", "") == "loadbearing.bool":
        return str(v)
    return format_inr(int(v))


def _d(value) -> str | None:
    return value.isoformat() if value is not None else None


def _finding(f: Finding) -> dict[str, Any]:
    return {
        "finding_id": f.finding_id,
        "type": f.type,
        "state": f.state,
        "head": f.head,
        "why": f.why,
        "amount_paise": f.amount_paise,
        "amount_display": format_inr(f.amount_paise) if f.amount_paise is not None else None,
        "amount_gross_paise": f.amount_gross_paise,
        "amount_gross_display": (
            format_inr(f.amount_gross_paise) if f.amount_gross_paise is not None else None
        ),
        "amount_basis": f.amount_basis,
        "citations": [
            {"ref_type": c.ref_type, "ref_id": c.ref_id, "title": c.title,
             "quote": c.quote, "verified": c.verified, "document_id": c.document_id,
             "page": c.page}
            for c in f.citations
        ],
        "evidence": list(f.evidence_ids),
        "steps": list(f.calculation_steps),
        "missing": list(f.missing),
        "limitations": list(f.limitations),
        "escalation": f.escalation,
        "escalation_reason": f.escalation_reason,
        "notes": list(f.notes),
        "gates_failed": list(f.gates_failed),
        "bundle_complete": f.bundle_complete,
    }


def _provenance(rep: CaseReport) -> dict[str, Any]:
    g = rep.graph
    return {
        "corpus_snapshot_id": getattr(g, "corpus_snapshot_id", None) or "not-yet-ingested",
        "rulepack_version": getattr(g, "rulepack_version", None) or "unknown",
        "graph_version": getattr(g, "graph_version", None) or "unversioned",
        "engine": "claimcheck deterministic trust core",
        "arithmetic": "integer paise; ratios as exact fractions; one rounding event at the end",
        "verdict_is": "a computed statement about the documents supplied, not a legal opinion",
        "withheld_findings": len(rep.adjudication.withheld),
    }


def render_letter(run: CaseRun) -> str:
    """A draft grievance letter. Only complete, amount-bearing findings appear.

    The letter quotes the instrument verbatim and asks for the missing items by name.
    It is assembled from the report's own fields — it invents nothing.
    """
    r = build_report(run)
    L: list[str] = []
    add = L.append
    money_findings = [f for f in r["findings"]
                      if f["type"] == "FINANCIAL" and f["amount_paise"]]
    missing_items: list[str] = []
    for f in r["findings"]:
        missing_items.extend(f["missing"])

    add(f"To: The Grievance Redressal Officer, {r['case']['insurer']}")
    add("")
    add(f"Subject: Claim {r['case']['case_id']} under policy {r['case']['uin'] or 'as stated'} — "
        f"request for review of deductions")
    add("")
    add(f"Policy in force from {r['case']['inception_date']}; hospitalisation "
        f"{r['case']['admission_date']} to {r['case']['discharge_date']} at "
        f"{r['case']['hospital']}; claim of {r['case']['claimed_display']} settled at "
        f"{r['case']['paid_display']} on {r['case']['decision_date']}.")
    add("")
    add("The deductions are disputed to the extent set out below. Each amount has been recomputed "
        "step by step against the policy's own wording and the applicable instruments.")
    add("")
    for f in money_findings:
        add(f"1. {f['head'].replace('_', ' ')} — {f['amount_display']}")
        add(f"   {f['why']}")
        for c in f["citations"]:
            if c["ref_type"] == "rule_version":
                add(f"   {c['title']} — {c['ref_id']}")
                if c["quote"]:
                    add(f"   \"{c['quote']}\"")
        if f["amount_basis"]:
            add(f"   Basis: {f['amount_basis']}")
        add("")
    if missing_items:
        add("2. Documents or reasons requested")
        for m in dict.fromkeys(missing_items):
            add(f"   - {m}")
        add("")
    add("3. Relief sought")
    total = sum(f["amount_paise"] for f in money_findings)
    add(f"   Payment of the difference of {format_inr(total)}, being the amount that holds under "
        f"every reading of the documents, together with the interest the applicable instrument "
        f"provides for delay.")
    add("")
    add("If this is not resolved, the matter will be taken to the Insurance Ombudsman having "
        "jurisdiction, having first allowed the fifteen days the grievance process is allowed.")
    add("")
    add("Enclosures: settlement letter; itemised bill; policy wording; this computation sheet.")
    add("")
    return "\n".join(L)


def summarise_findings(findings: Iterable[Finding]) -> str:
    return "\n".join(f"{f.finding_id} {f.type} {f.state} {f.head}" for f in findings)
