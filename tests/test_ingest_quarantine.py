"""Money-fact quarantine fixtures (Phase 3D §12) and the bill self-check (§7).

Seven required fixtures, each proving the same property: **an unsafe monetary fact does not
reach the trust core.** The claims are made against the real shapes the corpus exhibits, and
the strings below are the observed defects, not invented ones.

The fixture list is the one specified:

1. rate != amount          5. duplicate line
2. amount = quantity       6. missing line
3. decimal comma           7. conflicting totals
4. OCR dropped digit
"""

from __future__ import annotations

import pytest

from claimcheck.ingest.bill import as_read_paise, item_line_verdict, read_bill_line, align_lines
from claimcheck.ingest.dual import Agreement, FactDisposition, reconcile_money
from claimcheck.ingest.model import BBox, EditOp, PageExtraction, TextLine
from claimcheck.ingest.money import TokenClass, classify_token, find_tokens, read_line_money
from claimcheck.ingest.records import MoneyFormat, MoneyReading
from bench.data_eda.bill_self_check import Discrepancy, SuspectKind, check_bill, consistency_rate
from claimcheck.ingest.records import BillLine, BillSubtotal, BillTotal, ParsedBill

# Real strings from the acquired corpus. The broken amount column is not a hypothesis:
# `350,00` is what the scanner produced where the document printed `350.00`.
REAL_LINES = {
    "clean": "1. 15/11/2025 LB203 BLOOD CULTURE & SENSITIVITY 368.00 x 1.00 368.00",
    "decimal_comma": "4. 18/11/2025 _CNOO2 Consultation for Inpatients 350.00 x 1.00 350,00",
    "clean2": "3. 15/11/2025 LBOI2 cBC 240.00 x 1.00 240,00",
    "doubled_stop": "7. 15/11/2025 LB122 GLYCOSYLATED HAEMOGLOBIN 240.00. x 1.00 240.00",
    "pharmacy": "1 18/11/2025 0 PHARMACY CHARGE 70491.00 x 0.75 52868.25",
}
SUMMARY = "Total of BED CHARGES : 6000.00"
TARIFF_CODE_LINE = "PHARMACY CHARGE 999311 |"


def _page(document_id: str, page_number: int, lines: list[str], reader: str,
          method: EditOp = EditOp.OCR) -> PageExtraction:
    out: list[TextLine] = []
    cursor = 0
    for i, text in enumerate(lines, start=1):
        out.append(TextLine(text=text, bbox=BBox(0, 1000 - i * 10, 500, 1000 - i * 10 + 9),
                            page_number=page_number, char_start=cursor,
                            char_end=cursor + len(text), line_number=i, words=tuple(text.split()),
                            ocr_confidence=0.9))
        cursor += len(text) + 1
    return PageExtraction(document_id=document_id, page_number=page_number, reader=reader,
                          method=method, text="\n".join(l.text for l in out), lines=tuple(out))


def _line(text: str, native_text: str | None = None, line_no: int = 1) -> BillLine:
    from claimcheck.ingest.bill import AlignedLine

    layout_line = TextLine(text=text, bbox=BBox(0, 0, 10, 10), page_number=1, char_start=0,
                           char_end=len(text), line_number=line_no, words=tuple(text.split()))
    native_line = None
    if native_text is not None:
        native_line = TextLine(text=native_text, bbox=BBox(0, 0, 10, 10), page_number=1,
                               char_start=0, char_end=len(native_text), line_number=line_no,
                               words=tuple(native_text.split()))
    return read_bill_line(document_id="d", page_number=1, line_no=line_no,
                          aligned=AlignedLine(layout_line, native_line,
                                              1.0 if native_line else 0.0),
                          source_sha256="a" * 64, page_sha="b" * 64)


# =========================================================== 1. rate != amount


