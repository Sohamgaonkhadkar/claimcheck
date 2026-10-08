"""Core schema types shared across CLAIMCHECK.

Design rules encoded here (Phase-2 architecture §8, §21, §36):

* Money is **integer paise** (``Money = int``). Never a float, never a decimal string.
* Every value that can decide money carries provenance: a document, a page, a span,
  the raw text as printed, an origin and a verification status.
* A fact is never "just a value". Its *status* (READ / INFERRED / MISSING /
  CONFLICTED / QUARANTINED) drives the rule engine's three-valued logic and the
  verdict gates.
* Nothing in this module imports a model, a network client or a document parser.
  This is the trusted domain's type system.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable, Literal, Mapping, Sequence

# ---------------------------------------------------------------------------
# Primitive aliases
# ---------------------------------------------------------------------------
Money = int  # integer paise. 100 paise = 1 rupee.
Pct = Fraction  # percentages as exact rationals (10% == Fraction(1, 10))
Ratio = Fraction  # dimensionless ratios


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class DocRole(str, Enum):
    POLICY_WORDING = "policy_wording"
    POLICY_SCHEDULE = "policy_schedule"
    CIS = "cis"
    BILL = "bill"
    SETTLEMENT = "settlement"
    DISCHARGE = "discharge_summary"
    PREAUTH = "preauth"
    CLAIM_FORM = "claim_form"
    BANK = "bank_advice"
    OTHER = "other"


class Origin(str, Enum):
    """Where a fact came from. ``USER`` facts are assertions, not evidence."""

    PRINTED = "printed"  # read from a document span
    DERIVED = "derived"  # computed from other facts by code
    INFERRED = "inferred"  # code-derived but not printed anywhere
    USER = "user"  # asserted by the user


class FactStatus(str, Enum):
    READ = "READ"
    INFERRED = "INFERRED"
    MISSING = "MISSING"
    CONFLICTED = "CONFLICTED"
    QUARANTINED = "QUARANTINED"

    @property
    def usable(self) -> bool:
        return self in (FactStatus.READ, FactStatus.INFERRED)


class VerifyMethod(str, Enum):
    EXACT = "exact"
    NORMALISED = "normalised"
    FUZZY = "fuzzy"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"

    @property
    def accepted(self) -> bool:
        return self in (VerifyMethod.EXACT, VerifyMethod.NORMALISED, VerifyMethod.FUZZY)


class CanonicalCategory(str, Enum):
    """Closed category set (architecture §18.2). Versions of this set are data."""

    ROOM = "room"
    ICU = "icu"
    NURSING = "nursing"
    SURGEON = "surgeon"
    ANAESTHESIA = "anaesthesia"
    OT_CHARGES = "ot_charges"
    CONSULTATION = "consultation"
    PHARMACY = "pharmacy"
    CONSUMABLES = "consumables"
    IMPLANTS = "implants"
    MEDICAL_DEVICES = "medical_devices"
    DIAGNOSTICS = "diagnostics"
    IMAGING = "imaging"
    PROCEDURES = "procedures"
    BLOOD = "blood"
    PHYSIOTHERAPY = "physiotherapy"
    DIET = "diet"
    DOCUMENTATION = "documentation"
    ADMINISTRATION = "administration"
    PACKAGE = "package"
    AMBULANCE = "ambulance"
    MISCELLANEOUS = "miscellaneous"
    UNMAPPED = "UNMAPPED"


class MappingSource(str, Enum):
    USER_OVERRIDE = "user_override"
    EXACT = "exact"
    ALIAS = "alias"
    RULE = "rule"
    EMBEDDING = "embedding"
    MODEL = "model"
    NONE = "none"


class OrderProvenance(str, Enum):
    """How the position of a step in the calculation was established."""

    STATED = "stated"
    IMPLIED = "implied"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"
    DEMO_ONLY = "demo_only"


class ReadingId(str, Enum):
    """Named readings/variants, so a number is always attributable to one."""

    PRIMARY = "R0"
    ALT_SCOPE_NARROW = "R1"  # e.g. AME scope = room rent only
    ALT_ORDER_COPAY_AFTER = "R2"
    ALT_ORDER_COPAY_BEFORE = "R3"


class PageQualityTier(str, Enum):
    ADEQUATE = "adequate"  # score >= 0.8
    LOW = "low"  # 0.5 <= score < 0.8
    CRITICAL = "critical"  # score < 0.5 on a load-bearing region


# ---------------------------------------------------------------------------
# Documents, pages, evidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Page:
    page_id: str
    document_id: str
    page_number: int  # 1-based
    text: str
    width_px: int | None = None
    height_px: int | None = None
    quality_score: float = 1.0
    quality_codes: tuple[str, ...] = ()
    text_layer_used: bool = True

    @property
    def quality_tier(self) -> PageQualityTier:
        if self.quality_score >= 0.8:
            return PageQualityTier.ADEQUATE
        if self.quality_score >= 0.5:
            return PageQualityTier.LOW
        return PageQualityTier.CRITICAL


@dataclass(frozen=True)
class Document:
    document_id: str
    case_id: str
    role: DocRole
    media_type: str
    sha256: str
    byte_size: int
    object_key: str
    page_count: int
    classification_score: float = 1.0
    source_url: str | None = None
    licence: str | None = None

    @classmethod
    def fingerprint(cls, data: bytes) -> str:
        return sha256_bytes(data)


@dataclass(frozen=True)
class ExtractionMeta:
    """Who produced a value, with what, and how sure."""

    method: str  # 'text_layer', 'regex', 'table_parse', 'vlm', 'user', 'derived'
    engine: str = "deterministic"
    engine_version: str = "0"
    prompt_id: str | None = None
    model_version: str | None = None
    confidence: float = 1.0
    dual_read: bool = False
    reads_agree: bool | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0, 1]")


@dataclass(frozen=True)
class Evidence:
    """A verified span of a source document. The atom of the trusted domain.

    ``quoted_text`` must be an exact (or normalised/fuzzy-verified) substring of the
    page text. An ``Evidence`` whose ``verified`` is ``REJECTED`` may never be used
    to support a finding.
    """

    evidence_id: str
    document_id: str
    document_role: DocRole
    page_number: int
    quoted_text: str
    page_text_hash: str
    char_start: int | None = None
    char_end: int | None = None
    bbox: tuple[tuple[float, float], ...] | None = None
    verified: VerifyMethod = VerifyMethod.REJECTED
    verify_score: float = 0.0
    extraction: ExtractionMeta = field(default_factory=lambda: ExtractionMeta(method="unknown"))
    quality_score: float | None = None
    note: str | None = None
    injection_flags: tuple[str, ...] = ()

    @property
    def is_trustworthy(self) -> bool:
        return self.verified.accepted

    @property
    def verify_method(self) -> str:
        """How the span was located: exact, normalised, fuzzy, ambiguous or rejected."""
        return self.verified.value

    @property
    def verification_status(self) -> str:
        if self.verified.accepted:
            return "VERIFIED"
        if self.verified is VerifyMethod.AMBIGUOUS:
            return "AMBIGUOUS"
        return "REJECTED"

    @property
    def extraction_method(self) -> str:
        return self.extraction.method


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
@dataclass
class Fact:
    """A typed assertion about a document, or a value derived from assertions.

    ``raw_text`` is preserved for every fact that came from a page: when a number is
    disputed, the only defensible artefact is what the page actually printed.
    """

    fact_id: str
    type: str
    value: Any
    unit: str = "inr_paise"
    origin: Origin = Origin.PRINTED
    status: FactStatus = FactStatus.READ
    confidence: float = 1.0
    evidence_id: str | None = None
    raw_text: str | None = None
    derived_from: tuple[str, ...] = ()
    inference_note: str | None = None
    quarantine_reason: str | None = None
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.status.usable

    def as_money(self) -> Money:
        if self.unit != "inr_paise":
            raise ValueError(f"fact {self.fact_id} is not a money fact (unit={self.unit})")
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise ValueError(f"fact {self.fact_id} money value must be integer paise")
        return self.value

    def as_ratio(self) -> Ratio:
        if isinstance(self.value, Fraction):
            return self.value
        raise ValueError(f"fact {self.fact_id} is not a ratio")


@dataclass
class PolicyFacts:
    """Structured policy facts for one policy (schedule + wording).

    Every monetary/limit field is a ``Fact`` so its provenance travels with it.
    ``ame_definition_present`` is deliberately tri-state: True / False / None(missing).
    """

    policy_id: str
    insurer: str
    product_name: str
    policy_era: str
    documents: dict[str, str] = field(default_factory=dict)  # role -> document_id
    uin: str | None = None
    inception_date: date | None = None
    renewal_date: date | None = None
    claim_date: date | None = None
    sum_insured: Fact | None = None
    room_rent_limit: Fact | None = None  # per-day amount form
    icu_limit: Fact | None = None
    co_pay_percent: Fact | None = None
    co_pay_base_stated: bool = False
    co_pay_applies_to: tuple[CanonicalCategory, ...] = ()
    deductible: Fact | None = None
    sub_limits: tuple[SubLimit, ...] = ()
    waiting_periods: tuple[Fact, ...] = ()
    ame_definition_present: bool | None = None
    ame_heads: tuple[CanonicalCategory, ...] = ()
    ame_definition_evidence: str | None = None
    non_payable_items: tuple[NonPayableItem, ...] = ()
    calc_method_note: str | None = None
    stated_order: tuple[str, ...] = ()  # step_type names, in the order the policy states
    clauses: dict[str, PolicyClause] = field(default_factory=dict)
    moratorium_months: int | None = None

    def facts(self) -> list[Fact]:
        out: list[Fact] = []
        for f in (
            self.sum_insured,
            self.room_rent_limit,
            self.icu_limit,
            self.co_pay_percent,
            self.deductible,
            *self.waiting_periods,
        ):
            if f is not None:
                out.append(f)
        return out


@dataclass(frozen=True)
class SubLimit:
    """A named cap. ``basis`` matters (per procedure / per year / per eye)."""

    name: str
    amount: Fact
    basis: str = "per_procedure"
    applies_to: tuple[CanonicalCategory, ...] = ()


@dataclass(frozen=True)
class NonPayableItem:
    """An item-level rule sourced from the *policy document* or an annexure.

    The architecture forbids ``item -> true/false``. An item is a state plus
    conditions plus a source (architecture §13.6).
    """

    name: str
    aliases: tuple[str, ...]
    stance: Literal["NOT_PAYABLE", "PAYABLE", "CONDITIONAL", "SUBSUMED"]
    source: Literal["policy_document", "insurer_annexure", "irdai_list"]
    source_span: str | None = None  # evidence_id of the printed line
    subsumed_into: str | None = None
    conditions: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True)
class PolicyClause:
    """A clause inside the user's own policy document (the primary source)."""

    clause_id: str
    heading_path: str
    text: str
    evidence_id: str | None = None
    clause_type: str = "general"
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class BillLine:
    line_id: str
    raw_description: str
    amount: Money
    category: CanonicalCategory = CanonicalCategory.UNMAPPED
    mapping_source: MappingSource = MappingSource.NONE
    mapping_confidence: float = 0.0
    quantity: Fraction | None = None
    unit_rate: Money | None = None
    service_group: str | None = None
    evidence_id: str | None = None
    raw_amount_text: str | None = None
    is_package: bool = False

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("bill line amount cannot be negative")


