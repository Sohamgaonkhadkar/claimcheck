"""CLAIMCHECK corpus: sourced, versioned knowledge with a reproducible provenance chain.

    PRIMARY SOURCE -> CORPUS DOCUMENT -> CLAUSE/SPAN -> RULE LINK -> SNAPSHOT -> RUN

Corpus V0 (milestone §16). What this package does:

*   stores the regulatory instruments this project has actually **read**, with their
    provenance, their standing (including the standing that is *unresolved*) and the open
    verification gaps named rather than closed;
*   indexes clauses by locating verbatim anchors in the stored artifact — never by
    generating text;
*   publishes immutable, content-addressed snapshots;
*   joins rules to the clauses they were read from and checks their quotations against
    the stored text deterministically;
*   keeps synthetic policy wordings clearly labelled and non-authoritative.

What it deliberately does not do (later milestones): parsing PDFs, OCR, embeddings,
retrieval ranking, or storing any real insurer's copyrighted wording.

Quick start::

    from claimcheck.corpus import build_corpus, FilesystemCorpusRepository

    build = build_corpus("data/corpus")
    repo = FilesystemCorpusRepository("data/corpus")
    snapshot = repo.publish(build, version_label="v0.1", created_on=date(2026, 10, 3))
    view = repo.open(snapshot.corpus_snapshot_id)        # what a run would pin
"""

from .builder import (
    ClauseSpec,
    CorpusBuild,
    DocumentSpec,
    IngestedDocument,
    build_corpus,
    ingest_document,
    load_document_spec,
)
from .hashing import canonical_json, clause_set_hash, document_hash, hash_payload, short
from .link import (
    RuleLink,
    RuleLinkReport,
    explain_provenance,
    clause_ids_in,
    link_pack,
    link_rule,
    provenance_for_rule,
)
from .model import (
    CLAUSE_RELATIONS,
    ClauseType,
    CorpusClause,
    CorpusDocument,
    CorpusKind,
    CorpusSnapshot,
    RetrievalMethod,
    SourceStatus,
    SourceType,
    clause_id_for,
)
from .repository import (
    CorpusRepository,
    FilesystemCorpusRepository,
    InMemoryCorpusRepository,
    default_snapshot_root,
)
from .snapshot import (
    LoadedSnapshot,
    SnapshotVerification,
    make_snapshot,
    read_snapshot,
    snapshot_id_for,
    verify_snapshot_disk,
    write_snapshot,
)
from .verify import (
    ACCEPTED_METHODS,
    QuoteCheck,
    best_clause,
    check_quote,
    check_quote_in_clause,
    explain_differences,
)

__all__ = [
    "ACCEPTED_METHODS", "CLAUSE_RELATIONS", "ClauseSpec", "ClauseType", "CorpusBuild",
    "CorpusClause", "CorpusDocument", "CorpusKind", "CorpusRepository", "CorpusSnapshot",
    "DocumentSpec", "FilesystemCorpusRepository", "InMemoryCorpusRepository",
    "IngestedDocument", "LoadedSnapshot", "QuoteCheck", "RetrievalMethod", "RuleLink",
    "RuleLinkReport", "SnapshotVerification", "SourceStatus", "SourceType", "best_clause",
    "build_corpus", "canonical_json", "check_quote", "check_quote_in_clause",
    "clause_id_for", "clause_set_hash", "default_snapshot_root", "document_hash",
    "explain_differences", "explain_provenance", "hash_payload", "ingest_document",
    "clause_ids_in", "link_pack", "link_rule", "load_document_spec", "make_snapshot", "provenance_for_rule",
    "read_snapshot", "short", "snapshot_id_for", "verify_snapshot_disk", "write_snapshot",
]