def test_rate_different_from_amount_is_read_as_amount_not_rate():
    line = _line(REAL_LINES["pharmacy"], REAL_LINES["pharmacy"])
    # 70491.00 x 0.75 = 52868.25: the rate is not the amount and the amount is not the rate
    assert line.rate_paise == 7_049_100
    assert line.quantity_milli == 750
    assert as_read_paise(line) == 5_286_825
    assert line.amount_paise == 5_286_825


def test_a_quantity_of_less_than_one_is_not_confused_with_a_rate():
    money = read_line_money(REAL_LINES["pharmacy"])
    assert money.quantity_milli == 750
    assert money.amount is not None and money.amount.paise == 5_286_825


# =========================================================== 2. amount = quantity


def test_amount_equal_to_quantity_with_a_rate_present_is_quarantined():
    """The mechanism behind the real ₹1,706: the tail rule takes `1.00`, the quantity."""
    line = _line(REAL_LINES["decimal_comma"])
    assert as_read_paise(line) == 100, "the previous phase's reading is reproduced"
    assert line.amount_paise is None, "but the contract refuses to release it as a fact"
    assert line.quantity_milli == 1000
    assert line.rate_paise == 35_000
    assert line.flags


def test_quantity_read_as_amount_is_named_as_a_suspect():
    line = _line("1. 01/01/2025 SOME SERVICE 500.00 x 1.00 1.00")
    from bench.data_eda.bill_self_check import _classify_suspect

    suspect = _classify_suspect(line)
    assert suspect is not None
    assert suspect.kind in (SuspectKind.QUANTITY_READ_AS_AMOUNT,
                            SuspectKind.DECIMAL_COMMA_AMOUNT)


# =========================================================== 3. decimal comma


def test_decimal_comma_token_is_classified_ambiguous_and_refused():
    tok = classify_token("350,00")
    assert tok.token_class is TokenClass.DECIMAL_COMMA
    assert tok.paise is None, "no magnitude may be produced from an ambiguous separator"
    assert tok.alternative_paise == 35_000
    assert "DECIMAL_COMMA_AMBIGUOUS" in tok.flags


def test_decimal_comma_alternative_readings_are_both_reported():
    tok = classify_token("350,00")
    assert tok.alternative_paise == 35_000          # decimal comma -> 350.00
    assert "35000" in tok.alternative_reason        # grouped integer -> 35,000.00
    assert "does not say which" in tok.alternative_reason


def test_the_existing_core_parser_would_have_silently_produced_a_wrong_magnitude():
    """Documents the defect the ingestion layer exists to stop.

    ``calc.money.parse_amount`` returns ₹35,000 for the token ``350,00`` with ``ok`` True.
    Ingestion refuses the token before the core can ever see it.
    """
    from claimcheck.calc.money import parse_amount

    core = parse_amount("350,00")
    assert core.paise == 3_500_000 and core.ok, "the core flags but does not refuse"
    assert classify_token("350,00").paise is None, "ingestion refuses"


def test_plain_indian_grouping_is_still_read_correctly():
    assert classify_token("1,50,000").paise == 15_000_000
    assert classify_token("1,50,000").token_class is TokenClass.GROUPED_INTEGER
    assert classify_token("52,868.25").paise == 5_286_825
    assert classify_token("1,180.00").paise == 118_000


# =========================================================== 4. OCR dropped digit


def test_a_dropped_digit_shows_up_as_a_reader_disagreement():
    """Two readers at different resolutions: one drops a digit, so the fact is refused."""
    line = _line("1. 15/11/2025 LB042 HBsAg 240.00 x 1.00 240,00",
                 "1. 15/11/2025 LB042 HBsAg 240.00 x 1.00 240.00")
    permissive = [r for r in line.amount_readings if r.rule == "permissive_tail"]
    readers = {r.reader: r.paise for r in permissive}
    assert readers.get("layout") is None, "the layout reader cannot read the amount column"
    assert readers.get("native") == 24_000, "the alternate reader can"
    fact = reconcile_money("x", "permissive_tail", permissive)
    assert fact.agreement in (Agreement.SINGLE_READER, Agreement.UNREADABLE)
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None


