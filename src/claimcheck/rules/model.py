"""The rule model: every rule is dated, sourced, quoted and versioned.

Five properties the model will not negotiate (architecture §13):
  * a rule carries its instrument, its paragraph and a **verbatim quote**;
  * it carries ``effective_from`` and can carry ``superseded_on``;
  * it carries the date on which we last checked the instrument against the primary
    source (``verified_on``) and how long that check stays good (``valid_for_days``);
  * it carries an explicit ``status`` — ``verified``, ``unverified``, ``contested`` or
    ``gated`` — and only ``verified`` may assert;
  * its ``kind`` records whether the rule comes from a public instrument or from the
    policy document itself, because those two have different force.

A rule that has not been checked recently does not become wrong; it becomes
**unassertable**, which is a different and much safer failure.
"""

from __future__ import annotations

import enum
import pathlib
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from ..logic import Tri


class RuleStatus(str, enum.Enum):
    VERIFIED = "verified"          # checked against the primary source within its window
    UNVERIFIED = "unverified"      # never checked, or the source was secondary only
    CONTESTED = "contested"        # primary sources or readings disagree
    GATED = "gated"                # an open verification question (OV-n) blocks it


class RuleKind(str, enum.Enum):
    """The vocabulary the pack uses, and nothing else.

    ``standardised`` means the instrument prescribes the practice (a product standard);
    ``contested`` means sources disagree; ``policy_dependent`` means the instrument
    requires the *policy* to state the parameter for the practice to be lawful at all.
    """

    STANDARDISED = "standardised"
    CONTESTED = "contested"
    POLICY_DEPENDENT = "policy_dependent"
    PROCEDURE = "procedure"            # a claims-handling obligation
    LAW = "law"                        # a statute or regulation
    POLICY_TERM = "policy_term"        # the policy's own wording
    MARKET_PRACTICE = "market_practice"  # observed practice: never a rule
    GUIDANCE = "guidance"              # guidance, not binding


#: A rule with one of these effects can decide money. Anything else may inform, never decide.
VERDICT_CAPABLE_EFFECTS = frozenset({
    "PROHIBIT_APPLICATION",       # the deduction may not be made at all
    "REQUIRE_SCOPE_LIMIT",        # the deduction is confined to a defined scope
    "REQUIRE_PARAMETER_PRESENT",  # the policy must state a parameter for it to exist
    "REQUIRE_CONDITION",          # the deduction is conditional on a stated fact
    "CAP_AMOUNT",                 # the deduction is capped by the instrument
    "BAR_GROUND",                 # a repudiation ground may not be used
    "ENTITLE_INTEREST",           # the claimant is entitled to interest
    "ENTITLE_REFUND",             # the claimant is entitled to a refund of money taken
})

#: Effects that bear on the *process* of a claim rather than on an amount.
PROCEDURAL_EFFECTS = frozenset({
    "REQUIRE_COMMUNICATION", "REQUIRE_TIMELINESS",
})

#: Effects that are recorded and shown, but never used to decide anything.
INFORMATIONAL_EFFECTS = frozenset({"INFORM_ONLY", "DISCLOSE_TERM"})


@dataclass(frozen=True)
class RuleSourceRef:
    """A pointer from a rule to the exact clause it was read from (corpus V0, §7).

    The rule model already carries *what* a rule says (``quote``) and *which document* it
    came from (``Instrument``). This adds the missing link: *where in that document*.

    It is deliberately a pointer and not a copy. The corpus holds the text, its hash and
    its snapshot; the rule holds the reference — so a rule cannot drift away from a
    source, and :mod:`claimcheck.corpus.link` can check the quote against the stored text
    at any time. ``corpus_snapshot_id`` is optional: a rule may be written against a
    clause before a snapshot exists, and the link report then says so.
    """

    corpus_document_id: str
    clause_id: str
    corpus_snapshot_id: str | None = None
    page: int | None = None

    def describe(self) -> str:
        bits = [self.clause_id]
        if self.corpus_snapshot_id:
            bits.append(self.corpus_snapshot_id)
        return " / ".join(bits)


