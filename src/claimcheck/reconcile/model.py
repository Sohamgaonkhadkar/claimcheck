"""Reconstruct the insurer's arithmetic, then compare it with the lawful recomputation.

Two computations, one comparison, and **no third number invented to fill the gap**:

    A  what the settlement letter implies    -> :class:`InsurerModel`
    B  what this policy, with these rules, supports -> the calculator's run
    A vs B -> :class:`Reconciliation`, which reports the difference in three parts:

        supported_difference   cuts a rule forbids, net of the lawful steps that would
                               still apply to the restored amount. This is the amount
                               that holds under *every* reading, so it is the amount
                               that can be asked for without over-claiming.
        unexplained_difference a line with no head, ratio or clause behind it.
        undetermined           anything the documents do not settle.

The reconciliation never reports the largest difference a reading could support.
Where the readings differ, the minimum is reported and the readings are shown
alongside it, individually labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from ..calc.money import format_inr
from ..schema import BillFacts, CanonicalCategory, SettlementFacts

#: Cut amounts are usually a clean ratio of a head set. A candidate base is accepted
#: only if the implied ratio is a fraction with a small denominator; anything else is
#: a coincidence, and coincidences do not explain other people's arithmetic.
MAX_RATIO_DENOMINATOR = 200


@dataclass(frozen=True)
class HeadCut:
    """One deduction line from the letter, attributed to a head (or left unattributed)."""

    head: str
    cut_paise: int
    letter_text: str
    attributed_by: str                 # implied_scope | head_name_match | unattributed
    ratio_applied: Fraction | None = None
    bill_amount_paise: int | None = None
    barred_by_rule: bool = False
    rule_ids: tuple[str, ...] = ()
    reason_quote: str = ""

    @property
    def is_attributed(self) -> bool:
        return self.attributed_by != "unattributed"


@dataclass(frozen=True)
class RatioCandidate:
    """A head set whose sum could be the base of the letter's ratio, and the ratio it implies."""

    head_set: tuple[str, ...]
    base_paise: int
    cut_fraction: Fraction               # cut / base
    applied_ratio: Fraction              # 1 - cut_fraction (the share the insurer allowed)
    simple: bool                         # denominator small enough to be a real policy ratio


@dataclass(frozen=True)
class RatioCut:
    """The reconstructed proportionate-deduction line."""

    cut_fraction: Fraction
    applied_fraction: Fraction
    stated_fraction: Fraction | None = None

    @property
    def as_percent(self) -> str:
        return f"{float(self.cut_fraction) * 100:.4g}%"


