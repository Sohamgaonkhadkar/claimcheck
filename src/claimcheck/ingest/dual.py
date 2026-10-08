"""The dual-extraction contract (Phase 3D §11).

Two deterministic readers read every page (see :mod:`pdf`). This module decides what may
leave ingestion as a *usable monetary candidate*. The rules are the ones specified, and
none of them can be shortcut:

===============================  ==========================  ===================
situation                        disposition                 magnitude released
===============================  ==========================  ===================
both readers read the same value  ``USABLE``                  yes
readers produce different values  ``QUARANTINED``             **no**
only one reader saw the fact      ``QUARANTINED``             **no**
a reader saw it but could not
read it unambiguously             ``QUARANTINED``             **no**
no reader saw it at all           ``EVIDENCE_GAP``            **no**
===============================  ==========================  ===================

The forbidden move is the tempting one: picking the value that "looks more plausible".
``DualMoneyFact.paise`` is therefore ``None`` unless the readers actually agreed, and the
competing values are carried alongside as *readings* for a human to see.

Reader independence is not claimed where it does not exist. On a born-digital page the two
readers are genuinely different code paths (geometry-ordered character boxes versus the
producer's content-stream order). On a scanned page both are Tesseract, at two different
render resolutions: that catches resolution-dependent digit errors — the exact failure class
Phase 3C measured — but it does not catch an error that both resolutions make. The report
states this rather than implying the contract is stronger than it is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from .records import MoneyReading


class Agreement(str, Enum):
    AGREE = "agree"
    DISAGREE = "disagree"
    SINGLE_READER = "single_reader"
    UNREADABLE = "unreadable"
    NO_READER = "no_reader"


class FactDisposition(str, Enum):
    USABLE = "usable"
    QUARANTINED = "quarantined"
    EVIDENCE_GAP = "evidence_gap"


@dataclass(frozen=True)
class DualMoneyFact:
    """A monetary fact with the full account of how it was read."""

    fact_id: str
    subject: str                       # what the fact is about: a line id, a total kind, ...
    rule: str                          # which reading rule the readers were asked to apply
    readings: tuple[MoneyReading, ...]
    agreement: Agreement
    disposition: FactDisposition
    paise: int | None = None           # populated *only* when the readers agreed
    reasons: tuple[str, ...] = ()
    competing_paise: tuple[int, ...] = ()

    @property
    def usable(self) -> bool:
        return self.disposition is FactDisposition.USABLE

    @property
    def readers(self) -> tuple[str, ...]:
        return tuple(r.reader for r in self.readings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "rule": self.rule,
            "agreement": self.agreement.value,
            "disposition": self.disposition.value,
            "paise": self.paise,
            "competing_paise": list(self.competing_paise),
            "reasons": list(self.reasons),
            "readings": [
                {"reader": r.reader, "rule": r.rule, "raw": r.raw, "paise": r.paise,
                 "format": r.money_format.value, "alternative_paise": r.alternative_paise,
                 "flags": list(r.flags),
                 "span": r.evidence.span_id if r.evidence else None}
                for r in self.readings],
        }


def _fact_id(subject: str, rule: str, readings: Sequence[MoneyReading]) -> str:
    material = "|".join([subject, rule] + [f"{r.reader}:{r.raw}:{r.paise}" for r in readings])
    return "MF-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12].upper()


def reconcile_money(subject: str, rule: str,
                    readings: Iterable[MoneyReading]) -> DualMoneyFact:
    """Apply the contract to one monetary fact read by several readers."""
    readings = tuple(readings)
    values = [r.paise for r in readings if r.paise is not None]
    ambiguous = [r for r in readings if r.paise is None and (r.flags or r.raw)]
    reasons: list[str] = []

    if not readings:
        return DualMoneyFact(fact_id=_fact_id(subject, rule, ()), subject=subject, rule=rule,
                             readings=(), agreement=Agreement.NO_READER,
                             disposition=FactDisposition.EVIDENCE_GAP,
                             reasons=("no reader produced a reading",))

    if not values:
        reasons.append("no reader could read the fact unambiguously")
        for r in ambiguous:
            if r.flags:
                reasons.append(f"{r.reader}: {', '.join(r.flags)}")
        return DualMoneyFact(fact_id=_fact_id(subject, rule, readings), subject=subject,
                             rule=rule, readings=readings,
                             agreement=Agreement.UNREADABLE,
                             disposition=(FactDisposition.EVIDENCE_GAP if not ambiguous
                                          else FactDisposition.QUARANTINED),
                             reasons=tuple(reasons))

    distinct = sorted(set(values))
    if len(distinct) > 1:
        reasons.append("readers disagree: " + ", ".join(
            f"{r.reader}={r.paise}" for r in readings if r.paise is not None))
        return DualMoneyFact(fact_id=_fact_id(subject, rule, readings), subject=subject,
                             rule=rule, readings=readings, agreement=Agreement.DISAGREE,
                             disposition=FactDisposition.QUARANTINED,
                             competing_paise=tuple(distinct), reasons=tuple(reasons))

    raw_forms = {r.raw for r in readings if r.paise is not None}
    if len(raw_forms) > 1:
        reasons.append("readers agree on the value but not on how it is printed: "
                       + ", ".join(sorted(raw_forms)))

    if len(values) < len(readings):
        # Some reader could not read it, even though another could. Refuse.
        weak = [r.reader for r in readings if r.paise is None]
        reasons.append("read by only some readers; unreadable to " + ", ".join(sorted(weak)))
        return DualMoneyFact(fact_id=_fact_id(subject, rule, readings), subject=subject,
                             rule=rule, readings=readings,
                             agreement=Agreement.SINGLE_READER,
                             disposition=FactDisposition.QUARANTINED, paise=None,
                             reasons=tuple(reasons))

    if len(readings) < 2:
        reasons.append("only one reader saw this fact; the contract requires two")
        return DualMoneyFact(fact_id=_fact_id(subject, rule, readings), subject=subject,
                             rule=rule, readings=readings,
                             agreement=Agreement.SINGLE_READER,
                             disposition=FactDisposition.QUARANTINED, paise=None,
                             reasons=tuple(reasons))

    return DualMoneyFact(fact_id=_fact_id(subject, rule, readings), subject=subject,
                         rule=rule, readings=readings, agreement=Agreement.AGREE,
                         disposition=FactDisposition.USABLE, paise=distinct[0],
                         reasons=tuple(reasons))


def partition(facts: Iterable[DualMoneyFact]) -> dict[str, list[DualMoneyFact]]:
    """Group facts by disposition — the shape every report and test needs."""
    out: dict[str, list[DualMoneyFact]] = {d.value: [] for d in FactDisposition}
    for fact in facts:
        out[fact.disposition.value].append(fact)
    return out


@dataclass
class Quarantine:
    """Everything ingestion refused to release, with the reason it refused."""

    facts: list[DualMoneyFact] = field(default_factory=list)

    def add(self, fact: DualMoneyFact) -> None:
        if fact.disposition is not FactDisposition.USABLE:
            self.facts.append(fact)

    @property
    def count(self) -> int:
        return len(self.facts)

    def by_agreement(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.facts:
            out[f.agreement.value] = out.get(f.agreement.value, 0) + 1
        return dict(sorted(out.items()))

    def as_dicts(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.facts]
