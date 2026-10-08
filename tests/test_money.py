"""Indian number architecture tests (Phase-2 §17, Phase-3 item 6)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from claimcheck.calc.money import (
    amount_in_words,
    as_ratio,
    format_inr,
    format_ratio,
    parse_amount,
    parse_money,
    parse_number_words,
    parse_percent,
    percent_to_fraction,
    validate_amount,
)


@pytest.mark.parametrize(
    "raw,expected_paise",
    [
        ("\u20b95,000", 500_000),
        ("Rs. 5,000", 500_000),
        ("Rs 5000", 500_000),
        ("\u20b95,00,000", 50_000_000),
        ("\u20b91,25,450.50", 12_545_050),
        ("5,00,000", 50_000_000),
        ("1.25 lakh", 12_500_000),
        ("2.11 crore", 21_100_000_00),
        ("2.11 crores", 21_100_000_00),
        ("1,83,500.00", 18_350_000),
        ("183,500.00", 18_350_000),
        ("1,83,500/-", 18_350_000),
        ("Rs. 1,00,000 only", 10_000_000),
        ("INR 1250.75", 125_075),
        ("1,50", 15_000),          # Indian short group -> 150, NOT 1.50
        ("1500.5", 150_050),
        ("50k", 5_000_000),
        ("1.5 L", 15_000_000),      # flagged ambiguous, still parsed
        ("12,34,567.89", 1_234_567_89),
        ("   \u20b9  25,000   only ", 2_500_000),
    ],
)
def test_parses_indian_formats(raw, expected_paise):
    p = parse_amount(raw)
    assert p.ok, (raw, p.error)
    assert p.paise == expected_paise, (raw, p.paise, expected_paise)


def test_grouping_style_is_reported():
    assert parse_amount("1,83,500").format_detected == "indian"
    assert parse_amount("18,35,500").format_detected == "indian"
    assert parse_amount("183,500").format_detected == "ambiguous_equal"
    assert parse_amount("1,500").format_detected == "ambiguous_equal"
    assert parse_amount("183500").format_detected == "plain"


def test_lakh_crore_words_are_not_read_as_decimals():
    # The single most expensive mistake in the whole product: 1.25 lakh != 1.25
    assert parse_money("1.25 lakh") == 12_500_000
    assert parse_money("1.25 lakh") != 125
    assert parse_money("2 crore") == 20_000_000_00


def test_ocr_confused_numbers():
    p = parse_amount("5,0O0.00")
    assert p.ok and p.paise == 500_000
    assert p.ocr_corrected and "OCR_CORRECTED" in p.flags
    assert p.confidence < 1.0

    p2 = parse_amount("1,83,5OO")
    assert p2.ok and p2.paise == 18_350_000

    p3 = parse_amount("l,OOO")
    assert p3.ok and p3.paise == 100_000


def test_ocr_repair_never_rewrites_words():
    p = parse_amount("SON")
    assert not p.ok  # must fail, not become '50N'


def test_leading_trailing_symbols():
    for raw in ["\u20b9 1,000/-", "= 1,000", "\u20b91,000 only", "Rs.:1,000"]:
        p = parse_amount(raw)
        assert p.ok and p.paise == 100_000, raw


def test_words_and_digits():
    assert parse_number_words("One Lakh Eighty Three Thousand Five Hundred") == 183_500
    assert parse_number_words("Rupees Two Crore Eleven Lakh Only") == 2_11_00_000
    assert amount_in_words(18_350_000) == "one lakh eighty three thousand five hundred rupees"

    p = parse_amount("Rupees One Lakh Only")
    assert p.ok and p.paise == 10_000_000 and p.format_detected == "words"


def test_words_digits_disagreement_blocks():
    res = validate_amount(18_300_000, raw="1,83,000", words_text="One Lakh Eighty Three Thousand Five Hundred")
    assert not res.ok
    assert "WORDS_DIGITS_DISAGREE" in res.codes()


def test_plausibility_and_arithmetic_validation():
    res = validate_amount(100_000_00, sum_insured_paise=500_000)
    assert not res.ok and "IMPLAUSIBLE_VS_SI" in res.codes()

    res2 = validate_amount(500_000, aggregate_paise=499_000, tolerance_paise=50)
    assert "ARITHMETIC_MISMATCH" in res2.codes()


def test_unparseable_inputs_are_typed_failures():
    for raw in ["", "   ", "nothing here", "N/A"]:
        p = parse_amount(raw)
        assert not p.ok and p.error
    with pytest.raises(Exception):
        parse_money("no amount at all")


def test_format_inr_indian_grouping():
    assert format_inr(18_350_000) == "\u20b91,83,500.00"
    assert format_inr(1_234_567_89) == "\u20b912,34,567.89"
    assert format_inr(500_000) == "\u20b95,000.00"
    assert format_inr(-250_000) == "-\u20b92,500.00"
    assert format_inr(10_000_000, decimals=0) == "\u20b91,00,000"


def test_percent_and_ratio_are_exact():
    assert parse_percent("10%") == Fraction(10)
    assert percent_to_fraction(Fraction(10)) == Fraction(1, 10)
    assert as_ratio(500_000, 800_000) == Fraction(5, 8)
    assert as_ratio(1, 0) is None
    assert format_ratio(Fraction(5, 8)) == "0.625"
    # No floating point anywhere in the trusted path.
    assert Fraction(1, 10) * 18_000_000 == 1_800_000
