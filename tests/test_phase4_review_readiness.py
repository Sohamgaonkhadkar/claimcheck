"""Pure Phase 4 review-value allow-list and fail-closed type validation tests."""
from __future__ import annotations

import pytest

from claimcheck.application.review_readiness import field_spec, validate_field_value
from claimcheck.schema import CanonicalCategory


def test_review_field_allow_list_is_narrow_and_role_scoped() -> None:
    assert field_spec("policy.sum_insured_paise").roles == ("policy_schedule",)
    assert field_spec("bill.line.BL-123.amount_paise").kind == "money"
    assert field_spec("bill.line.BL-123.category").kind == "category"
    assert field_spec("settlement.line.SF-123.head").kind == "head"
    assert field_spec("case.latest_analysis_run_id") is None
    assert field_spec("bill.line.BL-123.other") is None


def test_money_review_values_require_bounded_integer_paise() -> None:
    assert validate_field_value("bill.total_paise", 10_000) == 10_000
    for unsafe in (True, 1.0, "10000", -1, 10**14 + 1):
        with pytest.raises(ValueError):
            validate_field_value("bill.total_paise", unsafe)


def test_categories_and_settlement_heads_are_explicit_not_guessed() -> None:
    assert validate_field_value("bill.line.BL-123.category", "room") == "room"
    with pytest.raises(ValueError):
        validate_field_value("bill.line.BL-123.category", CanonicalCategory.UNMAPPED.value)
    with pytest.raises(ValueError):
        validate_field_value("bill.line.BL-123.category", "pharmacy-ish")
    assert validate_field_value("settlement.line.SF-123.head", "non_payable") == "non_payable"
    with pytest.raises(ValueError):
        validate_field_value("settlement.line.SF-123.head", "Non-Payable")


def test_percent_boolean_and_calendar_date_validation_is_exact() -> None:
    assert validate_field_value("policy.co_pay_percent", "12.5") == "12.5"
    for unsafe in (True, 101, "101", "10.555", 12.5):
        with pytest.raises(ValueError):
            validate_field_value("policy.co_pay_percent", unsafe)
    assert validate_field_value("settlement.repudiated", False) is False
    with pytest.raises(ValueError):
        validate_field_value("settlement.repudiated", "false")
    assert validate_field_value("policy.claim_date", "2026-10-06") == "2026-10-06"
    with pytest.raises(ValueError):
        validate_field_value("policy.claim_date", "2026-02-30")