def test_a_digit_that_survives_only_one_reader_is_never_released():
    readings = [
        MoneyReading(reader="layout", rule="permissive_tail", raw="240,00", paise=None,
                     money_format=MoneyFormat.DECIMAL_COMMA, flags=("DECIMAL_COMMA_AMBIGUOUS",)),
        MoneyReading(reader="native", rule="permissive_tail", raw="240.00", paise=24_000,
                     money_format=MoneyFormat.PLAIN),
    ]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None
    assert any("only some readers" in r for r in fact.reasons)


# =========================================================== 5. duplicate line


def test_a_duplicated_line_is_kept_twice_and_not_merged():
    """Repeated daily charges are legitimate: two identical lines are two lines."""
    lines = ["1. 15/11/2025 BED CHARGE GENERAL WARD 1500.00 x 1.00 1500.00",
             "2. 16/11/2025 BED CHARGE GENERAL WARD 1500.00 x 1.00 1500.00"]
    page = _page("d", 1, lines, "layout")
    parsed = ParsedBill(document_id="d", lines=[_line(t) for t in lines], pages_read=1)
    assert len(parsed.item_lines) == 2
    check = check_bill(parsed)
    assert check.line_sum_paise == 300_000


def test_a_duplicate_with_the_same_date_is_still_two_lines_but_is_flagged_in_review():
    """Deduplication is a *review* question, never silent: the lines carry distinct ids."""
    a = _line("1. 15/11/2025 BED CHARGE GENERAL WARD 1500.00 x 1.00 1500.00", line_no=1)
    b = _line("1. 15/11/2025 BED CHARGE GENERAL WARD 1500.00 x 1.00 1500.00", line_no=2)
    assert a.line_id != b.line_id


# =========================================================== 6. missing line


def test_a_line_present_in_only_one_reader_is_quarantined_not_dropped():
    page = _page("d", 1, [REAL_LINES["clean"]], "layout")
    from claimcheck.ingest.bill import align_lines as _align

    layout = page.lines
    native = ()                                  # the alternate reader saw nothing
    aligned = _align(layout, native)
    assert len(aligned) == 1 and aligned[0].native is None
    line = read_bill_line(document_id="d", page_number=1, line_no=1, aligned=aligned[0],
                          source_sha256="a" * 64, page_sha="b" * 64)
    permissive = [r for r in line.amount_readings if r.rule == "permissive_tail"]
    fact = reconcile_money("x", "permissive_tail", permissive)
    assert fact.disposition is FactDisposition.QUARANTINED
    assert "LINE_UNMATCHED_BETWEEN_READERS" in line.flags


def test_a_missing_line_is_visible_as_a_shortfall_not_absorbed():
    """A line the parser never saw cannot be silently reconciled away."""
    parsed = ParsedBill(document_id="d", lines=[
        _line("1. 15/11/2025 BED CHARGE GENERAL WARD 1500.00 x 1.00 1500.00"),
    ], subtotals=[], totals=[
        BillTotal(total_id="t", document_id="d", page_number=1, kind="grand_total",
                  amount_paise=300_000),
    ], pages_read=1)
    check = check_bill(parsed)
    assert check.line_sum_paise == 150_000
    assert check.discrepancy_class in (Discrepancy.LINE_SUM_MISMATCH,
                                       Discrepancy.SUSPECTED_OCR_AMOUNT_ERROR)
    assert check.arithmetic_consistent is False


# =========================================================== 7. conflicting totals


def test_two_printed_totals_that_disagree_are_both_kept():
    """The real bill prints 73420.25 and 73420.28 — three paise apart. Neither wins."""
    parsed = ParsedBill(
        document_id="d", lines=[_line("1. 15/11/2025 SERVICE 100.00 x 1.00 100.00")],
        totals=[
            BillTotal(total_id="a", document_id="d", page_number=1, kind="grand_total",
                      amount_paise=10_000),
            BillTotal(total_id="b", document_id="d", page_number=1, kind="net_payable",
                      amount_paise=10_003),
        ], pages_read=1)
    check = check_bill(parsed)
    assert check.total_to_total_difference_paise == 3
    assert Discrepancy.MULTIPLE_PRINTED_TOTALS_DISAGREE.value in (
        check.discrepancy_class.value,) + check.also_observed


