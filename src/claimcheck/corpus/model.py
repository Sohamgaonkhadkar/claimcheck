"""The corpus model: a source document, its clauses, and the snapshot that pinned them.

The chain this package exists to make reproducible (milestone §2):

    PRIMARY SOURCE  ->  CORPUS DOCUMENT  ->  CLAUSE / SOURCE SPAN
        ->  RULE / CITATION  ->  CORPUS SNAPSHOT  ->  a reproducible run

Three deliberate omissions from the models, each of them a rule:

*   **No inferred fields.** ``SourceStatus`` is not guessed from the document's age, and
    ``effective_from`` is not back-filled from a circular's date. If the source does not
    say it, the field is ``None`` and the gap belongs in ``gaps``.
*   **No text that was not read.** A clause exists only where the builder *found* its
    anchor in a stored artifact. A document whose text has not been read (the item
    lists, OV-5) is a document with **zero clauses** and a named gap — not a document
    with plausible-looking clause text.
*   **No floats.** Money-like or ratio-like parameters in corpus metadata are strings or
    integers; the corpus is knowledge, not arithmetic.

``Instrument`` / ``RuleVersion`` in :mod:`claimcheck.rules.model` remain the only rule
model. This package adds a *pointer* (:class:`RuleSourceRef`, in the rule model) and the
documents it points at — not a second rule system.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Mapping, Sequence

from ..errors import CorpusProvenanceMissing
from . import hashing


class CorpusKind(str, enum.Enum):
    """The five knowledge bases of architecture §20.1. Corpus V0 fills two of them."""

    REGULATIONS = "regulations"     # public instruments: circulars, guidelines, regulations
    POLICIES = "policies"           # policy wordings (synthetic here; the case owns the real one)
    ITEMS = "items"                 # standardised non-payable item lists
    LEXICON = "lexicon"             # bill-head lexicon
    RUBRIC = "rubric"               # annotation rubric


class SourceType(str, enum.Enum):
    REGULATION = "regulation"
    GUIDELINE = "guideline"
    MASTER_CIRCULAR = "master_circular"
    CIRCULAR = "circular"
    FAQ = "faq"
    POLICY_WORDING = "policy_wording"
    ITEM_LIST = "item_list"
    LEXICON = "lexicon"
    RUBRIC = "rubric"


class SourceStatus(str, enum.Enum):
    """What is known about the document's standing — nothing more.

    ``SYNTHETIC`` is not a weaker ``IN_FORCE``: it records that the document is a
    synthetic fixture, which may be used to test the machinery and may never be cited
    as authority for a real finding.
    """

    IN_FORCE = "IN_FORCE"        # currently in force, on the basis recorded in status_basis
    SUPERSEDED = "SUPERSEDED"    # superseded, and the superseding instrument is named
    UNKNOWN = "UNKNOWN"          # standing not established
    VERIFY = "VERIFY"            # standing is *in question*: an open verification item
    SYNTHETIC = "SYNTHETIC"      # a fixture, never an authority


class RetrievalMethod(str, enum.Enum):
    PRIMARY_SITE_PAGE = "primary_site_page"      # the regulator's own document page render
    PRIMARY_SITE_PDF = "primary_site_pdf"        # the regulator's own PDF text layer
    MIRROR = "mirror"                            # a secondary host's copy of the text
    AUTHORED = "authored"                        # written for this project (synthetic only)


class ClauseType(str, enum.Enum):
    OBLIGATION = "obligation"        # "insurers shall ..."
    PROHIBITION = "prohibition"      # "shall not ..."
    DEFINITION = "definition"
    PROCEDURE = "procedure"
    APPLICABILITY = "applicability"
    GOVERNANCE = "governance"
    REPEAL = "repeal"
    GENERAL = "general"


#: Where a clause's relationship to another clause is recorded, these are the types.
CLAUSE_RELATIONS = ("restated_by", "refers_to", "refined_by", "subject_of")


@dataclass(frozen=True)
class CorpusDocument:
    """One source document as stored. Provenance is mandatory; ambiguity is explicit."""

    corpus_document_id: str
    corpus: CorpusKind

    # -- what it is ---------------------------------------------------------
    source_type: SourceType
    issuer: str
    title: str
    reference: str                       # the document's own number, or "" if it has none

    # -- when it applies ----------------------------------------------------
    issued_on: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    superseded_on: date | None = None
    superseded_by: str | None = None     # reference of the superseding instrument

    # -- where it came from -------------------------------------------------
    source_url: str = ""
    retrieved_at: date | None = None
    retrieval_method: RetrievalMethod = RetrievalMethod.PRIMARY_SITE_PAGE
    licence: str = ""
    synthetic: bool = False

    # -- what we know about its standing ------------------------------------
    source_status: SourceStatus = SourceStatus.UNKNOWN
    status_basis: str = ""               # *why* that status; mandatory
    coverage: str = ""                   # which part of the source the artifact contains
    gaps: tuple[str, ...] = ()           # verification gaps left open, named
    language: str = "en"

    # -- the immutable artifact ---------------------------------------------
    artifact_path: str | None = None     # repo-relative path of the stored text
    artifact_sha256: str | None = None   # sha256 of the stored text, verbatim

    notes: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)

    # -- validation ----------------------------------------------------------
    REQUIRED = ("corpus_document_id", "issuer", "title", "source_url", "licence",
                "status_basis", "coverage")

    def validate(self) -> None:
        """Refuse a document that cannot be traced. There is no default provenance."""
        missing = [name for name in self.REQUIRED if not str(getattr(self, name)).strip()]
        if self.retrieved_at is None:
            missing.append("retrieved_at")
        if self.source_status is not SourceStatus.SYNTHETIC and self.issued_on is None:
            missing.append("issued_on")
        if missing:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id or '<unnamed>'}: missing mandatory provenance: "
                + ", ".join(sorted(missing)),
                corpus_document_id=self.corpus_document_id, missing=sorted(missing),
            )
        if self.synthetic:
            if "synthetic" not in self.licence.lower():
                raise CorpusProvenanceMissing(
                    f"{self.corpus_document_id}: a synthetic document must say so in its "
                    f"licence line (got {self.licence!r})",
                    corpus_document_id=self.corpus_document_id,
                )
            if self.source_status is not SourceStatus.SYNTHETIC:
                raise CorpusProvenanceMissing(
                    f"{self.corpus_document_id}: a synthetic document cannot carry status "
                    f"{self.source_status.value}",
                    corpus_document_id=self.corpus_document_id,
                )
        elif self.source_status is SourceStatus.SYNTHETIC:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id}: status SYNTHETIC requires synthetic = true",
                corpus_document_id=self.corpus_document_id,
            )
        if self.source_status is SourceStatus.SUPERSEDED and not self.superseded_by:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id}: a SUPERSEDED document must name what superseded it",
                corpus_document_id=self.corpus_document_id,
            )
        if self.source_status is SourceStatus.VERIFY and not self.gaps:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id}: status VERIFY must name at least one open gap",
                corpus_document_id=self.corpus_document_id,
            )
        if self.effective_from and self.issued_on and self.effective_from < self.issued_on:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id}: effective_from precedes issued_on",
                corpus_document_id=self.corpus_document_id,
            )
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise CorpusProvenanceMissing(
                f"{self.corpus_document_id}: effective_to precedes effective_from",
                corpus_document_id=self.corpus_document_id,
            )

    # -- time ----------------------------------------------------------------
    def in_force_on(self, day: date) -> bool:
        """Whether the document *claims* to apply on ``day``. Standing is separate.

        A document with status VERIFY or UNKNOWN can still be ``in_force_on`` — this is
        the date arithmetic, not a statement of authority.
        """
        if self.effective_from and day < self.effective_from:
            return False
        if self.effective_to and day > self.effective_to:
            return False
        return True

    @property
    def authoritative(self) -> bool:
        """May a finding rest authority on this document?"""
        return self.source_status is SourceStatus.IN_FORCE and not self.synthetic

    # -- serialisation -------------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "corpus_document_id": self.corpus_document_id,
            "corpus": self.corpus.value,
            "source_type": self.source_type.value,
            "issuer": self.issuer,
            "title": self.title,
            "reference": self.reference,
            "issued_on": _iso(self.issued_on),
            "effective_from": _iso(self.effective_from),
            "effective_to": _iso(self.effective_to),
            "superseded_on": _iso(self.superseded_on),
            "superseded_by": self.superseded_by,
            "source_url": self.source_url,
            "retrieved_at": _iso(self.retrieved_at),
            "retrieval_method": self.retrieval_method.value,
            "licence": self.licence,
            "synthetic": self.synthetic,
            "source_status": self.source_status.value,
            "status_basis": self.status_basis,
            "coverage": self.coverage,
            "gaps": list(self.gaps),
            "language": self.language,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "notes": self.notes,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> "CorpusDocument":
        return cls(
            corpus_document_id=str(p["corpus_document_id"]),
            corpus=CorpusKind(p["corpus"]),
            source_type=SourceType(p["source_type"]),
            issuer=str(p["issuer"]),
            title=str(p["title"]),
            reference=str(p.get("reference", "")),
            issued_on=_date(p.get("issued_on")),
            effective_from=_date(p.get("effective_from")),
            effective_to=_date(p.get("effective_to")),
            superseded_on=_date(p.get("superseded_on")),
            superseded_by=p.get("superseded_by"),
            source_url=str(p.get("source_url", "")),
            retrieved_at=_date(p.get("retrieved_at")),
            retrieval_method=RetrievalMethod(p.get("retrieval_method", "primary_site_page")),
            licence=str(p.get("licence", "")),
            synthetic=bool(p.get("synthetic", False)),
            source_status=SourceStatus(p.get("source_status", "UNKNOWN")),
            status_basis=str(p.get("status_basis", "")),
            coverage=str(p.get("coverage", "")),
            gaps=tuple(p.get("gaps", ())),
            language=str(p.get("language", "en")),
            artifact_path=p.get("artifact_path"),
            artifact_sha256=p.get("artifact_sha256"),
            notes=str(p.get("notes", "")),
            provenance=dict(p.get("provenance", {})),
        )


@dataclass(frozen=True)
class CorpusClause:
    """A clause of a corpus document, located in the stored artifact by character offsets."""

    clause_id: str                       # "<corpus_document_id>#<label>"
    corpus_document_id: str
    label: str                           # "4", "17(b)", "A1.3"
    heading_path: str                    # "para 4", "Annexure-1 clause 3"
    text: str
    char_start: int
    char_end: int
    position: int
    clause_type: ClauseType = ClauseType.GENERAL
    page_number: int | None = None       # only when the source itself told us
    parent_clause_id: str | None = None
    relations: tuple[Mapping[str, str], ...] = ()
    references: tuple[str, ...] = ()     # instrument references this clause names
    notes: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        problems = []
        if not self.clause_id.strip() or "#" not in self.clause_id:
            problems.append("clause_id must be '<corpus_document_id>#<label>'")
        if not self.text.strip():
            problems.append("empty clause text")
        if self.char_end <= self.char_start:
            problems.append("char_end must exceed char_start")
        if "#" in self.clause_id and not self.clause_id.startswith(self.corpus_document_id + "#"):
            problems.append("clause_id does not belong to its corpus_document_id")
        if problems:
            raise CorpusProvenanceMissing(
                f"{self.clause_id or '<unnamed clause>'}: " + "; ".join(problems),
                clause_id=self.clause_id, problems=problems,
            )

    @property
    def text_sha256(self) -> str:
        return hashing.sha256_text(self.text)

    def payload(self) -> dict[str, Any]:
        """The clause as it is hashed. Text is hashed by content, not by object id."""
        return {
            "clause_id": self.clause_id,
            "corpus_document_id": self.corpus_document_id,
            "label": self.label,
            "heading_path": self.heading_path,
            "text_sha256": self.text_sha256,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "position": self.position,
            "clause_type": self.clause_type.value,
            "page_number": self.page_number,
            "parent_clause_id": self.parent_clause_id,
            "relations": [dict(r) for r in self.relations],
            "references": list(self.references),
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.payload(), "text": self.text, "notes": self.notes,
                "provenance": dict(self.provenance)}

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> "CorpusClause":
        return cls(
            clause_id=str(p["clause_id"]),
            corpus_document_id=str(p["corpus_document_id"]),
            label=str(p["label"]),
            heading_path=str(p["heading_path"]),
            text=str(p["text"]),
            char_start=int(p["char_start"]),
            char_end=int(p["char_end"]),
            position=int(p["position"]),
            clause_type=ClauseType(p.get("clause_type", "general")),
            page_number=int(p["page_number"]) if p.get("page_number") is not None else None,
            parent_clause_id=p.get("parent_clause_id"),
            relations=tuple(dict(r) for r in p.get("relations", ())),
            references=tuple(p.get("references", ())),
            notes=str(p.get("notes", "")),
            provenance=dict(p.get("provenance", {})),
        )

    def span(self) -> tuple[int, int]:
        return (self.char_start, self.char_end)


@dataclass(frozen=True)
class CorpusSnapshot:
    """An immutable, content-addressed pin of a set of documents and their clauses."""

    corpus_snapshot_id: str
    version_label: str                     # "v0.1"
    created_on: date
    document_ids: tuple[str, ...]
    document_hashes: Mapping[str, str]
    clause_counts: Mapping[str, int]
    clause_set_hash: str
    content_hash: str                      # the hash that the snapshot id carries
    rulepack_version: str = ""
    created_by: str = "corpus-builder"
    notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "corpus_snapshot_id": self.corpus_snapshot_id,
            "version_label": self.version_label,
            "created_on": self.created_on.isoformat(),
            "document_ids": list(self.document_ids),
            "document_hashes": dict(sorted(self.document_hashes.items())),
            "clause_counts": dict(sorted(self.clause_counts.items())),
            "clause_set_hash": self.clause_set_hash,
            "content_hash": self.content_hash,
            "rulepack_version": self.rulepack_version,
            "created_by": self.created_by,
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> "CorpusSnapshot":
        return cls(
            corpus_snapshot_id=str(p["corpus_snapshot_id"]),
            version_label=str(p["version_label"]),
            created_on=date.fromisoformat(str(p["created_on"])),
            document_ids=tuple(p["document_ids"]),
            document_hashes=dict(p["document_hashes"]),
            clause_counts={k: int(v) for k, v in p["clause_counts"].items()},
            clause_set_hash=str(p["clause_set_hash"]),
            content_hash=str(p["content_hash"]),
            rulepack_version=str(p.get("rulepack_version", "")),
            created_by=str(p.get("created_by", "corpus-builder")),
            notes=str(p.get("notes", "")),
            metadata=dict(p.get("metadata", {})),
        )

    def with_notes(self, **kw: Any) -> "CorpusSnapshot":
        return replace(self, **kw)


# ---------------------------------------------------------------------------
def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def clause_id_for(document_id: str, label: str) -> str:
    return f"{document_id}#{label}"


def documents_by_id(docs: Sequence[CorpusDocument]) -> dict[str, CorpusDocument]:
    return {d.corpus_document_id: d for d in docs}