@dataclass(frozen=True)
class Instrument:
    """The document a rule comes from. Provenance is not optional."""

    ref: str                     # e.g. IRDAI/HLT/REG/CIR/151/06/2020
    title: str
    issued_on: date | None = None
    url: str | None = None
    retrieved_at: date | None = None
    licence: str | None = None   # public instrument; never an insurer's copyrighted wording

    def describe(self) -> str:
        bits = [self.ref]
        if self.issued_on:
            bits.append(f"dated {self.issued_on.isoformat()}")
        return " / ".join(bits)


@dataclass(frozen=True)
class RuleVersion:
    rule_id: str
    version: int
    title: str
    instrument: Instrument
    citation: str                      # the paragraph ("para 4(b)", "clause 3.6")
    quote: str                         # verbatim, from the primary source
    effective_from: date
    kind: RuleKind
    effect: str
    scope: str = ""                    # prose scope, shown to the user
    status: RuleStatus = RuleStatus.UNVERIFIED
    superseded_on: date | None = None
    verified_on: date | None = None
    valid_for_days: int = 180
    applicability: Mapping[str, Any] = field(default_factory=dict)
    exceptions: tuple[Mapping[str, Any], ...] = ()
    conditions: tuple[Mapping[str, Any], ...] = ()
    evidence_requirement: tuple[str, ...] = ()
    verdict_effect: str = ""
    human_escalation: bool = False
    notes: str = ""
    tests: tuple[str, ...] = ()
    appliesto: tuple[str, ...] = ()
    source: RuleSourceRef | None = None    # the corpus clause this rule was read from

    # -- time -----------------------------------------------------------------
    def valid_until(self) -> date | None:
        if self.verified_on is None:
            return None
        return self.verified_on + timedelta(days=self.valid_for_days)

    def in_force_on(self, day: date) -> bool:
        if self.superseded_on is not None and day >= self.superseded_on:
            return False
        return day >= self.effective_from

    def verification_current(self, day: date) -> bool:
        until = self.valid_until()
        return until is not None and day <= until

    def assertable(self, day: date) -> bool:
        """May this rule decide money today?

        Requires: verified against the primary source, verification still current, the
        rule in force, and an effect that is capable of deciding anything at all.
        """
        if self.kind is RuleKind.MARKET_PRACTICE:
            return False
        if self.status is not RuleStatus.VERIFIED:
            return False
        if self.effect not in VERDICT_CAPABLE_EFFECTS:
            return False
        return self.in_force_on(day) and self.verification_current(day)

    def describe(self) -> str:
        return f"{self.rule_id} v{self.version} ({self.effect}, {self.status.value})"


@dataclass(frozen=True)
class RulePack:
    """A dated bundle of rule versions. Verdicts name the pack and version they used."""

    version: str
    released_on: date
    rules: tuple[RuleVersion, ...]
    source_path: str | None = None
    notes: str = ""

    def by_id(self, rule_id: str) -> RuleVersion | None:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None


# ---------------------------------------------------------------------------
_PACK_DIR = pathlib.Path(__file__).with_name("rulepacks")


def default_rulepack_path() -> pathlib.Path:
    return _PACK_DIR / "irdai_2026_10.toml"


def load_rulepack(path: str | pathlib.Path | None = None) -> RulePack:
    p = pathlib.Path(path) if path else default_rulepack_path()
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    rules = tuple(_rule_from_mapping(r, p) for r in data.get("rule", []))
    ids = [r.rule_id for r in rules]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate rule_id(s) in {p}: {sorted(dupes)}")
    head = data.get("rulepack") or data.get("pack") or {}
    if not head:
        raise ValueError(f"{p}: no [rulepack] block")
    return RulePack(
        version=str(head["version"]),
        released_on=date.fromisoformat(str(head["released_on"])),
        rules=rules,
        source_path=str(p),
        notes=str(head.get("notes", "")),
    )


load_default_rulepack = load_rulepack