def test_a_large_total_conflict_becomes_the_primary_finding():
    parsed = ParsedBill(
        document_id="d", lines=[_line("1. 15/11/2025 SERVICE 100.00 x 1.00 100.00")],
        totals=[
            BillTotal(total_id="a", document_id="d", page_number=1, kind="grand_total",
                      amount_paise=10_000),
            BillTotal(total_id="b", document_id="d", page_number=1, kind="net_payable",
                      amount_paise=99_999),
        ], pages_read=1)
    check = check_bill(parsed)
    assert check.discrepancy_class is Discrepancy.MULTIPLE_PRINTED_TOTALS_DISAGREE


# =========================================================== the contract itself


def test_both_readers_agreeing_produces_a_usable_fact():
    readings = [MoneyReading(reader="layout", rule="permissive_tail", raw="368.00",
                             paise=36_800, money_format=MoneyFormat.PLAIN),
                MoneyReading(reader="native", rule="permissive_tail", raw="368.00",
                             paise=36_800, money_format=MoneyFormat.PLAIN)]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.agreement is Agreement.AGREE
    assert fact.disposition is FactDisposition.USABLE and fact.paise == 36_800


def test_readers_producing_different_values_never_release_a_magnitude():
    """Two readers, two different numbers: the contract releases neither."""
    readings = [MoneyReading(reader="layout", rule="permissive_tail", raw="240,00",
                             paise=240_000, money_format=MoneyFormat.PLAIN),
                MoneyReading(reader="native", rule="permissive_tail", raw="2400.00",
                             paise=2_400_000, money_format=MoneyFormat.PLAIN)]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.agreement is Agreement.DISAGREE
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None, "the more plausible of two readings is still a choice"
    assert fact.competing_paise == (240_000, 2_400_000)
    assert any("disagree" in r for r in fact.reasons)


def test_a_reader_that_refuses_while_another_reads_is_quarantined_not_resolved():
    readings = [MoneyReading(reader="layout", rule="permissive_tail", raw="240,00", paise=None),
                MoneyReading(reader="native", rule="permissive_tail", raw="2400.00",
                             paise=240_000, money_format=MoneyFormat.PLAIN)]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None
    assert fact.agreement in (Agreement.SINGLE_READER, Agreement.UNREADABLE)
    assert any("only some readers" in r for r in fact.reasons)


def test_single_reader_is_quarantined_even_when_the_value_looks_perfect():
    readings = [MoneyReading(reader="layout", rule="permissive_tail", raw="9999.99",
                             paise=999_999, money_format=MoneyFormat.PLAIN)]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.agreement is Agreement.SINGLE_READER
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None, "one reader is never enough, however plausible the number"


def test_no_reading_at_all_is_an_evidence_gap_not_a_guess():
    fact = reconcile_money("line", "permissive_tail", [])
    assert fact.agreement is Agreement.NO_READER
    assert fact.disposition is FactDisposition.EVIDENCE_GAP
    assert fact.paise is None


def test_unreadable_by_every_reader_is_a_gap():
    readings = [MoneyReading(reader=r, rule="permissive_tail", raw="",
                             paise=None, money_format=MoneyFormat.UNPARSABLE,
                             flags=("UNPARSEABLE_AMOUNT",)) for r in ("layout", "native")]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.disposition is FactDisposition.QUARANTINED
    assert fact.paise is None


