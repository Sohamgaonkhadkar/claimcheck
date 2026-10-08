"""Three-valued logic shared by the graph, the rule engine and the verdict engine.

Everything uncertainty-related in CLAIMCHECK reduces to one idea: a predicate over
facts can be **TRUE**, **FALSE** or **UNKNOWN**, and UNKNOWN is never collapsed into
either of the other two. That is what stops a missing document from becoming a
confident "the insurer was wrong" or a confident "everything is fine".

Predicates are *data* (JSON-shaped), evaluated by a closed set of operators. No
``eval``, no expressions, no model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .schema import Fact, FactStatus


class Tri(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    @property
    def is_decided(self) -> bool:
        return self is not Tri.UNKNOWN

    def __bool__(self) -> bool:  # pragma: no cover - guard against accidental truthiness
        raise TypeError("Tri is three-valued; compare explicitly with Tri.TRUE")


@dataclass(frozen=True)
class PredicateResult:
    value: Tri
    reasons: tuple[str, ...] = ()

    def explain(self) -> str:
        return ", ".join(self.reasons) if self.reasons else self.value.value


def tri_from_bool(value: bool | None) -> Tri:
    if value is None:
        return Tri.UNKNOWN
    return Tri.TRUE if value else Tri.FALSE


def all_of(*values: Tri) -> Tri:
    if any(v is Tri.FALSE for v in values):
        return Tri.FALSE
    if any(v is Tri.UNKNOWN for v in values):
        return Tri.UNKNOWN
    return Tri.TRUE


def any_of(*values: Tri) -> Tri:
    if any(v is Tri.TRUE for v in values):
        return Tri.TRUE
    if any(v is Tri.UNKNOWN for v in values):
        return Tri.UNKNOWN
    return Tri.FALSE


def negate(value: Tri) -> Tri:
    if value is Tri.TRUE:
        return Tri.FALSE
    if value is Tri.FALSE:
        return Tri.TRUE
    return Tri.UNKNOWN


# ---------------------------------------------------------------------------
# Predicate language
# ---------------------------------------------------------------------------
# A predicate is a dict with exactly one key from this closed set:
#
#   {"fact": "<fact_id>", "op": "present"}
#   {"fact": "<fact_id>", "op": "eq",  "value": ...}
#   {"fact": "<fact_id>", "op": "ne",  "value": ...}
#   {"fact": "<fact_id>", "op": "gt",  "value": ...}
#   {"fact": "<fact_id>", "op": "ge",  "value": ...}
#   {"fact": "<fact_id>", "op": "lt",  "value": ...}
#   {"fact": "<fact_id>", "op": "le",  "value": ...}
#   {"fact": "<fact_id>", "op": "in",  "value": [...]}
#   {"fact": "<fact_id>", "op": "contains", "value": ...}
#   {"all": [ ... ]}   {"any": [ ... ]}   {"not": { ... }}
#   {"const": true|false|null}
#
# `fact` predicates read a FactStore (or any object exposing `.status`/`.value`).


class PredicateError(Exception):
    pass


@dataclass(frozen=True)
class FactView:
    """Minimal interface the predicate evaluator needs from a fact provider."""

    value: Any
    status: FactStatus

    @property
    def usable(self) -> bool:
        return self.status.usable


class FactProvider:
    """Adapter over a FactStore plus ad-hoc named values (policy flags, dates)."""

    def __init__(self, facts: Mapping[str, Any] | None = None,
                 store: Any | None = None) -> None:
        self._facts = dict(facts or {})
        self._store = store

    @property
    def store(self) -> Any | None:
        return self._store

    def set(self, fact_id: str, value: Any) -> None:
        """Named values and facts entered by the pipeline (not by a document reader)."""
        self._facts[fact_id] = value

    def set_many(self, values: Mapping[str, Any]) -> None:
        self._facts.update(values)

    def has(self, fact_id: str) -> bool:
        return self.view(fact_id).status.usable

    def view(self, fact_id: str) -> FactView:
        if self._store is not None:
            get = getattr(self._store, "get", None)
            if get is not None:
                f = get(fact_id)
                if f is not None:
                    return FactView(f.value, f.status)
        if fact_id in self._facts:
            v = self._facts[fact_id]
            if isinstance(v, FactView):
                return v
            if isinstance(v, Fact):
                return FactView(v.value, v.status)
            if v is None:
                return FactView(None, FactStatus.MISSING)
            if isinstance(v, Tri):
                # An explicitly unknown value is not a usable fact: a rule that tests it
                # must return UNKNOWN, never a comparison against a sentinel object.
                if v is Tri.UNKNOWN:
                    return FactView(None, FactStatus.MISSING)
                return FactView(v is Tri.TRUE, FactStatus.READ)
            return FactView(v, FactStatus.READ)
        return FactView(None, FactStatus.MISSING)


def _ordered_pair(a: Any, b: Any) -> tuple[Any, Any] | None:
    """Coerce only where coercion is obviously right; refuse mixed text/number pairs.

    Comparing an amount to the word "2021" is not a comparison: it is a mismatch that
    would otherwise answer FALSE, which is a wrong answer rather than a missing one.
    """
    try:
        if isinstance(a, str) != isinstance(b, str):
            text = a if isinstance(a, str) else b
            other = b if isinstance(a, str) else a
            if isinstance(other, (int, float, Fraction, bool)) or other is None:
                return None
            return (str(a), str(b)) if isinstance(a, str) else (a, b)
        if isinstance(a, str) and isinstance(b, str):
            return a, b
        if isinstance(a, Fraction) or isinstance(b, Fraction):
            return Fraction(a), Fraction(b)
        return a, b
    except (TypeError, ValueError):
        return None


def evaluate_predicate(pred: Mapping[str, Any] | None, provider: FactProvider) -> PredicateResult:
    """Evaluate a data predicate with three-valued semantics."""
    if pred is None:
        return PredicateResult(Tri.TRUE, ("no predicate",))
    if not isinstance(pred, Mapping) or len(pred) != 1:
        raise PredicateError(f"predicate must be a single-key mapping, got {pred!r}")

    key, arg = next(iter(pred.items()))

    if key == "const":
        return PredicateResult(tri_from_bool(arg))
    if key == "all":
        parts = [evaluate_predicate(p, provider) for p in arg]
        return PredicateResult(all_of(*(p.value for p in parts)),
                               tuple(r for p in parts for r in p.reasons))
    if key == "any":
        parts = [evaluate_predicate(p, provider) for p in arg]
        return PredicateResult(any_of(*(p.value for p in parts)),
                               tuple(r for p in parts for r in p.reasons))
    if key == "not":
        inner = evaluate_predicate(arg, provider)
        return PredicateResult(negate(inner.value), inner.reasons)
    if key == "fact":
        if not isinstance(arg, Mapping):
            raise PredicateError("fact predicate needs a mapping argument")
        fact_id = str(arg.get("fact_id") or arg.get("id") or "")
        op = str(arg.get("op", "present"))
        view = provider.view(fact_id)
        if op == "present":
            return PredicateResult(tri_from_bool(view.status.usable),
                                   (f"{fact_id}={view.value!r} ({view.status.value})",))
        if not view.status.usable:
            return PredicateResult(Tri.UNKNOWN,
                                   (f"{fact_id} is {view.status.value}",))
        expected = arg.get("value")
        got = view.value
        if op in ("eq", "ne", "gt", "ge", "lt", "le"):
            pair = _ordered_pair(got, expected)
            if pair is None:
                return PredicateResult(Tri.UNKNOWN, (f"{fact_id} not comparable",))
            a, b = pair
            try:
                result = {
                    "eq": a == b, "ne": a != b, "gt": a > b,
                    "ge": a >= b, "lt": a < b, "le": a <= b,
                }[op]
            except TypeError:
                return PredicateResult(Tri.UNKNOWN, (f"{fact_id} not comparable",))
            return PredicateResult(tri_from_bool(result), (f"{fact_id} {op} {expected!r}",))
        if op == "in":
            return PredicateResult(tri_from_bool(got in (expected or [])),
                                   (f"{fact_id} in {expected!r}",))
        if op == "contains":
            try:
                return PredicateResult(tri_from_bool(expected in got),
                                       (f"{fact_id} contains {expected!r}",))
            except TypeError:
                return PredicateResult(Tri.FALSE, (f"{fact_id} not container-like",))
        raise PredicateError(f"unsupported fact operator: {op!r}")

    raise PredicateError(f"unsupported predicate operator: {key!r}")


def collect_fact_refs(pred: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Every fact id a predicate depends on (used for evidence requirements)."""
    if not pred:
        return ()
    key, arg = next(iter(pred.items()))
    if key == "fact" and isinstance(arg, Mapping):
        fid = str(arg.get("fact_id") or arg.get("id") or "")
        return (fid,) if fid else ()
    if key in ("all", "any"):
        return tuple(f for p in arg for f in collect_fact_refs(p))
    if key == "not":
        return collect_fact_refs(arg)
    return ()
