"""Findings and the six-slot evidence bundle.

A finding is a *claim about someone's money*. Before it may be shown it must be able to
fill six slots — and the rule is enforced in code, not in a style guide:

    1. policy        the clause or term it rests on                    (a citation)
    2. regulation    the instrument and paragraph, if one is relied on (a citation)
    3. bill          the itemised charge it concerns                   (an evidence id)
    4. settlement    the deduction it answers                          (an evidence id)
    5. arithmetic    the ordered steps that produced the amount        (node ids)
    6. limitations   what the finding does not show                    (prose)

A finding whose bundle is incomplete is **withheld**: it goes to
``Adjudication.withheld`` and is never rendered with a soft caveat, because a caveat
attached to a number is a number the reader will believe.

Levels, not verdicts: each finding carries the gate level it reached —
``CONSISTENT`` (nothing to answer), ``POTENTIALLY_INCONSISTENT`` (a difference the
documents support, for the insurer to answer) and ``UNDETERMINED`` (the documents do
not settle it). The system never says "the insurer broke the law".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence

State = Literal["CONSISTENT", "POTENTIALLY_INCONSISTENT", "UNDETERMINED"]
FindingType = Literal["FINANCIAL", "PROCEDURAL", "EVIDENCE_GAP", "INFO"]

SLOTS = ("policy", "regulation", "bill", "settlement", "arithmetic", "limitations")

STATE_SEVERITY = {"CONSISTENT": 0, "UNDETERMINED": 1, "POTENTIALLY_INCONSISTENT": 2}


@dataclass(frozen=True)
class Citation:
    """A reference that a reader can check. ``verified`` is not decoration."""

    ref_type: Literal["policy_clause", "rule_version", "document", "item_entry"]
    ref_id: str
    title: str = ""
    quote: str = ""
    verified: bool = False
    document_id: str | None = None
    page: int | None = None

    def as_line(self) -> str:
        tag = self.title or self.ref_id
        mark = "" if self.verified else " [not verified for use as a rule]"
        return f"{tag} ({self.ref_id}){mark}"


@dataclass(frozen=True)
class Finding:
    finding_id: str
    type: FindingType
    state: State
    head: str
    why: str
    gates_failed: tuple[str, ...] = ()
    amount_paise: int | None = None
    amount_basis: str | None = None
    amount_gross_paise: int | None = None     # before downstream steps, when different
    citations: tuple[Citation, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    calculation_steps: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    escalation: Literal["self_service", "grievance", "ombudsman", "not_worth_it"] = "self_service"
    escalation_reason: str = ""
    notes: tuple[str, ...] = ()
    slots_na: tuple[str, ...] = ()
    slots_extra: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    code: str | None = None                   # stable machine code, for the benchmark

    def __post_init__(self) -> None:
        # A str is a sequence of one item here, never a sequence of characters.
        for name in ("gates_failed", "citations", "evidence_ids", "calculation_steps",
                     "missing", "limitations", "notes", "slots_na"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, (value,) if value else ())
        extra = self.slots_extra
        if isinstance(extra, str):
            object.__setattr__(self, "slots_extra", {"policy": (extra,)})
        elif isinstance(extra, Mapping):
            fixed = {k: ((v,) if isinstance(v, str) else tuple(v or ()))
                     for k, v in extra.items()}
            object.__setattr__(self, "slots_extra", fixed)

    # -- the six-slot bundle ------------------------------------------------
    def slots(self) -> dict[str, tuple[str, ...]]:
        """Which slots are filled, and with what.

        ``slots_na`` marks a slot that *cannot* apply to this finding type, with a
        reason. "Not applicable" is a statement; a blank slot is not.
        """
        has_policy_quote = any(c.ref_type in ("policy_clause", "document") and c.quote
                               for c in self.citations)
        has_reg = any(c.ref_type == "rule_version" for c in self.citations)
        settled = tuple(e for e in self.evidence_ids if e.endswith(":settlement"))
        billed = tuple(e for e in self.evidence_ids if e.endswith(":bill"))
        filled = {
            "policy": (tuple(c.ref_id for c in self.citations if c.ref_type == "policy_clause")
                       or tuple(c.ref_id for c in self.citations if c.ref_type == "document")),
            "regulation": tuple(c.ref_id for c in self.citations if c.ref_type == "rule_version"),
            "bill": billed,
            "settlement": settled,
            "arithmetic": self.calculation_steps,
            "limitations": self.limitations,
        }
        if has_policy_quote and not filled["policy"]:
            filled["policy"] = ("policy wording",)
        if has_reg and not filled["regulation"]:
            filled["regulation"] = ("instrument",)
        for slot in self.slots_na:
            if slot in filled and not filled[slot]:
                filled[slot] = (f"not applicable to a {self.type} finding",)
        for slot, values in (self.slots_extra or {}).items():
            if values:
                filled[slot] = tuple(values)
        return filled

    def missing_slots(self) -> tuple[str, ...]:
        s = self.slots()
        return tuple(k for k in SLOTS if not s[k])

    @property
    def bundle_complete(self) -> bool:
        return not self.missing_slots()

    @property
    def displayable(self) -> bool:
        """A money finding without arithmetic, or any finding with a hole, is withheld."""
        if not self.bundle_complete:
            return False
        if self.amount_paise is not None and not self.calculation_steps:
            return False
        return True


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str
    blocks_money_findings: bool = True


@dataclass
class Adjudication:
    policy_id: str
    gates: tuple[GateResult, ...]
    findings: list[Finding] = field(default_factory=list)
    withheld: list[Finding] = field(default_factory=list)
    head_states: dict[str, State] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    # -- state derivation ----------------------------------------------------
    def recompute_head_states(self, extra: Mapping[str, State] | None = None) -> dict[str, State]:
        """A head's state is the worst state among the findings against it.

        ``extra`` carries heads that no finding mentions but that a blocked rule or an
        open question already decided (contested instruments, unverified lists).
        Findings still sitting in ``withheld`` count: the reader does not see them,
        but their head is not thereby settled.
        """
        states: dict[str, State] = {}
        for f in list(self.findings) + list(self.withheld):
            cur = states.get(f.head)
            if cur is None or STATE_SEVERITY[f.state] > STATE_SEVERITY[cur]:
                states[f.head] = f.state
        for head, state in (extra or {}).items():
            states.setdefault(head, state)
        if not states:
            states["internally_consistent"] = "CONSISTENT"
        return states

    # -- queries -------------------------------------------------------------
    @property
    def gates_passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def failed_gates(self, *, blocking_only: bool = False) -> tuple[GateResult, ...]:
        return tuple(g for g in self.gates
                     if not g.passed and (g.blocks_money_findings or not blocking_only))

    def state_of(self, head: str) -> State | None:
        return self.head_states.get(head)

    def worst_state(self) -> State:
        if not self.head_states:
            return "UNDETERMINED"
        return max(self.head_states.values(), key=lambda s: STATE_SEVERITY[s])

    def amount_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.amount_paise)

    def procedural_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.type == "PROCEDURAL")

    def evidence_gaps(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.type == "EVIDENCE_GAP")

    def supported_total_paise(self) -> int:
        return sum(f.amount_paise or 0 for f in self.findings if f.type == "FINANCIAL")