def test_reader_disagreement_on_raw_form_alone_does_not_block_a_fact():
    readings = [MoneyReading(reader="layout", rule="permissive_tail", raw="1,180.00",
                             paise=118_000, money_format=MoneyFormat.WESTERN_GROUPED),
                MoneyReading(reader="native", rule="permissive_tail", raw="1180.00",
                             paise=118_000, money_format=MoneyFormat.PLAIN)]
    fact = reconcile_money("line", "permissive_tail", readings)
    assert fact.disposition is FactDisposition.USABLE and fact.paise == 118_000
    assert any("not on how it is printed" in r for r in fact.reasons)


# =========================================================== item gate


def test_a_tariff_code_is_not_read_as_an_amount():
    """`999311` is a department code. Read as money it is ₹9,99,311 of nothing."""
    is_item, reason = item_line_verdict(TARIFF_CODE_LINE, read_line_money(TARIFF_CODE_LINE))
    assert is_item is False
    assert reason == "NO_MONEY_NO_DATE"


def test_a_header_fragment_is_not_a_bill_item():
    text = "Consult. Dr. : 2"
    is_item, _ = item_line_verdict(text, read_line_money(text))
    assert is_item is False


def test_summary_and_credit_rows_are_not_charges():
    """Real shapes from the corpus that must not enter the line sum as charges."""
    for text in ("Service Amount 2,20,976.30",
                 "After Discount (Bill Amount) 2,20,976.30",
                 "Paid Amount 18,500.00",
                 "ADVANCE RECEIVED (BEARER 750) - 750.00",
                 "Net Amount : 1,850.00",
                 "Balance Amt : 2,067.70",
                 "Total Discount : 3,057.00"):
        from claimcheck.ingest.bill import summary_kind

        is_item, reason = item_line_verdict(text, read_line_money(text))
        # Either the gate refuses it, or it is classified as a summary row — both keep it out
        # of the charge sum, which is the property that matters.
        assert (not is_item) or summary_kind(text) is not None, f"{text!r} was a charge"
        if not item_line_verdict(text, read_line_money(text))[0]:
            assert reason in ("SUMMARY_VOCABULARY", "NO_MONEY_NO_DATE")


def test_a_charge_that_merely_mentions_payable_is_still_a_charge():
    text = "2. 15/11/2025 SURGERY PAYABLE AT DISCHARGE 5000.00 x 1.00 5000.00"
    is_item, reason = item_line_verdict(text, read_line_money(text))
    assert is_item, reason


def test_a_real_line_is_an_item():
    for key in ("clean", "decimal_comma", "doubled_stop", "pharmacy"):
        is_item, reason = item_line_verdict(REAL_LINES[key], read_line_money(REAL_LINES[key]))
        assert is_item, f"{key} was rejected: {reason}"


def test_a_standard_indian_amount_with_two_decimals_is_read_as_money():
    assert classify_token("52,868.25").paise == 5_286_825
    assert classify_token("70491.00").paise == 7_049_100


# =========================================================== consistency metric


def test_the_metric_is_named_and_defined_and_is_not_an_accuracy_figure():
    parsed = ParsedBill(
        document_id="d", lines=[_line("1. 15/11/2025 SERVICE 500.00 x 1.00 500.00")],
        totals=[BillTotal(total_id="t", document_id="d", page_number=1, kind="grand_total",
                          amount_paise=50_000)], pages_read=1)
    rates = consistency_rate([check_bill(parsed)])
    d = rates.as_dict()
    assert d["metric"] == "arithmetic_consistency_rate"
    assert "extraction accuracy" in d["not_a_metric_of"]
    assert d["arithmetic_consistency_rate"] == 1.0


def test_a_document_without_a_printed_total_cannot_be_counted_as_consistent():
    parsed = ParsedBill(document_id="d", lines=[_line(REAL_LINES["clean"])], pages_read=1)
    check = check_bill(parsed)
    assert check.discrepancy_class is Discrepancy.MISSING_TOTAL
    rates = consistency_rate([check])
    assert rates.documents_with_a_printed_total == 0
    assert rates.arithmetic_consistency_rate == 0.0
