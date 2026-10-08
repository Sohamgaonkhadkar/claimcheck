"""Non-payable items: conditional, sourced, and never a boolean lookup table.

The wrong design is ``item -> not payable``. The right design is a statement with a
**source**, a **condition**, and an **exception**:

    "Attendant charges"                                   <- item name (from a list)
      stance: NOT_PAYABLE                                 <- what the source says
      source: policy annexure / IRDAI standard list        <- where it comes from
      conditions: ["when claimed as a separate line item"] <- when it bites
      exceptions: ["when the policy has no such annexure"] <- when it does not

An item whose condition is not stated in the case is **INDETERMINATE**, and an item
whose only source is an unverified list cannot decide anything at all.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..logic import Tri, tri_from_bool
from .model import RuleStatus

#: Stance values. ``PAYABLE`` exists so the model can carry an insurer's positive
#: promise (e.g. "modern treatments are payable") rather than only prohibitions.
STANCE_NOT_PAYABLE = "NOT_PAYABLE"
STANCE_NOT_PAYABLE_UNLESS = "NOT_PAYABLE_UNLESS"
STANCE_PAYABLE = "PAYABLE"
STANCE_NOT_APPLICABLE = "NOT_APPLICABLE"

SOURCE_POLICY = "policy_document"
SOURCE_STANDARD_LIST = "standard_list"


class ItemState(str, enum.Enum):
    NOT_PAYABLE = "NOT_PAYABLE"
    PAYABLE = "PAYABLE"
    INDETERMINATE = "INDETERMINATE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXCLUDED_BY_POLICY = "EXCLUDED_BY_POLICY"


@dataclass(frozen=True)
class NonPayableItem:
    """One entry of one list, with its provenance and its conditions."""

    item_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    stance: str
    source: str                     # policy_document | standard_list (or a named list id)
    source_version: str = ""
    conditions: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()
    subsumed_into: str | None = None    # the item is inclusive of another item
    status: RuleStatus = RuleStatus.UNVERIFIED
    evidence_id: str | None = None
    quote: str = ""

    def matches(self, text: str) -> bool:
        t = normalise_item(text)
        if not t:
            return False
        names = (self.canonical_name, *self.aliases)
        for n in names:
            nn = normalise_item(n)
            if not nn:
                continue
            if t == nn or re.search(rf"\b{re.escape(nn)}\b", t):
                return True
        return False


@dataclass(frozen=True)
class ItemVerdict:
    item_id: str
    canonical_name: str
    line_id: str | None
    state: ItemState
    source: str
    source_version: str
    evidence_id: str | None
    quote: str
    reasons: tuple[str, ...]
    fired_conditions: tuple[str, ...] = ()
    withheld: bool = False

    @property
    def is_non_payable(self) -> bool:
        return self.state in (ItemState.NOT_PAYABLE, ItemState.EXCLUDED_BY_POLICY)


@dataclass
class ConditionContext:
    """Facts the conditions are tested against (prescription, indication, package…)."""

    facts: Mapping[str, Any] = field(default_factory=dict)

    def test(self, condition: str) -> Tri:
        v = self.facts.get(condition, None)
        if isinstance(v, Tri):
            return v
        if v is None:
            return Tri.UNKNOWN
        return tri_from_bool(bool(v))


def normalise_item(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
def evaluate_item(item: NonPayableItem, line_text: str, line_id: str | None,
                  conditions: ConditionContext | None = None) -> ItemVerdict:
    """Does this list entry apply to this bill line, and what does it mean for it?"""
    conditions = conditions or ConditionContext()
    baseline = dict(item_id=item.item_id, canonical_name=item.canonical_name,
                    line_id=line_id, source=item.source, source_version=item.source_version,
                    evidence_id=item.evidence_id, quote=item.quote)

    if not item.matches(line_text):
        return ItemVerdict(state=ItemState.NOT_APPLICABLE, reasons=(
            "the item does not describe this bill line",), **baseline)

    if item.status in (RuleStatus.UNVERIFIED, RuleStatus.GATED, RuleStatus.CONTESTED):
        return ItemVerdict(state=ItemState.INDETERMINATE, reasons=(
            f"the list entry comes from a source that is {item.status.value}; it cannot "
            f"decide payability until the source is verified",), **baseline)

    if item.subsumed_into:
        return ItemVerdict(state=ItemState.NOT_APPLICABLE, reasons=(
            f"the line is treated as part of '{item.subsumed_into}', which the policy "
            f"treats separately",), **baseline)

    if item.stance == STANCE_PAYABLE:
        return ItemVerdict(state=ItemState.PAYABLE, reasons=(
            "the source says this item is payable",), **baseline)

    if item.stance == STANCE_NOT_PAYABLE:
        fired = tuple(item.conditions)
        unresolved = [c for c in item.conditions if conditions.test(c) is Tri.UNKNOWN]
        if unresolved:
            return ItemVerdict(
                state=ItemState.INDETERMINATE,
                reasons=("the item is excluded only under stated conditions, and these are "
                         "not established for this line: " + "; ".join(unresolved),),
                fired_conditions=fired, **baseline)
        refuted = [c for c in item.conditions if conditions.test(c) is Tri.FALSE]
        if refuted:
            return ItemVerdict(state=ItemState.PAYABLE, reasons=(
                "the exclusion condition did not hold for this line: " + "; ".join(refuted),),
                fired_conditions=fired, **baseline)
        state = (ItemState.EXCLUDED_BY_POLICY if item.source == SOURCE_POLICY
                 else ItemState.NOT_PAYABLE)
        reason = ("the policy's own annexure excludes this item" if item.source == SOURCE_POLICY
                  else "the applicable list says this item is not payable")
        return ItemVerdict(state=state, reasons=(reason,), fired_conditions=fired, **baseline)

    return ItemVerdict(state=ItemState.INDETERMINATE, reasons=(
        f"stance {item.stance!r} is not one this engine knows how to apply",), **baseline)


def evaluate_items(items: Sequence[NonPayableItem], line_text: str,
                   conditions: ConditionContext | None = None) -> tuple[ItemVerdict, ...]:
    """All list entries that could bear on one bill line — including the ones that do not."""
    out = []
    for it in items:
        v = evaluate_item(it, line_text, None, conditions)
        if v.state is not ItemState.NOT_APPLICABLE:
            out.append(v)
    return tuple(out)


def non_payable_total(verdicts: Iterable[ItemVerdict],
                      amounts: Mapping[str, int]) -> tuple[int, tuple[str, ...]]:
    """Sum the amounts that are *decided* non-payable, and name what stays undecided."""
    total = 0
    undecided: list[str] = []
    for v in verdicts:
        amount = amounts.get(v.canonical_name, 0)
        if v.is_non_payable:
            total += amount
        elif v.state is ItemState.INDETERMINATE:
            undecided.append(v.canonical_name)
    return total, tuple(dict.fromkeys(undecided))


def can_compute(total_indeterminate_names: Sequence[str]) -> bool:
    """An undecided item does not stop the arithmetic; it stops the *claim* about it.

    The number is computed from the decided items; the undecided ones are carried into
    the report as a named gap. Silently dropping them would understate the claim;
    silently including them would overstate it.
    """
    return True