@dataclass(frozen=True)
class BillFacts:
    bill_id: str
    lines: tuple[BillLine, ...]
    bill_total: Money
    document_id: str | None = None
    admission_date: date | None = None
    discharge_date: date | None = None
    room_days: int | None = None
    room_rate: Money | None = None
    room_category: str | None = None
    hospital_name: str | None = None

    def lines_in(self, categories: Iterable[CanonicalCategory]) -> tuple[BillLine, ...]:
        wanted = set(categories)
        return tuple(l for l in self.lines if l.category in wanted)

    def sum_of(self, categories: Iterable[CanonicalCategory]) -> Money:
        return sum(l.amount for l in self.lines_in(categories))


@dataclass(frozen=True)
class SettlementDeduction:
    head: str
    amount: Money
    raw_head_text: str | None = None
    ratio_stated: Ratio | None = None
    clause_cited: str | None = None
    reason_text: str | None = None
    evidence_id: str | None = None


@dataclass(frozen=True)
class SettlementFacts:
    settlement_id: str
    claimed_amount: Money
    deductions: tuple[SettlementDeduction, ...]
    final_payable: Money
    document_id: str | None = None
    admitted_amount: Money | None = None
    decision_date: date | None = None
    intimation_date: date | None = None
    last_document_date: date | None = None
    payment_date: date | None = None
    repudiated: bool = False
    partial_disallowance: bool = True
    cites_specific_policy_terms: bool | None = None
    demands_documents_from_policyholder: bool | None = None
    includes_ombudsman_details: bool | None = None
    cis_provided: bool | None = None

    def total_deductions(self) -> Money:
        return sum(d.amount for d in self.deductions)

    def deduction_for(self, head_key: str) -> SettlementDeduction | None:
        key = head_key.strip().lower()
        for d in self.deductions:
            if key in d.head.strip().lower() or key in (d.raw_head_text or "").lower():
                return d
        return None


