"""The evaluator: deterministic, three-valued, and unable to invent confidence.

``APPLIES`` / ``NOT_APPLICABLE`` / ``INDETERMINATE`` are not enough on their own; a
rule outcome also carries **why** it could not be decided, so the report can name the
missing fact instead of shrugging. Two gates run before any logical evaluation:

    status gate          unverified in-force status (OV-1) -> cannot be applied
    verification gate    lapsed ``verified_on`` window  -> cannot be applied
    effective-date gate  not yet in force on the claim date -> not applicable
    supersession gate    superseded before the claim date -> not applicable

Only after those does the rule's own applicability predicate run. A contested rule
never returns APPLIES; it returns CONTESTED and blocks its head.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from ..logic import (
    FactProvider,
    PredicateError,
    Tri,
    collect_fact_refs,
    evaluate_predicate,
)
from .model import (
    INFORMATIONAL_EFFECTS,
    PROCEDURAL_EFFECTS,
    RulePack,
    RuleStatus,
    RuleVersion,
)

RESULT_APPLIES = "APPLIES"
RESULT_NOT_APPLICABLE = "NOT_APPLICABLE"
RESULT_INDETERMINATE = "INDETERMINATE"
RESULT_CONTESTED = "CONTESTED"
RESULT_UNVERIFIED = "UNVERIFIED"
RESULT_OUT_OF_FORCE = "OUT_OF_FORCE"
RESULT_INFORM_ONLY = "INFORM_ONLY"


# ---------------------------------------------------------------------------
@dataclass
class RuleContext:
    """What a rule may look at, expressed in the shared predicate language.

    There is exactly one predicate evaluator in this codebase (``claimcheck.logic``);
    rules do not get their own dialect. Facts come from the case's FactStore or from
    named values the pipeline sets explicitly, and a fact that is *missing* yields
    UNKNOWN — never a default.
    """

    provider: FactProvider = field(default_factory=FactProvider)
    claim_date: date | None = None
    values: dict[str, Any] = field(default_factory=dict)

    def set(self, fact_id: str, value: Any) -> None:
        self.provider.set(fact_id, value)

    def set_many(self, values: Mapping[str, Any]) -> None:
        self.provider.set_many(values)

    def get(self, fact_id: str, default: Any = None) -> Any:
        v = self.provider.view(fact_id)
        return default if not v.status.usable else v.value

    def has(self, fact_id: str) -> bool:
        return self.provider.has(fact_id)

    def check(self, predicate: Mapping[str, Any] | None) -> Tri:
        if not predicate:
            return Tri.TRUE
        return evaluate_predicate(predicate, self.provider).value


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    version: int
    title: str
    citation: str
    quote: str
    instrument_ref: str
    instrument_title: str
    kind: str
    effect: str
    status: str
    result: str
    reasons: tuple[str, ...]
    tested: date | None = None
    head: str | None = None
    human_escalation: bool = False

    @property
    def decidable(self) -> bool:
        return self.result == RESULT_APPLIES

    @property
    def blocks_head(self) -> bool:
        return self.result in (RESULT_INDETERMINATE, RESULT_CONTESTED, RESULT_UNVERIFIED)

    @property
    def whole_head_blocked(self) -> bool:
        """Contested/unverified rules block the head they govern; indeterminate ones
        usually name a missing fact instead."""
        return self.result in (RESULT_CONTESTED, RESULT_UNVERIFIED)

    def statement(self) -> str:
        return (f"{self.rule_id} ({self.instrument_ref} {self.citation}) "
                f"-> {self.result}")


@dataclass
class RuleEvaluation:
    pack_version: str
    as_of: date
    outcomes: tuple[RuleOutcome, ...]
    unverified_rules_used: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def by_id(self, rule_id: str) -> RuleOutcome | None:
        for o in self.outcomes:
            if o.rule_id == rule_id:
                return o
        return None

    def applied(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.outcomes if o.result == RESULT_APPLIES)

    def blocked(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.outcomes if o.blocks_head)

    def procedural(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.applied() if o.effect in PROCEDURAL_EFFECTS)

    def countable(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.applied() if o.effect not in INFORMATIONAL_EFFECTS)


# ---------------------------------------------------------------------------
def evaluate_rule(rule: RuleVersion, ctx: RuleContext, today: date,
                  *, claim_date: date | None = None) -> RuleOutcome:
    """One rule, once, against an explicit clock.

    ``today`` is the clock at which the *rule's status* is judged; ``claim_date`` is
    the clock at which the rule's *force* is judged. They are different questions and
    conflating them is how a rule that lapsed last year gets applied to a claim from
    the year before.
    """
    ref_date = claim_date or ctx.claim_date or today
    base = dict(
        rule_id=rule.rule_id, version=rule.version, title=rule.title,
        citation=rule.citation, quote=rule.quote,
        instrument_ref=rule.instrument.ref, instrument_title=rule.instrument.title,
        kind=rule.kind.value, effect=rule.effect, status=rule.status.value,
        tested=today, human_escalation=rule.human_escalation,
    )

    # 0. is the question live for this case at all?
    #    Applicability is asked before the status gate for one precise reason: a rule
    #    whose trigger the case does not present must be NOT_APPLICABLE, whatever its
    #    status. The status gate runs first only for rules that are *live*, so an open
    #    verification question or a contested reading flags the cases it actually
    #    touches instead of flagging every case in the system. (Found by the Phase-3B
    #    benchmark: with the status gate first, a contested instrument with no
    #    applicability facts of its own made every generated case UNDETERMINED.)
    if rule.applicability:
        live = ctx.check(rule.applicability)
        if live is Tri.FALSE:
            return RuleOutcome(result=RESULT_NOT_APPLICABLE, reasons=(
                "the rule's conditions of application are not met",
            ), **base)

    # 1. status gate (OV-1 and friends)
    if rule.status is RuleStatus.UNVERIFIED:
        return RuleOutcome(result=RESULT_UNVERIFIED, reasons=(
            "the rule was never checked against the primary source, so it cannot be applied",
        ), **base)
    if rule.status is RuleStatus.GATED:
        return RuleOutcome(result=RESULT_CONTESTED, reasons=(
            "an open verification question (OV-n) governs this instrument",
        ), **base)
    if rule.status is RuleStatus.CONTESTED:
        return RuleOutcome(result=RESULT_CONTESTED, reasons=(
            "the primary sources or their readings disagree; the instrument does not settle it",
        ), **base)

    # 2. verification freshness
    if not rule.verification_current(today):
        until = rule.valid_until()
        return RuleOutcome(result=RESULT_UNVERIFIED, reasons=(
            f"the rule's verification lapsed on {until.isoformat() if until else 'unknown'}; "
            f"re-check the instrument against the primary source",
        ), **base)

    # 3. force in time
    if rule.superseded_on is not None and ref_date >= rule.superseded_on:
        return RuleOutcome(result=RESULT_OUT_OF_FORCE, reasons=(
            f"superseded on {rule.superseded_on.isoformat()}, before the claim date "
            f"{ref_date.isoformat()}",
        ), **base)
    if ref_date < rule.effective_from:
        return RuleOutcome(result=RESULT_NOT_APPLICABLE, reasons=(
            f"in force only from {rule.effective_from.isoformat()}; the claim date is "
            f"{ref_date.isoformat()}",
        ), **base)

    # 4. applicability
    if rule.applicability:
        app = ctx.check(rule.applicability)
        if app is Tri.UNKNOWN:
            return RuleOutcome(result=RESULT_INDETERMINATE, reasons=(
                "the circumstances that make this rule apply are not established: "
                + _unknown_facts(rule.applicability, ctx),
            ), **base)
        if app is Tri.FALSE:
            return RuleOutcome(result=RESULT_NOT_APPLICABLE, reasons=(
                "the rule's conditions of application are not met",
            ), **base)

    # 5. the rule's own condition (which it asserts, rather than presupposes)
    if rule.conditions:
        for cond_pred in rule.conditions:
            cond = ctx.check(cond_pred)
            if cond is Tri.UNKNOWN:
                return RuleOutcome(result=RESULT_INDETERMINATE, reasons=(
                    "the fact this rule tests is not established: "
                    + _unknown_facts(cond_pred, ctx),
                ), **base)
            if cond is Tri.FALSE:
                return RuleOutcome(result=RESULT_NOT_APPLICABLE, reasons=(
                    "the rule applies to this case, but its condition is not breached",
                ), **base)

    if rule.effect in INFORMATIONAL_EFFECTS:
        return RuleOutcome(result=RESULT_INFORM_ONLY, reasons=(
            "shown for the record; this rule does not decide any amount",
        ), **base)
    return RuleOutcome(result=RESULT_APPLIES, reasons=(), **base)


def evaluate_pack(pack: RulePack, ctx: RuleContext, today: date,
                  *, claim_date: date | None = None) -> RuleEvaluation:
    outcomes = tuple(evaluate_rule(r, ctx, today, claim_date=claim_date) for r in pack.rules)
    used_unverified = tuple(
        o.rule_id for o in outcomes if o.status == RuleStatus.UNVERIFIED.value
        and o.result not in (RESULT_UNVERIFIED, RESULT_CONTESTED)
    )
    notes: list[str] = []
    stale = [r.rule_id for r in pack.rules
             if r.status is RuleStatus.VERIFIED and not r.verification_current(today)]
    if stale:
        notes.append(
            "verification lapsed for: " + ", ".join(sorted(stale))
            + " — these rules were not applied and are reported as such"
        )
    contested = [r.rule_id for r in pack.rules if r.status is RuleStatus.CONTESTED]
    if contested:
        notes.append("contested rules were evaluated to both readings, not to an answer: "
                     + ", ".join(sorted(contested)))
    return RuleEvaluation(pack_version=pack.version, as_of=today, outcomes=outcomes,
                          unverified_rules_used=used_unverified, notes=tuple(notes))


def _unknown_facts(predicate: Mapping[str, Any], ctx: RuleContext) -> str:
    """Name the facts that are missing, rather than saying 'unknown'."""
    refs = [f for f in collect_fact_refs(predicate) if not ctx.has(f)]
    named = [f"{f} is not stated in any document supplied" for f in refs]
    return "; ".join(dict.fromkeys(named)) or "a condition of the rule is not established"


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScopeCheck:
    """Which heads a proportionate deduction had *no lawful scope* over, and why.

    Two independent reasons, kept apart because they have different remedies:

    ``outside_definition``  the policy's own definition of associated medical expenses
                            does not include the head, so the ratio had nothing to bite
                            on (rule PD.SCOPE.NO_EXTRA).
    ``rule_barred``         the head is excluded from the definition by instrument, or
                            the ratio may not touch it at all (PD.AME.EXCLUSIONS,
                            PD.ICU.BAR).
    """

    applied: tuple[str, ...]
    defined: tuple[str, ...]
    outside_definition: tuple[str, ...]
    rule_barred: tuple[str, ...]
    allowed: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]

    @property
    def barred(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.outside_definition + self.rule_barred))

    def is_barred(self, head: str) -> bool:
        return head in self.barred

    def rules_for(self, head: str) -> tuple[str, ...]:
        return self.reasons.get(head, ())

    @property
    def indeterminate(self) -> bool:
        """True when the policy defines no associated medical expenses at all: then the
        ratio has no lawful scope anywhere, and the whole deduction is undetermined
        (rule PD.AME.DEFINE) — reported, never assumed away."""
        return not self.defined


def check_ratio_scope(*, applied_heads: Sequence[str], policy_defined_heads: Sequence[str],
                      rule_barred_heads: Sequence[str]) -> ScopeCheck:
    applied = tuple(dict.fromkeys(applied_heads))
    defined = tuple(dict.fromkeys(policy_defined_heads))
    rule_barred = tuple(dict.fromkeys(rule_barred_heads))

    outside: list[str] = []
    barred: list[str] = []
    reasons: dict[str, list[str]] = {}
    for head in applied:
        if head in rule_barred:
            barred.append(head)
            reasons.setdefault(head, []).append(
                "PD.ICU.BAR" if head == "icu" else "PD.AME.EXCLUSIONS")
        if defined and head not in defined:
            outside.append(head)
            reasons.setdefault(head, []).append("PD.SCOPE.NO_EXTRA")
        elif not defined:
            reasons.setdefault(head, []).append("PD.AME.DEFINE")
    allowed = tuple(h for h in applied if h not in set(outside) | set(barred))
    return ScopeCheck(
        applied=applied, defined=defined,
        outside_definition=tuple(outside), rule_barred=tuple(barred),
        allowed=allowed,
        reasons={k: tuple(dict.fromkeys(v)) for k, v in reasons.items()},
    )


def rule_rows(pack: RulePack, as_of: date) -> tuple[dict[str, Any], ...]:
    """A flat, printable view of the pack — used by docs and the rules page."""
    rows = []
    for r in pack.rules:
        rows.append({
            "rule_id": r.rule_id,
            "version": r.version,
            "kind": r.kind.value,
            "effect": r.effect,
            "status": r.status.value,
            "instrument": r.instrument.ref,
            "citation": r.citation,
            "effective_from": r.effective_from.isoformat(),
            "superseded_on": r.superseded_on.isoformat() if r.superseded_on else "",
            "verified_on": r.verified_on.isoformat() if r.verified_on else "",
            "valid_until": (r.valid_until().isoformat() if r.valid_until() else ""),
            "assertable_today": r.assertable(as_of),
            "tests": "; ".join(r.tests),
        })
    return tuple(rows)


def contested_rules(pack: RulePack) -> tuple[RuleVersion, ...]:
    return tuple(r for r in pack.rules if r.status is RuleStatus.CONTESTED)


def unverified_rules(pack: RulePack) -> tuple[RuleVersion, ...]:
    return tuple(r for r in pack.rules
                 if r.status in (RuleStatus.UNVERIFIED, RuleStatus.GATED))