@dataclass
class InsurerModel:
    settlement_id: str
    claimed_paise: int
    net_payable_paise: int
    lines: tuple[HeadCut, ...]
    ratio_cut: RatioCut | None = None
    implied_scope: tuple[str, ...] = ()
    implied_base_paise: int | None = None
    residual_paise: int = 0                # the letter's arithmetic does not close
    unexplained_paise: int = 0             # a line with no stated basis
    head_cuts: tuple[HeadCut, ...] = ()
    barred_cut_paise: int = 0
    ratio_candidates: tuple[RatioCandidate, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def barred_cuts(self) -> tuple[HeadCut, ...]:
        return tuple(h for h in self.head_cuts if h.barred_by_rule)


RATIO_HEAD_KEYWORDS = ("proportionate", "pro rata", "prorata", "ratio", "associated medical")
ROOM_KEYWORDS = ("room rent restriction", "room rent", "room category", "room-rent")
NONPAYABLE_KEYWORDS = ("not payable", "non-payable", "non payable", "excluded", "exclusion")
COPAY_KEYWORDS = ("co-pay", "copay", "co-payment", "copayment", "deductible", "excess")
#: a deduction the policy's own ceiling explains. Without this, a letter that states the
#: sum-insured excess -- the ordinary way a capped claim is paid -- is reported as an
#: unattributed deduction, and a case the insurer settled exactly is called UNDETERMINED.
SUM_INSURED_KEYWORDS = ("sum insured", "sum-insured", "above the sum insured", "si limit",
                        "exceeds the sum insured")
SUM_INSURED_HEAD = "sum_insured_ceiling"
UNEXPLAINED_KEYWORDS = ("other deduction", "miscellaneous", "other", "balance")


# ---------------------------------------------------------------------------
def reconstruct_insurer_model(
    settlement: SettlementFacts,
    bill: BillFacts,
    policy: Any = None,          # accepted for call-site symmetry; unused by design
    *,
    barred_heads: Sequence[str] = (),
    candidate_head_sets: Mapping[str, Sequence[str]] | None = None,
    policy_items: Sequence[tuple[str, Sequence[str]]] = (),
) -> InsurerModel:
    """Reconstruct what the letter's numbers imply, without trusting its labels."""
    branch_amounts = _branch_amounts(bill)
    lines: list[HeadCut] = []
    notes: list[str] = []

    # -- 1. the proportionate-deduction line ---------------------------------
    ratio_cut, implied_scope, implied_base, candidates = _reconstruct_ratio(
        settlement, branch_amounts, candidate_head_sets or {})
    if ratio_cut is None and any(_is_ratio_line(d) for d in settlement.deductions):
        notes.append(
            "a proportionate deduction was applied but no candidate head set reproduces "
            "it exactly; the reconstruction is reported as indeterminate rather than fitted"
        )
    if ratio_cut is not None:
        notes.append(
            f"the letter's ratio reproduces exactly when applied to {list(implied_scope)} "
            f"(base {format_inr(implied_base)})"
        )

    # -- 2. attribute every line ---------------------------------------------
    barred = {h.lower() for h in barred_heads}
    for line in settlement.deductions:
        head, how, fraction = _attribute_line(line, ratio_cut, implied_scope, branch_amounts,
                                              policy_items)
        if how == "implied_scope" and implied_scope:
            # The letter states one total for the ratio. Its own ratio and its own implied
            # scope divide that total into per-head cuts — computed here, exactly, in
            # integer paise with the residual allocated deterministically.
            lines.extend(_expand_ratio_line(line, ratio_cut, implied_scope, branch_amounts,
                                            barred))
            continue
        bill_amount = branch_amounts.get(head)
        is_barred = head in barred and how == "implied_scope"
        lines.append(HeadCut(
            head=head,
            cut_paise=int(line.amount),
            letter_text=_line_text(line),
            attributed_by=how,
            ratio_applied=fraction,
            bill_amount_paise=bill_amount,
            barred_by_rule=is_barred,
            reason_quote=getattr(line, "reason_text", "") or "",
        ))

    # -- 3. is the letter internally consistent? -----------------------------
    residual = _residual(settlement)
    if residual:
        notes.append(
            f"the letter's own arithmetic does not close: {format_inr(residual)} unexplained"
        )

    head_cuts = tuple(h for h in lines if h.is_attributed)
    unexplained = sum(h.cut_paise for h in lines if not h.is_attributed)
    if unexplained:
        notes.append(
            "one or more deduction lines name no head and cite no term: "
            + ", ".join(f"{_line_text(h)} ({format_inr(h.cut_paise)})"
                        for h in lines if not h.is_attributed)
        )

    return InsurerModel(
        settlement_id=settlement.settlement_id,
        claimed_paise=settlement.claimed_amount,
        net_payable_paise=settlement.final_payable,
        lines=tuple(lines),
        ratio_cut=ratio_cut,
        implied_scope=tuple(implied_scope),
        implied_base_paise=implied_base,
        residual_paise=residual,
        unexplained_paise=unexplained,
        head_cuts=head_cuts,
        barred_cut_paise=sum(h.cut_paise for h in head_cuts if h.barred_by_rule),
        ratio_candidates=candidates,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
def _branch_amounts(bill: BillFacts) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in bill.lines:
        key = line.category.value if isinstance(line.category, CanonicalCategory) else str(line.category)
        out[key] = out.get(key, 0) + int(line.amount)
    return out


def _line_text(line: Any) -> str:
    return getattr(line, "raw_head_text", "") or getattr(line, "head", "")


def _is_ratio_line(line: Any) -> bool:
    text = _line_text(line).lower()
    return any(k in text for k in RATIO_HEAD_KEYWORDS) or getattr(line, "ratio_stated", None)


def _base_tolerance(expected_base_paise: int) -> int:
    """How far a head-set total may sit from the base a stated ratio implies.

    Two effects are absorbed and nothing more: the letter rounds the cut to the paisa
    (at most one paisa), and a ratio that reaches us as printed text carries half a unit
    of its last printed digit (0.05% for one decimal place). A tenth of a percent of the
    base covers both with room to spare and stays far tighter than any two plausible
    head sets on the same bill.
    """
    return max(2, expected_base_paise // 1000)


def _reconstruct_ratio(
    settlement: SettlementFacts,
    branch_amounts: Mapping[str, int],
    candidate_head_sets: Mapping[str, Sequence[str]],
) -> tuple[RatioCut | None, tuple[str, ...], int | None, tuple[RatioCandidate, ...]]:
    """Solve for the ratio and the base together, and keep every solution that fits.

    Given a deduction of ``c`` from a base ``B`` at a ratio ``r`` (``c = B * (1 - r)``),
    one equation with two unknowns has no unique answer — so the answer is constrained
    by *this* bill: the base must be the sum of some set of this bill's heads, and the
    ratio must be a ratio a policy could plausibly state.
    """
    ratio_lines = [d for d in settlement.deductions if _is_ratio_line(d)]
    if not ratio_lines:
        return None, (), None, ()
    cut = sum(int(getattr(d, "amount", 0)) for d in ratio_lines)
    if cut <= 0:
        return None, (), None, ()

    candidates: list[RatioCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for _name, heads in (candidate_head_sets or {}).items():
        key = tuple(sorted(heads))
        if key in seen:
            continue
        seen.add(key)
        base = sum(branch_amounts.get(h, 0) for h in heads)
        if base <= 0:
            continue
        cut_fraction = Fraction(cut, base)
        if not (0 < cut_fraction < 1):
            continue
        simple = cut_fraction.denominator <= MAX_RATIO_DENOMINATOR
        candidates.append(RatioCandidate(
            head_set=key, base_paise=base, cut_fraction=cut_fraction,
            applied_ratio=1 - cut_fraction, simple=simple,
        ))

    stated = next((getattr(d, "ratio_stated", None) for d in ratio_lines
                   if getattr(d, "ratio_stated", None)), None)
    if stated is not None:
        # The letter states its own ratio: trust it for *reconstruction of the base*,
        # never as a source of the policy parameter.
        #
        # The cut is a rounded money amount (and, when the ratio reaches us as printed
        # text, the ratio itself is printed to a fixed number of decimals), so the base
        # it implies is only accurate to within that rounding. Candidates are matched
        # within ``_base_tolerance`` paise -- and only when the match is *unique*. Two
        # head sets that both fit means the letter's scope is genuinely ambiguous, and
        # an ambiguous scope is reported as unknown rather than guessed.
        applied = Fraction(stated)
        cut_fraction = 1 - applied
        expected_base = int(Fraction(cut, 1) / cut_fraction) if cut_fraction else None
        matches: list[RatioCandidate] = []
        if expected_base:
            tolerance = _base_tolerance(expected_base)
            matches = [c for c in candidates
                       if abs(c.base_paise - expected_base) <= tolerance]
        if not matches:
            matches = [c for c in candidates if c.cut_fraction == cut_fraction]
        if len(matches) == 1:
            scope, base = matches[0].head_set, matches[0].base_paise
        else:
            scope = ()
            base = expected_base
        return (RatioCut(cut_fraction=cut_fraction, applied_fraction=applied,
                         stated_fraction=Fraction(stated)),
                tuple(scope), base, tuple(candidates))

    simple = [c for c in candidates if c.simple]
    if len(simple) != 1:
        # 0 candidates: nothing reproduces the cut. >1: the letter is ambiguous.
        return None, (), None, tuple(candidates)
    c = simple[0]
    return (RatioCut(cut_fraction=c.cut_fraction, applied_fraction=c.applied_ratio),
            c.head_set, c.base_paise, tuple(candidates))


def _expand_ratio_line(line: Any, ratio_cut: RatioCut, implied_scope: Sequence[str],
                       branch_amounts: Mapping[str, int],
                       barred: set[str]) -> list[HeadCut]:
    """Split the letter's single ratio total across the heads its own ratio implies."""
    total = int(line.amount)
    amounts = {h: int(branch_amounts.get(h, 0)) for h in implied_scope}
    base = sum(amounts.values())
    exact = {h: Fraction(a) * ratio_cut.cut_fraction for h, a in amounts.items()}
    shares = {h: int(v) for h, v in exact.items()}          # exact truncation, no float
    remainder = total - sum(shares.values())
    if remainder:
        order = sorted(amounts, key=lambda h: (-amounts[h], h))
        for h in order:
            if remainder == 0:
                break
            step = 1 if remainder > 0 else -1
            shares[h] += step
            remainder -= step
    out = []
    for head in sorted(implied_scope):
        amount = amounts[head]
        cut = shares[head]
        out.append(HeadCut(
            head=head,
            cut_paise=cut,
            letter_text=_line_text(line),
            attributed_by="implied_scope",
            ratio_applied=ratio_cut.cut_fraction,
            bill_amount_paise=amount,
            barred_by_rule=head.lower() in barred,
            reason_quote=getattr(line, "reason_text", "") or "",
        ))
    if base and abs(sum(shares.values()) - total) > 0:      # cannot happen; belt and braces
        raise AssertionError("ratio expansion does not reproduce the letter's total")
    return out


def _attribute_line(line: Any, ratio_cut: RatioCut | None, implied_scope: Sequence[str],
                    branch_amounts: Mapping[str, int],
                    policy_items: Sequence[tuple[str, Sequence[str]]] = (),
                    ) -> tuple[str, str, Fraction | None]:
    text = _line_text(line).lower()
    if ratio_cut is not None and _is_ratio_line(line):
        return "proportionate_deduction", "implied_scope", ratio_cut.cut_fraction
    if any(k in text for k in ROOM_KEYWORDS):
        return "room", "head_name_match", None
    if any(k in text for k in COPAY_KEYWORDS):
        return "copay", "head_name_match", None
    if any(k in text for k in SUM_INSURED_KEYWORDS):
        # The policy's own ceiling: an explained cut, computed by the same graph the
        # recomputation uses (step SUM_INSURED_CEILING).
        return SUM_INSURED_HEAD, "stated_label", None
    for canonical, aliases in policy_items:
        if any(a in text for a in aliases):
            return _slug(canonical), "policy_item", None
    if any(k in text for k in NONPAYABLE_KEYWORDS):
        head = _named_head(text, branch_amounts)
        if head:
            return head, "head_name_match", None
    if any(k in text for k in UNEXPLAINED_KEYWORDS):
        return "unattributed", "unattributed", None
    head = _named_head(text, branch_amounts)
    if head:
        return head, "head_name_match", None
    return "unattributed", "unattributed", None


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")


def _named_head(text: str, branch_amounts: Mapping[str, int]) -> str | None:
    for head in branch_amounts:
        if head.replace("_", " ") in text or head in text:
            return head
    aliases = {"pharmacy": ("medicine", "pharmacy", "drugs"),
               "diagnostics": ("diagnostic", "investigation"),
               "nursing": ("nursing",), "surgeon": ("surgeon",),
               "anaesthesia": ("anaesthesia", "anesthesia"),
               "ot_charges": ("operation theatre", "ot charge"),
               "consumables": ("consumable", "dressing"), "icu": ("icu", "intensive"),
               "room": ("room rent", "room-rent", "room category")}
    for head, words in aliases.items():
        if head in branch_amounts and any(w in text for w in words):
            return head
    return None


def _residual(settlement: SettlementFacts) -> int:
    """claimed - sum(deductions) - net_payable, in paise (0 when the letter closes)."""
    deducted = sum(int(getattr(d, "amount", 0)) for d in settlement.deductions)
    return int(settlement.claimed_amount) - deducted - int(settlement.final_payable)


# ---------------------------------------------------------------------------
@dataclass
class Reconciliation:
    """A and B side by side, with the difference split into named parts."""

    insurer: InsurerModel
    lawful_payable_paise: int
    paid_paise: int
    difference_paise: int
    restore_recompute_paise: int
    supported_paise: int
    unexplained_paise: int
    unclaimed_headroom_paise: int = 0        # lawful payable not claimed at all by the bill
    readings: tuple[tuple[str, int], ...] = ()
    notes: tuple[str, ...] = ()
    identity_ok: bool = True
    identity_note: str = ""

    @property
    def supported(self) -> int:
        return self.supported_paise

    def summary(self) -> str:
        return (f"lawful {format_inr(self.lawful_payable_paise)} vs paid "
                f"{format_inr(self.paid_paise)}: difference {format_inr(self.difference_paise)}"
                f" = supported {format_inr(self.supported_paise)}"
                f" + unexplained {format_inr(self.unexplained_paise)}")


def reconcile(
    insurer: InsurerModel,
    *,
    lawful_payable_paise: int,
    restoration_payable_paise: int,
    unexplained_paise: int | None = None,
    readings: Iterable[tuple[str, int]] = (),
    rounding_slack_paise: int = 0,
) -> Reconciliation:
    """Compare the two computations and refuse to over-claim.

    ``supported_paise`` is the *minimum* across readings of the difference the rules
    support — never the maximum, and never a reading average. ``identity_ok`` checks
    that the restoration route and the recomputation route agree; when they disagree
    the verdict's provenance gate fails and the amount is not asserted.
    """
    unexplained = insurer.unexplained_paise if unexplained_paise is None else unexplained_paise
    paid = int(insurer.net_payable_paise)
    difference = int(lawful_payable_paise) - paid
    restore_delta = int(restoration_payable_paise) - paid

    identity = restore_delta + unexplained
    # The two routes apply the policy's fractional steps to different bases: the
    # recomputation applies them to the whole admissible amount, the restoration applies
    # them to the restored delta alone. One half-up rounding per re-applied fractional
    # step can therefore separate the routes by a paisa even when both are right. The
    # slack is bounded by the number of such steps (the caller counts them from the
    # restoration trace), it is never larger than that, and whatever residual remains is
    # stated. A difference of one rupee or more is still a failure.
    residual = identity - difference
    identity_ok = abs(residual) <= rounding_slack_paise
    note = ""
    if not identity_ok:
        note = (f"the restoration route gives {format_inr(restore_delta)} while the recomputation "
                f"route gives {format_inr(difference)}: the difference is not asserted")
    elif residual:
        note = (f"the two routes agree to {format_inr(abs(residual))}: the rounding of the "
                f"policy's fractional steps re-applied to the restored amount alone "
                f"({rounding_slack_paise} step{'s' if rounding_slack_paise != 1 else ''})")

    per_reading = tuple(readings)
    diffs = [int(v) - paid for _k, v in per_reading] or [difference]
    # ``supported`` is a claim about money the insurer withheld. It can never be
    # negative: when the settlement paid more than the recomputation supports, there is
    # nothing to claim, and the amount is reported as an overpayment instead of being
    # carried into the finding. (Found by the Phase-3B Tier-1 benchmark, archetype A18:
    # a quarantined fact left the recomputation below the settlement and the difference
    # was published as -53,400.)
    supported = max(0, min(restore_delta, min(diffs)))

    notes: list[str] = []
    if len(per_reading) > 1:
        notes.append(
            "difference under each reading: "
            + "; ".join(f"{k} {format_inr(v - paid)}" for k, v in per_reading)
            + f" — the amount put forward is the smallest ({format_inr(supported)})"
        )
    overpaid = max(0, -difference)
    if overpaid:
        notes.append(
            f"the settlement paid {format_inr(overpaid)} more than this recomputation "
            f"supports; nothing is claimed on that footing")
    if restore_delta > supported:
        notes.append(
            f"the barred cuts total {format_inr(insurer.barred_cut_paise)} before downstream "
            f"steps; after the lawful steps that would still apply to the restored amount "
            f"the claim is {format_inr(restore_delta)}"
        )

    return Reconciliation(
        insurer=insurer,
        lawful_payable_paise=int(lawful_payable_paise),
        paid_paise=paid,
        difference_paise=difference,
        restore_recompute_paise=restore_delta,
        supported_paise=supported,
        unexplained_paise=unexplained,
        readings=per_reading,
        notes=tuple(notes),
        identity_ok=identity_ok,
        identity_note=note,
    )