@dataclass(frozen=True)
class CaseDocuments:
    """The three documents a case needs, by role."""

    policy_wording: str | None = None
    policy_schedule: str | None = None
    bill: str | None = None
    settlement: str | None = None
    discharge: str | None = None
    cis: str | None = None
    origin: Literal["synthetic", "consented", "public"] = "synthetic"

    def missing_for_financial_verdict(self) -> tuple[str, ...]:
        missing = []
        if not (self.policy_wording or self.policy_schedule):
            missing.append("policy_wording_or_schedule")
        if not self.bill:
            missing.append("bill")
        if not self.settlement:
            missing.append("settlement")
        return tuple(missing)


# ---------------------------------------------------------------------------
# Fact store
# ---------------------------------------------------------------------------
class FactStore:
    """Lookup over facts with three-valued semantics.

    A predicate asking for a fact that is MISSING gets ``None`` (INDETERMINATE),
    never a default. This is the mechanism that keeps uncertainty from turning
    into a confident verdict.
    """

    def __init__(self, facts: Sequence[Fact] = ()) -> None:
        self._facts: dict[str, Fact] = {}
        for f in facts:
            self.add(f)

    def add(self, fact: Fact) -> None:
        self._facts[fact.fact_id] = fact

    def get(self, fact_id: str) -> Fact | None:
        return self._facts.get(fact_id)

    def value(self, fact_id: str) -> Any | None:
        f = self._facts.get(fact_id)
        if f is None or not f.usable:
            return None
        return f.value

    def status(self, fact_id: str) -> FactStatus:
        f = self._facts.get(fact_id)
        return f.status if f else FactStatus.MISSING

    def all(self) -> tuple[Fact, ...]:
        return tuple(self._facts.values())

    def load_bearing_ids(self) -> set[str]:
        return {f.fact_id for f in self._facts.values() if f.type.startswith("loadbearing.")}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d")


def looks_numeric(text: str) -> bool:
    return bool(_NUM_RE.search(text))
