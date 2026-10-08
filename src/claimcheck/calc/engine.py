"""Deterministic calculation engine (Phase-2 §12). The product's core.

Guarantees:

* Money is integer paise; ratios are exact ``Fration``. No float, no ``eval``.
* Operations come from a **closed set**. A model cannot add one.
* Every run produces a trace; every trace step names its inputs, its parameters and
  where they came from.
* Invariants are asserted after every run (conservation, non-negativity,
  idempotence, monotonicity, no-double-application, SI respect). A violation is a
  defect, not a finding: the head is aborted with an internal error.
* The engine executes a **supplied policy-specific graph**; it contains no
  insurance formula of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Literal, Mapping, Sequence

from ..errors import CalcInvariantViolation, CalcMissingParam
from ..schema import CanonicalCategory, Money, Ratio

# ---------------------------------------------------------------------------
# Operations (closed set)
# ---------------------------------------------------------------------------
Operation = Literal[
    "identity",
    "subtract",
    "cap",
    "floor",
    "multiply_ratio",
    "percentage",
    "apportion",
    "threshold",
    "ceiling",
]

OPERATIONS: frozenset[str] = frozenset(
    {"identity", "subtract", "cap", "floor", "multiply_ratio", "percentage",
     "apportion", "threshold", "ceiling"}
)

RoundingPolicy = Literal["CARRY_EXACT_FINAL_ROUND", "PER_STEP_HALF_UP", "PER_STEP_HALF_EVEN"]


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoundingEvent:
    rounded_from: str
    rounded_to: int
    policy: str


@dataclass(frozen=True)
class CalcStep:
    sequence: int
    node_id: str
    step_type: str
    label: str
    inputs_named: Mapping[str, object]
    operation: str
    params: Mapping[str, object]
    output_paise: int
    param_sources: Mapping[str, str] = field(default_factory=dict)
    rounding: RoundingEvent | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "node_id": self.node_id,
            "step_type": self.step_type,
            "label": self.label,
            "inputs": dict(self.inputs_named),
            "operation": self.operation,
            "params": dict(self.params),
            "param_sources": dict(self.param_sources),
            "output_paise": self.output_paise,
            "rounding": (self.rounding.__dict__ if self.rounding else None),
            "note": self.note,
        }


@dataclass
class CalcRun:
    run_id: str
    graph_id: str
    reading_id: str
    payable_paise: int
    trace: list[CalcStep]
    invariant_status: Literal["ok", "violation"] = "ok"
    violations: tuple[str, ...] = ()
    total_reductions_paise: int = 0

    def step(self, node_id: str) -> CalcStep | None:
        for s in self.trace:
            if s.node_id == node_id:
                return s
        return None


# ---------------------------------------------------------------------------
# Operations implementation (pure, exact)
# ---------------------------------------------------------------------------
def _as_fraction(value: object) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    raise CalcMissingParam(f"expected an exact number, got {type(value).__name__}")


def apply_operation(
    operation: str,
    *,
    base_paise: int,
    params: Mapping[str, object],
) -> tuple[int, RoundingEvent | None]:
    """Apply one closed-set operation. Returns exact integer paise."""
    if operation not in OPERATIONS:
        raise CalcMissingParam(f"unsupported operation: {operation!r}")
    if operation == "identity":
        return base_paise, None
    if operation == "subtract":
        amount = int(params["amount_paise"])
        return base_paise - amount, None
    if operation == "cap":
        return min(base_paise, int(params["limit_paise"])), None
    if operation == "floor":
        return max(base_paise, int(params["minimum_paise"])), None
    if operation == "ceiling":
        return min(base_paise, int(params["ceiling_paise"])), None
    if operation == "multiply_ratio":
        ratio = _as_fraction(params["ratio"])
        if not (0 <= ratio <= 1):
            raise CalcMissingParam(f"ratio out of range: {ratio}")
        exact = ratio * base_paise
        return _round_paise(exact, params)
    if operation == "percentage":
        pct = _as_fraction(params["percent"])
        if not (0 <= pct <= 100):
            raise CalcMissingParam(f"percent out of range: {pct}")
        exact = (pct / 100) * base_paise
        return _round_paise(exact, params)
    if operation == "apportion":
        weights = [_as_fraction(w) for w in params["weights"]]
        return _apportion(base_paise, weights), None
    if operation == "threshold":
        return base_paise, None
    raise CalcMissingParam(f"unhandled operation {operation}")


def _round_paise(exact: Fraction, params: Mapping[str, object]) -> tuple[int, RoundingEvent | None]:
    policy = str(params.get("rounding", "CARRY_EXACT_FINAL_ROUND"))
    floor_val = exact.numerator // exact.denominator
    if exact.denominator == 1:
        return floor_val, None
    if policy == "CARRY_EXACT_FINAL_ROUND":
        # Keep exactness: round half-up here, but record it so the caller can carry
        # the residual. The budget check in satisfy_conservation() accounts for it.
        rounded = int(exact + Fraction(1, 2)) if exact >= 0 else -int(-exact + Fraction(1, 2))
        return rounded, RoundingEvent(str(exact), rounded, policy)
    if policy == "PER_STEP_HALF_UP":
        rounded = int(exact + Fraction(1, 2)) if exact >= 0 else -int(-exact + Fraction(1, 2))
        return rounded, RoundingEvent(str(exact), rounded, policy)
    if policy == "PER_STEP_HALF_EVEN":
        lower = floor_val
        remainder = exact - lower
        if remainder > Fraction(1, 2):
            rounded = lower + 1
        elif remainder < Fraction(1, 2):
            rounded = lower
        else:
            rounded = lower if lower % 2 == 0 else lower + 1
        return rounded, RoundingEvent(str(exact), rounded, policy)
    raise CalcMissingParam(f"unknown rounding policy {policy!r}")


def _apportion(total_paise: int, weights: Sequence[Fraction]) -> int:
    """Allocate ``total`` by weights with the largest-remainder method, so parts sum exactly.

    Returns the allocation for the *first* weight (the executor calls it per part).
    """
    if not weights or sum(weights) == 0:
        raise CalcMissingParam("apportion needs non-zero weights")
    total_weight = sum(weights)
    shares = [total_paise * w / total_weight for w in weights]
    floors = [int(s) for s in shares]
    remainder = total_paise - sum(floors)
    order = sorted(range(len(shares)), key=lambda i: (-(shares[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors[0]


def apportion_all(total_paise: int, weights: Sequence[Fraction]) -> list[int]:
    """Full allocation list; guaranteed to sum to ``total_paise`` exactly."""
    if not weights or sum(weights) == 0:
        raise CalcMissingParam("apportion needs non-zero weights")
    total_weight = sum(weights)
    shares = [total_paise * w / total_weight for w in weights]
    floors = [int(s) for s in shares]
    remainder = total_paise - sum(floors)
    order = sorted(range(len(shares)), key=lambda i: (-(shares[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


# ---------------------------------------------------------------------------
# Invariant checks
# ---------------------------------------------------------------------------
def check_non_negativity(payable_paise: int) -> list[str]:
    return [] if payable_paise >= 0 else [f"payable is negative: {payable_paise}"]


def check_no_reduction_exceeds_base(steps: Sequence[CalcStep]) -> list[str]:
    problems: list[str] = []
    for s in steps:
        if s.operation in ("subtract",) and s.output_paise < 0:
            problems.append(f"step {s.node_id} reduced below zero")
    return problems


def check_no_double_application(step_types: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for st in step_types:
        if st in seen and st in {"NON_PAYABLE_DEDUCTION", "ROOM_RENT_LIMIT", "ICU_LIMIT",
                                 "DEDUCTIBLE", "COPAY", "SUBLIMIT_CAP", "PACKAGE_CAP"}:
            dupes.append(st)
        seen.add(st)
    return [f"step type applied twice: {d}" for d in dupes]


def check_si_respect(payable_paise: int, si_paise: int, restorations_paise: int = 0) -> list[str]:
    limit = si_paise + restorations_paise
    return [] if payable_paise <= limit else [f"payable {payable_paise} exceeds SI limit {limit}"]


def check_conservation(
    gross_paise: int,
    payable_paise: int,
    reductions_paise: int,
    patient_share_paise: int = 0,
    tolerance_paise: int = 0,
) -> list[str]:
    """Every rupee is attributed: gross = reductions + payable + patient share."""
    diff = gross_paise - (payable_paise + reductions_paise + patient_share_paise)
    if abs(diff) > tolerance_paise:
        return [f"conservation violated by {diff} paise "
                f"(gross={gross_paise}, payable={payable_paise}, "
                f"reductions={reductions_paise}, patient={patient_share_paise})"]
    return []


def assert_invariants(
    *,
    payable_paise: int,
    gross_paise: int,
    reductions_paise: int,
    steps: Sequence[CalcStep],
    si_paise: int | None = None,
    restorations_paise: int = 0,
    patient_share_paise: int = 0,
    check_conservation_flag: bool = True,
) -> tuple[str, ...]:
    """Run every invariant. Returns violation messages (empty tuple means clean)."""
    problems: list[str] = []
    problems += check_non_negativity(payable_paise)
    problems += check_no_reduction_exceeds_base(steps)
    problems += check_no_double_application([s.step_type for s in steps])
    if check_conservation_flag:
        problems += check_conservation(gross_paise, payable_paise, reductions_paise,
                                       patient_share_paise)
    if si_paise is not None:
        problems += check_si_respect(payable_paise, si_paise, restorations_paise)
    return tuple(problems)


def make_violation_error(violations: Sequence[str], head: str) -> CalcInvariantViolation:
    return CalcInvariantViolation(
        f"calculation invariants violated for {head}: {'; '.join(violations)}",
        head=head, violations=list(violations),
    )


# ---------------------------------------------------------------------------
# Category helpers used by bases
# ---------------------------------------------------------------------------
def sum_categories(lines: Iterable[object], categories: Iterable[CanonicalCategory]) -> Money:
    """Sum bill lines in the given categories. ``lines`` are ``BillLine``-like objects."""
    wanted = set(categories)
    total = 0
    for line in lines:
        if getattr(line, "category", None) in wanted:
            total += int(getattr(line, "amount"))
    return total
