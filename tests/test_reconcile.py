"""Reconstructing what the insurer did, and refusing to over-claim afterwards.

The reconciliation is where a system that wants to be dramatic would cheat: it could
report the largest difference any reading supports. It does not. Every test here is
about that restraint, and about the exactness of the arithmetic that backs it.
"""

from __future__ import annotations

import dataclasses as dc
from datetime import date
from fractions import Fraction

import pytest

from claimcheck.cases.golden_001 import build_case
from claimcheck.calc.money import format_inr
from claimcheck.pipeline import run_case
from claimcheck.reconcile.model import (
    MAX_RATIO_DENOMINATOR,
    reconcile,
    reconstruct_insurer_model,
)

CASE = build_case()


def model(**kw):
    return reconstruct_insurer_model(
        CASE.settlement, CASE.bill,
        barred_heads=["pharmacy", "consumables", "implants", "medical_devices",
                      "diagnostics", "icu"],
        candidate_head_sets=CASE.candidate_head_sets,
        policy_items=tuple((i.canonical_name, i.aliases) for i in CASE.policy.non_payable_items),
        **kw,
    )


def test_the_ratio_and_its_base_are_solved_together():
    m = model()
    assert m.ratio_cut is not None
    assert m.ratio_cut.cut_fraction == Fraction(3, 8)      # 37.5% removed
    assert m.ratio_cut.applied_fraction == Fraction(5, 8)  # 62.5% allowed, as printed
    assert m.implied_base_paise == 14_300_000
    assert set(m.implied_scope) == {"nursing", "surgeon", "anaesthesia", "ot_charges",
                                    "icu", "pharmacy", "diagnostics"}


def test_per_head_cuts_reproduce_the_letter_exactly():
    m = model()
    cuts = {h.head: h.cut_paise for h in m.head_cuts if h.attributed_by == "implied_scope"}
    assert sum(cuts.values()) == 5_362_500
    assert cuts["icu"] == 800_000 * 3 // 8
    assert cuts["pharmacy"] == 1_500_000 * 3 // 8
    assert cuts["diagnostics"] == 2_500_000 * 3 // 8
    assert cuts["nursing"] == 375_000


def test_only_the_heads_a_rule_bars_are_marked_barred():
    m = model()
    barred = {h.head for h in m.head_cuts if h.barred_by_rule}
    assert barred == {"icu", "pharmacy", "diagnostics"}
    assert m.barred_cut_paise == 1_800_000


def test_the_policy_annexure_line_is_attributed_to_the_item_not_called_unexplained():
    m = model()
    attendant = next(h for h in m.head_cuts if h.head == "attendant_charges")
    assert attendant.attributed_by == "policy_item"
    assert attendant.barred_by_rule is False      # excluding it is exactly what the policy says


def test_a_line_with_no_stated_basis_is_reported_as_unexplained():
    m = model()
    assert m.unexplained_paise == 234_000
    assert any("Other deductions" in h.letter_text for h in m.lines if not h.is_attributed)


def test_the_letters_own_arithmetic_is_checked():
    m = model()
    assert m.residual_paise == 0            # the letter closes on its own numbers
    broken = dc.replace(CASE.settlement, final_payable=CASE.settlement.final_payable - 100)
    m2 = reconstruct_insurer_model(broken, CASE.bill,
                                   candidate_head_sets=CASE.candidate_head_sets)
    assert m2.residual_paise == 100


def test_a_ratio_that_no_head_set_reproduces_is_left_indeterminate():
    weird = dc.replace(
        CASE.settlement,
        deductions=tuple(
            d if d.head != "proportionate_deduction"
            else dc.replace(d, amount=5_362_501, ratio_stated=None)
            for d in CASE.settlement.deductions
        ),
    )
    m = reconstruct_insurer_model(weird, CASE.bill, candidate_head_sets=CASE.candidate_head_sets)
    assert m.ratio_cut is None
    assert any("indeterminate" in n for n in m.notes)


def test_an_ambiguous_ratio_with_two_plausible_bases_is_not_fitted():
    """Two candidate head sets that both produce a plausible ratio: no winner is declared."""
    cut = 3_000_000
    sets = {"a": ["nursing", "surgeon"], "b": ["nursing", "surgeon", "anaesthesia"]}
    letter = dc.replace(
        CASE.settlement,
        deductions=(dc.replace(CASE.settlement.deductions[2], amount=cut, ratio_stated=None),),
    )
    m = reconstruct_insurer_model(letter, CASE.bill, candidate_head_sets=sets)
    simple = [c for c in m.ratio_candidates if c.simple]
    assert len(simple) <= 1 or m.ratio_cut is None


def test_the_difference_reported_is_the_smallest_across_readings():
    run = run_case(CASE)
    rec = reconcile(run.insurer, lawful_payable_paise=run.lawful_payable,
                    restoration_payable_paise=run.restoration.payable_paise,
                    readings=tuple(sorted(run.readings.items())))
    assert rec.readings == (("R0", 13_263_750), ("R1", 16_470_000))
    assert rec.supported_paise == 1_620_000          # R0, the smaller difference
    assert rec.supported_paise != 5_060_250          # what R1 would have allowed him to claim
    assert rec.identity_ok


def test_the_two_routes_must_agree_or_the_amount_is_withheld():
    run = run_case(CASE)
    rec = reconcile(run.insurer, lawful_payable_paise=run.lawful_payable,
                    restoration_payable_paise=run.restoration.payable_paise + 1,
                    readings=tuple(sorted(run.readings.items())))
    assert rec.identity_ok is False
    assert "not asserted" in rec.identity_note


def test_summary_is_readable_and_exact():
    run = run_case(CASE)
    rec = reconcile(run.insurer, lawful_payable_paise=run.lawful_payable,
                    restoration_payable_paise=run.restoration.payable_paise,
                    readings=tuple(sorted(run.readings.items())))
    assert rec.summary() == ("lawful ₹1,32,637.50 vs paid ₹1,14,097.50: difference "
                             "₹18,540.00 = supported ₹16,200.00 + unexplained ₹2,340.00")