def _rule_from_mapping(m: Mapping[str, Any], path: pathlib.Path) -> RuleVersion:
    inst = m.get("instrument", {})
    instrument = Instrument(
        ref=str(inst.get("ref", "")),
        title=str(inst.get("title", "")),
        issued_on=_date(inst.get("issued_on")) if inst.get("issued_on") else None,
        url=inst.get("url"),
        retrieved_at=_date(inst.get("retrieved_at")) if inst.get("retrieved_at") else None,
        licence=inst.get("licence"),
    )
    if not instrument.ref or not instrument.title:
        raise ValueError(f"{path}: rule {m.get('rule_id')} has no instrument block")
    quote = str(m.get("quote", "")).strip()
    status = _status(m)
    effect = str(m["effect"])
    if not quote and status is RuleStatus.VERIFIED and effect in VERDICT_CAPABLE_EFFECTS:
        # A verified rule that decides money must carry the words it decides on.
        raise ValueError(
            f"{path}: rule {m.get('rule_id')} is verdict-capable but has no verbatim quote"
        )
    return RuleVersion(
        rule_id=str(m["rule_id"]),
        version=int(m.get("version", 1)),
        title=str(m["title"]),
        instrument=instrument,
        citation=str(m["citation"]),
        quote=quote,
        effective_from=_date(m["effective_from"]),
        kind=RuleKind(m.get("kind", "standardised")),
        effect=str(m["effect"]),
        scope=str(m.get("scope", "")),
        status=_status(m),
        superseded_on=_date(m["superseded_on"]) if m.get("superseded_on") else None,
        verified_on=_date(m["verified_on"]) if m.get("verified_on") else None,
        valid_for_days=int(m.get("valid_for_days", 180)),
        applicability=dict(m.get("applicability", {})),
        exceptions=tuple(m.get("exception", [])),
        conditions=_predicate_list(m.get("condition")),
        evidence_requirement=tuple(m.get("evidence_requirement", [])),
        verdict_effect=str(m.get("verdict_effect", "")),
        human_escalation=bool(m.get("human_escalation", False)),
        notes=str(m.get("notes", "")),
        tests=tuple(m.get("tests", [])),
        appliesto=tuple(m.get("appliesto", [])),
        source=_source_ref(m.get("source")),
    )


def _source_ref(v: Any) -> RuleSourceRef | None:
    """``[rule.source]`` is optional: a rule without one still loads (backward compatible)."""
    if not v:
        return None
    if not isinstance(v, Mapping):
        raise ValueError("[rule.source] must be a table with corpus_document_id and clause_id")
    doc = str(v.get("corpus_document_id", "")).strip()
    clause = str(v.get("clause_id", "")).strip()
    if not doc or not clause:
        raise ValueError(
            "[rule.source] needs both corpus_document_id and clause_id; a half-link is not "
            "a link")
    return RuleSourceRef(
        corpus_document_id=doc,
        clause_id=clause,
        corpus_snapshot_id=(str(v["corpus_snapshot_id"]) if v.get("corpus_snapshot_id") else None),
        page=int(v["page"]) if v.get("page") is not None else None,
    )


def _predicate_list(v: Any) -> tuple[Mapping[str, Any], ...]:
    """TOML gives one predicate as a table and several as an array of tables."""
    if v is None or v == [] or v == {}:
        return ()
    if isinstance(v, Mapping):
        return (dict(v),)
    return tuple(dict(x) for x in v)


def _status(m: Mapping[str, Any]) -> RuleStatus:
    """A ``kind = "contested"`` rule *is* a contested rule: one field, one meaning."""
    if m.get("kind") == "contested":
        return RuleStatus.CONTESTED
    return RuleStatus(m.get("status", "verified"))


def _date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def verdict_capable(rule: RuleVersion, day: date) -> bool:
    return rule.assertable(day)


def pack_summary(pack: RulePack) -> dict[str, int]:
    out = {s.value: 0 for s in RuleStatus}
    for r in pack.rules:
        out[r.status.value] += 1
    out["total"] = len(pack.rules)
    return out


def assert_no_unverified_verdict_rule(pack: RulePack, day: date) -> None:
    """A pack may contain unverified rules; it may not *silently* use them.

    Any rule whose status is verified but whose verification window has lapsed is
    demoted in memory, so a stale pack degrades into unassertable rules instead of
    into confident wrong ones.
    """
    stale = [r.rule_id for r in pack.rules
             if r.status is RuleStatus.VERIFIED and not r.verification_current(day)]
    if stale:
        raise ValueError(
            "rule pack verification has lapsed for: " + ", ".join(sorted(stale))
        )
