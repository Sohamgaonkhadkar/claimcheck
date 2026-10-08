"""GOLDEN-001, end to end: the milestone, asserted to the paise.

One case, structured facts only, no model, no OCR, no retrieval: policy + bill +
settlement + rule pack -> graph -> calculator -> insurer reconstruction ->
reconciliation -> findings -> verdict. Every number below was computed by hand first
and is asserted exactly.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from claimcheck.cases.golden_001 import AS_OF, CASE_ID, build_case
from claimcheck.calc.money import format_inr
from claimcheck.explain.report import build_report, render_letter, render_markdown
from claimcheck.pipeline import run_case


@pytest.fixture(scope="module")
def run():
    return run_case(build_case())


# ---- 1. the case is what it claims to be -----------------------------------
def test_the_case_is_synthetic_and_says_so(run):
    assert run.case.origin == "synthetic"
    assert CASE_ID in run.case.case_id
    report = build_report(run)
    assert report["case"]["origin"] == "synthetic"
    md = render_markdown(run)
    assert "synthetic" in md.lower()


def _imports_in_a_clean_interpreter(module: str, forbidden: tuple[str, ...]) -> dict[str, bool]:
    """Ask a **fresh** interpreter what importing ``module`` pulls in.

    Checking ``sys.modules`` in-process is order-dependent: the moment any other test in the
    session imports an ingestion module, the rail fires on innocent code and people learn to
    ignore it. A subprocess tests the property that actually matters — *this* module does not
    reach for a model, an API or the ingestion layer.
    """
    import json as _json
    import subprocess
    import sys as _sys

    src = (
        "import sys, json\n"
        f"import {module}\n"
        f"print(json.dumps({{m: (m in sys.modules) for m in {forbidden!r}}}))\n"
    )
    out = subprocess.run([_sys.executable, "-c", src], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": _src_path()}, check=True)
    return _json.loads(out.stdout.strip().splitlines()[-1])


def _src_path() -> str:
    import pathlib as _pathlib
    return str(_pathlib.Path(__file__).resolve().parents[1] / "src")


def test_no_model_loads_anywhere_on_this_path():
    """The trust core must not import the LLM, retrieval, ingestion or API layers at all."""
    forbidden = ("claimcheck.llm", "claimcheck.retrieve", "claimcheck.ingest",
                 "claimcheck.extract", "claimcheck.api")
    loaded = _imports_in_a_clean_interpreter("claimcheck.pipeline", forbidden)
    assert not any(loaded.values()), loaded


# ---- 2. the policy's own computation (B) -----------------------------------
def test_lawful_payable_is_computed_step_by_step():
    run = run_case(build_case())
    trace = {s.node_id: s for s in run.execution.run.trace}
    assert trace["NP1"].output_paise == 19_800_000       # 2,00,000 less attendant charges
    assert trace["RR1"].output_paise == 2_500_000        # 5 days x Rs. 5,000
    assert trace["PD1"].output_paise == 5_937_500        # 95,000 x 5/8
    assert trace["CP1"].output_paise == 1_473_750        # 10% of 1,47,375
    assert trace["SI1"].output_paise == 13_263_750       # ceiling not reached
    assert run.execution.run.invariant_status == "ok"


def test_patient_share_and_reductions_are_named_separately(run):
    assert run.execution.reductions_paise == 3_762_500    # attendant + proportionate cut
    assert run.execution.patient_share_paise == 2_973_750  # room excess + co-payment
    assert (run.execution.payable_paise + run.execution.reductions_paise
            + run.execution.patient_share_paise) == 20_000_000


# ---- 3. what the insurer did (A) -------------------------------------------
def test_the_letter_is_reconstructed_from_its_own_numbers(run):
    assert run.insurer.implied_base_paise == 14_300_000
    assert set(run.insurer.implied_scope) == {"nursing", "surgeon", "anaesthesia",
                                              "ot_charges", "icu", "pharmacy", "diagnostics"}
    assert run.insurer.ratio_cut.applied_fraction == 5 / 8 or \
        run.insurer.ratio_cut.applied_fraction == __import__("fractions").Fraction(5, 8)
    assert run.insurer.residual_paise == 0         # the letter closes on its own terms


def test_the_rule_barred_cuts_are_isolated(run):
    assert run.insurer.barred_cut_paise == 1_800_000          # Rs. 18,000
    barred = {h.head: h.cut_paise for h in run.insurer.head_cuts if h.barred_by_rule}
    assert barred == {"icu": 300_000, "pharmacy": 562_500, "diagnostics": 937_500}


def test_a_deduction_with_no_stated_basis_is_unexplained_not_guessed(run):
    assert run.insurer.unexplained_paise == 234_000           # Rs. 2,340


# ---- 4. the comparison (A vs B) --------------------------------------------
def test_the_difference_is_split_into_supported_and_unexplained(run):
    assert run.reconciliation.lawful_payable_paise == 13_263_750
    assert run.reconciliation.paid_paise == 11_409_750
    assert run.reconciliation.difference_paise == 1_854_000
    assert run.reconciliation.supported_paise == 1_620_000     # Rs. 16,200
    assert run.reconciliation.unexplained_paise == 234_000     # Rs. 2,340
    assert 1_620_000 + 234_000 == 1_854_000


def test_the_gross_cut_is_not_what_is_claimed(run):
    """The headline number is the net one: Rs. 16,200, not the Rs. 18,000 cut."""
    assert run.insurer.barred_cut_paise == 1_800_000
    assert run.reconciliation.restore_recompute_paise == 1_620_000
    assert run.restoration.payable_paise == 13_029_750         # 1,14,097.50 + 18,000 - 1,800


def test_the_provenance_identity_holds(run):
    assert run.reconciliation.identity_ok is True
    assert (run.restoration.payable_paise + run.insurer.unexplained_paise
            == run.lawful_payable)


def test_both_readings_are_computed_and_the_smaller_is_put_forward(run):
    assert run.readings["R0"] == 13_263_750
    assert run.readings["R1"] == 16_470_000                    # the wider, unsupported reading
    assert min(run.readings.values()) == 13_263_750
    assert run.reconciliation.supported_paise == 1_620_000
    assert run.ambiguity.case == "A"                           # the order is stated
    assert run.ambiguity.readings_material is True             # the scope is contested


# ---- 5. findings and verdict -----------------------------------------------
def test_the_money_finding_carries_the_figures_and_the_rules(run):
    money = [f for f in run.adjudication.findings if f.type == "FINANCIAL"]
    assert len(money) == 1
    f = money[0]
    assert f.amount_paise == 1_620_000 and f.amount_gross_paise == 1_800_000
    assert f.state == "POTENTIALLY_INCONSISTENT"
    cited = {c.ref_id for c in f.citations}
    assert {"PD.ICU.BAR", "PD.AME.EXCLUSIONS", "PD.SCOPE.NO_EXTRA"} <= cited
    assert all(c.verified for c in f.citations if c.ref_type == "rule_version")


def test_the_unexplained_money_is_a_gap_with_a_request_attached(run):
    gaps = [f for f in run.adjudication.findings if f.head == "unexplained_deductions"]
    assert gaps and gaps[0].type == "EVIDENCE_GAP"
    assert gaps[0].amount_paise is None          # no number is invented for it
    assert gaps[0].missing


def test_the_procedural_defect_is_reported_without_an_amount(run):
    proc = [f for f in run.adjudication.findings if f.type == "PROCEDURAL"]
    assert proc
    assert proc[0].amount_paise is None
    assert any(c.ref_id == "CL.PARTIAL.REASONS" for c in proc[0].citations)


def test_the_verdict_is_a_level_not_a_verdict_on_anyone(run):
    assert run.adjudication.worst_state() == "POTENTIALLY_INCONSISTENT"
    assert run.adjudication.head_states["proportionate_deduction"] == "POTENTIALLY_INCONSISTENT"
    assert run.adjudication.head_states["unexplained_deductions"] == "UNDETERMINED"


def test_nothing_is_withheld_in_this_case(run):
    assert run.adjudication.withheld == []
    # Nine findings, none hidden: the money finding, the unexplained deduction, the
    # procedural defect, five questions this record cannot settle (two contested
    # instruments, an unverified one, two facts no document states) and the summary.
    assert [f.finding_id for f in run.adjudication.findings] == [f"F{i:03d}"
                                                                 for i in range(1, 10)]
    assert [str(f.type) for f in run.adjudication.findings] == [
        "FINANCIAL", "EVIDENCE_GAP", "PROCEDURAL", "EVIDENCE_GAP", "EVIDENCE_GAP",
        "EVIDENCE_GAP", "EVIDENCE_GAP", "EVIDENCE_GAP", "INFO",
    ]


def test_all_gates_pass_and_are_named(run):
    assert [(g.gate, g.passed) for g in run.adjudication.gates] == [
        ("G1_extraction", True), ("G2_document_set", True), ("G3_policy_support", True),
        ("G5_graph_order", True), ("G6_calculation", True), ("G8_provenance", True),
    ]


# ---- 6. the artefacts --------------------------------------------------------
def test_the_report_contains_every_number_a_reader_needs(run):
    data = build_report(run)
    assert data["money_flow"]["lawful_payable_display"] == "₹1,32,637.50"
    assert data["insurer_model"]["net_payable_display"] == "₹1,14,097.50"
    assert data["reconciliation"]["supported_difference_display"] == "₹16,200.00"
    assert data["verdict"]["state"] == "POTENTIALLY_INCONSISTENT"


def test_the_report_json_is_serialisable_and_free_of_floats_for_money(run):
    data = build_report(run)
    text = json.dumps(data, indent=2, default=str)
    reloaded = json.loads(text)
    assert reloaded["money_flow"]["lawful_payable_paise"] == 13_263_750
    for step in reloaded["money_flow"]["steps"]:
        assert isinstance(step["output_paise"], int)


def test_the_letter_quotes_the_instrument_and_asks_for_the_right_things(run):
    letter = render_letter(run)
    assert "₹16,200.00" in letter
    assert "shall not recover any expenses towards proportionate deductions" in letter
    assert "ICU charges" in letter or "ICU" in letter
    assert "Ombudsman" in letter


def test_the_whole_run_is_deterministic(run):
    again = run_case(build_case())
    assert json.dumps(build_report(run), default=str) == json.dumps(build_report(again),
                                                                    default=str)
    assert render_markdown(run) == render_markdown(again)
    assert render_letter(run) == render_letter(again)


def test_the_case_id_and_as_of_travel_with_the_result(run):
    assert run.case.case_id == CASE_ID
    assert run.case.as_of == AS_OF
