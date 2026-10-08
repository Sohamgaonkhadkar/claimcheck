"""Deterministic corpus ingestion: source artifact + clause anchors -> corpus records.

The builder takes what a human would take — a stored copy of the source, its
provenance, and a list of the clauses to index — and produces the typed records plus
the hashes that make them reproducible.

Deliberate design choices, each with a failure mode in mind:

*   **Clauses are located, never generated.** A clause declares an ``anchor``: a verbatim
    substring of the stored artifact. If the anchor is absent, or occurs more than once,
    ingestion *fails loudly*. There is no "best guess" clause text, because a guessed
    clause is indistinguishable from a fabricated one once it is in the corpus.
*   **The artifact's hash is recorded at ingestion**, and re-checked on every load. A
    source file edited after ingestion is a *new* source, and it is refused rather than
    silently absorbed.
*   **Missing provenance is a refusal.** No default issuer, no assumed licence, no
    today's date as ``retrieved_at``.
"""

from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from ..errors import CorpusAnchorNotFound, CorpusProvenanceMissing
from . import hashing
from .model import (
    ClauseType,
    CorpusClause,
    CorpusDocument,
    CorpusKind,
    RetrievalMethod,
    SourceStatus,
    SourceType,
    clause_id_for,
)

MANIFEST_NAME = "document.toml"
DEFAULT_ARTIFACT = "source.txt"


