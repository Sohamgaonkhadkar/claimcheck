"""Three-valued logic and the predicate language: UNKNOWN never collapses into a value.

These are the smallest tests in the suite and the ones that matter most: every
"the system chose the wrong reading" failure starts with an UNKNOWN that quietly
became a FALSE somewhere convenient.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from claimcheck.logic import (
    FactProvider,
    PredicateError,
    Tri,
    all_of,
    any_of,
    collect_fact_refs,
    evaluate_predicate,
    negate,
)
from claimcheck.schema import Fact, FactStatus, Origin


def test_all_and_any_keep_unknown_until_it_cannot_matter():
    assert all_of(Tri.TRUE, Tri.TRUE) is Tri.TRUE
    assert all_of(Tri.TRUE, Tri.FALSE) is Tri.FALSE
    assert all_of(Tri.TRUE, Tri.UNKNOWN) is Tri.UNKNOWN
    assert all_of(Tri.FALSE, Tri.UNKNOWN) is Tri.FALSE      # decided despite the gap
    assert any_of(Tri.FALSE, Tri.UNKNOWN) is Tri.UNKNOWN
    assert any_of(Tri.TRUE, Tri.UNKNOWN) is Tri.TRUE
    assert negate(Tri.UNKNOWN) is Tri.UNKNOWN


def test_truthiness_is_refused():
    with pytest.raises(TypeError):
        bool(Tri.UNKNOWN)


def test_missing_fact_is_unknown_not_false():
    provider = FactProvider({"policy.present": True})
    pred = {"fact": {"fact_id": "policy.absent", "op": "eq", "value": True}}
    assert evaluate_predicate(pred, provider).value is Tri.UNKNOWN
    assert evaluate_predicate({"not": pred}, provider).value is Tri.UNKNOWN


def test_absent_and_quarantined_facts_both_block():
    store_fact = Fact(fact_id="room_rate", type="loadbearing.money", value=800_000,
                      origin=Origin.PRINTED, status=FactStatus.QUARANTINED)
    provider = FactProvider({}, store=type("S", (), {"get": lambda _s, fid: store_fact})())
    out = evaluate_predicate({"fact": {"fact_id": "room_rate", "op": "gt", "value": 0}},
                             provider)
    assert out.value is Tri.UNKNOWN
    assert "quarantined" in out.explain().lower()


def test_comparisons_are_exact_for_money_and_ratios():
    provider = FactProvider({"a": 500_000, "b": Fraction(5, 8), "c": "post-2021"})
    assert evaluate_predicate({"fact": {"fact_id": "b", "op": "lt", "value": 1}}, provider).value is Tri.TRUE
    assert evaluate_predicate({"fact": {"fact_id": "a", "op": "eq", "value": 500_000}}, provider).value is Tri.TRUE
    assert evaluate_predicate({"fact": {"fact_id": "c", "op": "eq", "value": 2021}}, provider).value is Tri.UNKNOWN


def test_dates_compare_and_are_never_strings():
    provider = FactProvider({"claim_date": date(2026, 7, 14)})
    fresh = {"fact": {"fact_id": "claim_date", "op": "ge", "value": date(2021, 4, 1)}}
    stale = {"fact": {"fact_id": "claim_date", "op": "lt", "value": date(2021, 4, 1)}}
    assert evaluate_predicate(fresh, provider).value is Tri.TRUE
    assert evaluate_predicate(stale, provider).value is Tri.FALSE


def test_a_malformed_predicate_is_an_error_not_a_silent_false():
    with pytest.raises(PredicateError):
        evaluate_predicate({"fact": "x", "op": "eq"}, FactProvider({}))
    with pytest.raises(PredicateError):
        evaluate_predicate({"nonsense": 1}, FactProvider({}))


def test_fact_references_are_collectable_for_evidence_requirements():
    pred = {"all": [
        {"fact": {"fact_id": "a", "op": "eq", "value": 1}},
        {"any": [{"fact": {"fact_id": "b", "op": "present"}},
                 {"not": {"fact": {"fact_id": "c", "op": "present"}}}]},
    ]}
    assert collect_fact_refs(pred) == ("a", "b", "c")


def test_set_and_has_work_on_the_provider():
    p = FactProvider()
    assert not p.has("x")
    p.set("x", Tri.UNKNOWN)
    assert not p.has("x")          # a Tri.UNKNOWN value is not a usable fact
    p.set("x", 1)
    assert p.has("x")