@dataclass(frozen=True)
class ClauseSpec:
    """One clause as declared in a document manifest: a label and a verbatim anchor.

    ``end_anchor`` matters when the stored artifact holds several *excerpts* of one source
    (a long PDF whose middle pages were not retrieved, say): without it, a clause's text
    would run into the next excerpt and silently attribute it. With it, the clause ends
    exactly where the source excerpt ends.
    """

    label: str
    heading_path: str
    anchor: str
    end_anchor: str | None = None
    clause_type: ClauseType = ClauseType.GENERAL
    page_number: int | None = None
    parent_label: str | None = None
    references: tuple[str, ...] = ()
    relations: tuple[Mapping[str, str], ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class DocumentSpec:
    """A document manifest: the typed header plus its clause table."""

    document: CorpusDocument
    clauses: tuple[ClauseSpec, ...]
    artifact_name: str = DEFAULT_ARTIFACT
    directory: str | None = None

    @property
    def has_artifact(self) -> bool:
        return bool(self.artifact_name)


@dataclass(frozen=True)
class IngestedDocument:
    """What ingestion produced for one document: the record, its clauses, its hash."""

    document: CorpusDocument
    clauses: tuple[CorpusClause, ...]
    artifact_sha256: str | None
    artifact_bytes: int

    @property
    def document_hash(self) -> str:
        return hashing.document_hash(
            self.document.to_payload(),
            [c.payload() for c in self.clauses],
            {"source_text": self.artifact_sha256} if self.artifact_sha256 else {},
        )

    @property
    def clause_set_hash(self) -> str:
        return hashing.clause_set_hash([c.payload() for c in self.clauses])


@dataclass(frozen=True)
class CorpusBuild:
    """A whole ingested corpus: documents, their clauses, and where they came from."""

    documents: tuple[IngestedDocument, ...]
    source_root: str
    artifacts: Mapping[str, str] = field(default_factory=dict)   # path -> sha256

    def by_id(self, document_id: str) -> IngestedDocument | None:
        for d in self.documents:
            if d.document.corpus_document_id == document_id:
                return d
        return None

    def clause(self, clause_id: str) -> CorpusClause | None:
        for d in self.documents:
            for c in d.clauses:
                if c.clause_id == clause_id:
                    return c
        return None

    def all_clauses(self) -> tuple[CorpusClause, ...]:
        return tuple(c for d in self.documents for c in d.clauses)

    @property
    def document_hashes(self) -> dict[str, str]:
        return {d.document.corpus_document_id: d.document_hash for d in self.documents}

    @property
    def clause_counts(self) -> dict[str, int]:
        return {d.document.corpus_document_id: len(d.clauses) for d in self.documents}

    def summary(self) -> dict[str, Any]:
        return {
            "documents": len(self.documents),
            "clauses": len(self.all_clauses()),
            "with_text": sum(1 for d in self.documents if d.clauses),
            "synthetic": sum(1 for d in self.documents if d.document.synthetic),
            "by_status": _count(d.document.source_status.value for d in self.documents),
            "by_corpus": _count(d.document.corpus.value for d in self.documents),
        }


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------
def load_document_spec(path: str | pathlib.Path) -> DocumentSpec:
    p = pathlib.Path(path)
    if p.is_dir():
        p = p / MANIFEST_NAME
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    head = data.get("document")
    if not isinstance(head, Mapping):
        raise CorpusProvenanceMissing(f"{p}: no [document] block", path=str(p))

    def need(key: str) -> str:
        v = head.get(key)
        if v is None or str(v).strip() == "":
            raise CorpusProvenanceMissing(f"{p}: [document] is missing '{key}'", path=str(p),
                                          key=key)
        return str(v)

    corpus = CorpusKind(need("corpus"))
    doc = CorpusDocument(
        corpus_document_id=need("corpus_document_id"),
        corpus=corpus,
        source_type=SourceType(need("source_type")),
        issuer=need("issuer"),
        title=need("title"),
        reference=str(head.get("reference", "")),
        issued_on=_date(head.get("issued_on")),
        effective_from=_date(head.get("effective_from")),
        effective_to=_date(head.get("effective_to")),
        superseded_on=_date(head.get("superseded_on")),
        superseded_by=head.get("superseded_by"),
        source_url=need("source_url"),
        retrieved_at=_date(need("retrieved_at")),
        retrieval_method=RetrievalMethod(str(head.get("retrieval_method", "primary_site_page"))),
        licence=need("licence"),
        synthetic=bool(head.get("synthetic", False)),
        source_status=SourceStatus(need("source_status")),
        status_basis=need("status_basis"),
        coverage=need("coverage"),
        gaps=tuple(str(g) for g in head.get("gaps", ())),
        language=str(head.get("language", "en")),
        artifact_path=str(head.get("artifact", DEFAULT_ARTIFACT)) or None,
        notes=str(head.get("notes", "")),
        provenance=dict(head.get("provenance", {})),
    )
    specs = tuple(_clause_spec(c, p) for c in data.get("clause", ()))
    labels = [c.label for c in specs]
    if len(set(labels)) != len(labels):
        raise CorpusAnchorNotFound(f"{p}: duplicate clause labels: {sorted(labels)}",
                                   path=str(p), labels=sorted(labels))
    artifact = str(head.get("artifact", DEFAULT_ARTIFACT)) if doc.source_type else ""
    if head.get("artifact", DEFAULT_ARTIFACT) is None or head.get("artifact") == "":
        artifact = ""
    if specs and not artifact:
        raise CorpusProvenanceMissing(
            f"{p}: {len(specs)} clause(s) declared but no artifact file to locate them in",
            path=str(p))
    return DocumentSpec(document=doc, clauses=specs, artifact_name=artifact, directory=str(p.parent))


def _clause_spec(m: Mapping[str, Any], path: pathlib.Path) -> ClauseSpec:
    for key in ("label", "anchor"):
        if not str(m.get(key, "")).strip():
            raise CorpusProvenanceMissing(f"{path}: a [[clause]] is missing '{key}'",
                                          path=str(path), key=key)
    return ClauseSpec(
        label=str(m["label"]),
        heading_path=str(m.get("heading_path", m["label"])),
        anchor=str(m["anchor"]),
        end_anchor=str(m["end_anchor"]) if m.get("end_anchor") else None,
        clause_type=ClauseType(str(m.get("clause_type", "general"))),
        page_number=int(m["page_number"]) if m.get("page_number") is not None else None,
        parent_label=m.get("parent_label"),
        references=tuple(str(r) for r in m.get("references", ())),
        relations=tuple(dict(r) for r in m.get("relations", ())),
        notes=str(m.get("notes", "")),
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def ingest_document(spec: DocumentSpec, *, source_root: str | pathlib.Path | None = None,
                    expect_artifact_sha256: str | None = None) -> IngestedDocument:
    """Turn a manifest and its stored artifact into typed corpus records.

    ``expect_artifact_sha256`` is the hash recorded when the artifact was published; if
    the file on disk no longer matches, ingestion refuses. That refusal is the only
    thing standing between a live corpus and a source edited underneath it.
    """
    doc = spec.document
    doc.validate()
    text = ""
    artifact_hash: str | None = None
    if spec.has_artifact:
        base = pathlib.Path(spec.directory or (source_root or "."))
        artifact_file = base / spec.artifact_name
        if not artifact_file.exists():
            raise CorpusProvenanceMissing(
                f"{doc.corpus_document_id}: artifact {artifact_file} not found",
                corpus_document_id=doc.corpus_document_id, path=str(artifact_file))
        raw = artifact_file.read_bytes()
        text = raw.decode("utf-8")
        artifact_hash = hashing.sha256_bytes(raw)
        if expect_artifact_sha256 and expect_artifact_sha256 != artifact_hash:
            from ..errors import CorpusImmutable
            raise CorpusImmutable(
                f"{doc.corpus_document_id}: the stored artifact has changed "
                f"(recorded {expect_artifact_sha256[:12]}, found {artifact_hash[:12]}); "
                f"the source must be ingested as a new snapshot, not absorbed into this one",
                corpus_document_id=doc.corpus_document_id,
                recorded=expect_artifact_sha256, found=artifact_hash)
    elif spec.clauses:
        raise CorpusProvenanceMissing(
            f"{doc.corpus_document_id}: clauses declared with no artifact",
            corpus_document_id=doc.corpus_document_id)

    clauses = _locate_clauses(doc, spec, text)
    doc = _with_artifact(doc, spec, artifact_hash)
    ingested = IngestedDocument(document=doc, clauses=clauses,
                                artifact_sha256=artifact_hash, artifact_bytes=len(text))
    if doc.artifact_sha256 and doc.artifact_sha256 != artifact_hash:
        raise CorpusProvenanceMissing(
            f"{doc.corpus_document_id}: declared artifact_sha256 does not match the stored file",
            corpus_document_id=doc.corpus_document_id)
    return ingested


def _with_artifact(doc: CorpusDocument, spec: DocumentSpec, artifact_hash: str | None) -> CorpusDocument:
    from dataclasses import replace
    path = None
    if spec.has_artifact and spec.directory:
        path = str(pathlib.Path(spec.directory) / spec.artifact_name)
    return replace(doc, artifact_path=path, artifact_sha256=artifact_hash)


def _locate_clauses(doc: CorpusDocument, spec: DocumentSpec, text: str) -> tuple[CorpusClause, ...]:
    """Find every declared anchor in the artifact and slice the clause text around it."""
    found: list[tuple[ClauseSpec, int]] = []
    for cs in spec.clauses:
        occurrences = text.count(cs.anchor)
        if occurrences == 0:
            raise CorpusAnchorNotFound(
                f"{doc.corpus_document_id}: anchor for clause '{cs.label}' not found in the "
                f"stored source: {cs.anchor[:80]!r}",
                corpus_document_id=doc.corpus_document_id, label=cs.label, anchor=cs.anchor)
        if occurrences > 1:
            raise CorpusAnchorNotFound(
                f"{doc.corpus_document_id}: anchor for clause '{cs.label}' occurs "
                f"{occurrences} times; an ambiguous anchor cannot index a clause",
                corpus_document_id=doc.corpus_document_id, label=cs.label,
                anchor=cs.anchor, occurrences=occurrences)
        found.append((cs, text.index(cs.anchor)))

    order = sorted(found, key=lambda t: t[1])
    out: list[CorpusClause] = []
    for i, (cs, start) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else len(text)
        if cs.end_anchor:
            end = _find_end_anchor(doc, cs, text, start, end)
        body = text[start:end]
        stripped = body.rstrip()
        clause = CorpusClause(
            clause_id=clause_id_for(doc.corpus_document_id, cs.label),
            corpus_document_id=doc.corpus_document_id,
            label=cs.label,
            heading_path=cs.heading_path,
            text=stripped,
            char_start=start,
            char_end=start + len(stripped),
            position=i,
            clause_type=cs.clause_type,
            page_number=cs.page_number,
            parent_clause_id=(clause_id_for(doc.corpus_document_id, cs.parent_label)
                              if cs.parent_label else None),
            relations=cs.relations,
            references=cs.references,
            notes=cs.notes,
            provenance={"artifact_sha256": doc.artifact_sha256,
                        "anchor": cs.anchor} if doc.artifact_sha256 else {"anchor": cs.anchor},
        )
        clause.validate()
        if text[clause.char_start:clause.char_end] != clause.text:
            raise CorpusAnchorNotFound(
                f"{clause.clause_id}: located text does not round-trip to its offsets",
                clause_id=clause.clause_id)
        out.append(clause)
    return tuple(out)


def _find_end_anchor(doc: CorpusDocument, cs: ClauseSpec, text: str,
                     start: int, default_end: int) -> int:
    """Where a clause ends, when the manifest says so explicitly."""
    occurrences = text.count(cs.end_anchor or "")
    if occurrences != 1:
        raise CorpusAnchorNotFound(
            f"{doc.corpus_document_id}: end_anchor for clause '{cs.label}' must occur exactly "
            f"once in the stored source; found {occurrences}",
            corpus_document_id=doc.corpus_document_id, label=cs.label,
            end_anchor=cs.end_anchor, occurrences=occurrences)
    end = text.index(cs.end_anchor)               # type: ignore[arg-type]
    if end <= start:
        raise CorpusAnchorNotFound(
            f"{doc.corpus_document_id}: end_anchor for clause '{cs.label}' precedes its anchor",
            corpus_document_id=doc.corpus_document_id, label=cs.label)
    return min(end, default_end)


# ---------------------------------------------------------------------------
def build_corpus(root: str | pathlib.Path, *, corpora: Sequence[str] | None = None) -> CorpusBuild:
    """Ingest every document manifest under ``root`` (``<root>/<corpus>/<slug>/document.toml``)."""
    root = pathlib.Path(root).resolve()
    if not root.exists():
        raise CorpusProvenanceMissing(f"corpus root {root} does not exist", path=str(root))
    paths = sorted(root.glob(f"*/*/{MANIFEST_NAME}"))
    if not paths:
        raise CorpusProvenanceMissing(
            f"no {MANIFEST_NAME} found under {root} at <corpus>/<document>/", path=str(root))
    docs: list[IngestedDocument] = []
    artifacts: dict[str, str] = {}
    for path in paths:
        spec = load_document_spec(path)
        if corpora and spec.document.corpus.value not in corpora:
            continue
        ingested = ingest_document(spec)
        ingested = _relativise(ingested, root)
        docs.append(ingested)
        if ingested.artifact_sha256 and ingested.document.artifact_path:
            artifacts[ingested.document.artifact_path] = ingested.artifact_sha256
    ids = [d.document.corpus_document_id for d in docs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CorpusProvenanceMissing(f"duplicate corpus_document_id(s): {dupes}", ids=dupes)
    return CorpusBuild(documents=tuple(docs), source_root=str(root), artifacts=artifacts)


def _relativise(ingested: IngestedDocument, root: pathlib.Path) -> IngestedDocument:
    """Store the artifact's path relative to the corpus root, so a snapshot is portable."""
    from dataclasses import replace
    path = ingested.document.artifact_path
    if not path:
        return ingested
    try:
        rel = pathlib.Path(path).resolve().relative_to(root)
    except ValueError:
        return ingested
    return replace(ingested, document=replace(ingested.document, artifact_path=str(rel)))


def _date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))
