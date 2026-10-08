"""Corpus snapshots: content-addressed, immutable, reproducible (milestone §3C, §9).

A snapshot answers one question for every finding ever produced: *which exact corpus
did this run see?* It is content-addressed, so the answer is checkable rather than
asserted:

    corpus_snapshot_id = "SNAP-<version_label>-<content_hash[:12]>"

Two ingests of the same artifacts with the same metadata therefore produce the same
snapshot id — and a single changed byte anywhere produces a different one, because the
id is a function of the documents, their clauses and their artifacts. There is no
"update this snapshot" path: :func:`write_snapshot` refuses to overwrite a manifest whose
content differs.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping

from ..errors import CorpusImmutable, CorpusSnapshotInvalid
from . import hashing
from .builder import CorpusBuild, IngestedDocument, ingest_document, load_document_spec
from .model import CorpusClause, CorpusDocument, CorpusSnapshot

MANIFEST = "manifest.json"


@dataclass(frozen=True)
class LoadedSnapshot:
    """A snapshot read back from disk, with the documents and clauses it pinned."""

    snapshot: CorpusSnapshot
    documents: tuple[CorpusDocument, ...]
    clauses: tuple[CorpusClause, ...]
    root: str

    def by_id(self, document_id: str) -> CorpusDocument | None:
        for d in self.documents:
            if d.corpus_document_id == document_id:
                return d
        return None

    def clause(self, clause_id: str) -> CorpusClause | None:
        for c in self.clauses:
            if c.clause_id == clause_id:
                return c
        return None

    def clauses_of(self, document_id: str) -> tuple[CorpusClause, ...]:
        return tuple(c for c in self.clauses if c.corpus_document_id == document_id)

    def snapshot_id(self) -> str:
        return self.snapshot.corpus_snapshot_id


@dataclass(frozen=True)
class SnapshotVerification:
    """The result of re-deriving a published snapshot: what reproduces, and what drifted."""

    snapshot_id: str
    ok: bool
    problems: tuple[str, ...] = ()
    documents_checked: int = 0
    clauses_checked: int = 0
    recomputed_content_hash: str = ""

    def describe(self) -> str:
        if self.ok:
            return (f"{self.snapshot_id}: reproducible "
                    f"({self.documents_checked} documents, {self.clauses_checked} clauses)")
        return f"{self.snapshot_id}: NOT reproducible — " + "; ".join(self.problems)


def snapshot_id_for(version_label: str, content_hash: str) -> str:
    return f"SNAP-{version_label}-{hashing.short(content_hash)}"


def make_snapshot(build: CorpusBuild, *, version_label: str, created_on: date,
                  rulepack_version: str = "", created_by: str = "corpus-builder",
                  notes: str = "", metadata: Mapping[str, Any] | None = None) -> CorpusSnapshot:
    """Derive the snapshot record — and its id — from the ingested corpus."""
    document_ids = tuple(d.document.corpus_document_id for d in build.documents)
    document_hashes = {d.document.corpus_document_id: d.document_hash for d in build.documents}
    clause_counts = {d.document.corpus_document_id: len(d.clauses) for d in build.documents}
    clause_set = hashing.clause_set_hash([c.payload() for c in build.all_clauses()])
    content_hash = hashing.hash_payload({
        "version_label": version_label,
        "document_hashes": document_hashes,
        "clause_set_hash": clause_set,
        "rulepack_version": rulepack_version,
    })
    return CorpusSnapshot(
        corpus_snapshot_id=snapshot_id_for(version_label, content_hash),
        version_label=version_label,
        created_on=created_on,
        document_ids=document_ids,
        document_hashes=document_hashes,
        clause_counts=clause_counts,
        clause_set_hash=clause_set,
        content_hash=content_hash,
        rulepack_version=rulepack_version,
        created_by=created_by,
        notes=notes,
        metadata=dict(metadata or {}),
    )


COMMENTARY_FIELDS = ("notes", "created_by", "metadata")


def _without_commentary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest minus the fields that describe the release rather than the corpus."""
    stripped = {k: v for k, v in payload.items() if k != "snapshot"}
    snapshot = dict(payload.get("snapshot", {}))
    for field in COMMENTARY_FIELDS:
        snapshot.pop(field, None)
    stripped["snapshot"] = snapshot
    return stripped


def write_snapshot(root: str | pathlib.Path, build: CorpusBuild,
                   snapshot: CorpusSnapshot) -> pathlib.Path:
    """Publish a snapshot directory. Never overwrites differing content."""
    root = pathlib.Path(root)
    target = root / snapshot.corpus_snapshot_id
    payload = _snapshot_payload(snapshot, build)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if target.exists():
        # Release *commentary* (notes, author, free-form metadata) is not corpus content
        # and may be corrected in place; everything else is immutable. Compare the two
        # manifests with the commentary stripped, and rewrite the file if only that
        # changed.
        existing = json.loads((target / MANIFEST).read_text(encoding="utf-8")) \
            if (target / MANIFEST).exists() else None
        if existing == payload:
            return target                       # idempotent re-publish of identical content
        if existing is not None and _without_commentary(existing) == _without_commentary(payload):
            (target / MANIFEST).write_text(text, encoding="utf-8")
            return target
        raise CorpusImmutable(
            f"{snapshot.corpus_snapshot_id} already exists with different content; a change "
            f"to a source produces a new snapshot, never an edit of an old one",
            snapshot_id=snapshot.corpus_snapshot_id, path=str(target))
    (target / "documents").mkdir(parents=True, exist_ok=False)
    for d in build.documents:
        doc_payload = {
            "document": d.document.to_payload(),
            "artifact_sha256": d.artifact_sha256,
            "document_hash": d.document_hash,
            "clauses": [c.to_payload() for c in d.clauses],
        }
        path = target / "documents" / f"{d.document.corpus_document_id}.json"
        path.write_text(json.dumps(doc_payload, indent=2, sort_keys=True, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    (target / MANIFEST).write_text(text, encoding="utf-8")
    return target


def read_snapshot(root: str | pathlib.Path, snapshot_id: str) -> LoadedSnapshot:
    target = pathlib.Path(root) / snapshot_id
    manifest = target / MANIFEST
    if not manifest.exists():
        raise CorpusSnapshotInvalid(f"no snapshot {snapshot_id} under {root}",
                                    snapshot_id=snapshot_id, path=str(root))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    snapshot = CorpusSnapshot.from_payload(payload["snapshot"])
    documents: list[CorpusDocument] = []
    clauses: list[CorpusClause] = []
    for path in sorted((target / "documents").glob("*.json")):
        doc_payload = json.loads(path.read_text(encoding="utf-8"))
        documents.append(CorpusDocument.from_payload(doc_payload["document"]))
        clauses.extend(CorpusClause.from_payload(c) for c in doc_payload["clauses"])
    clauses = sorted(clauses, key=lambda c: (c.corpus_document_id, c.position))
    return LoadedSnapshot(snapshot=snapshot, documents=tuple(documents),
                          clauses=tuple(clauses), root=str(target))


def verify_snapshot_disk(root: str | pathlib.Path, snapshot_id: str,
                        source_root: str | pathlib.Path | None = None) -> SnapshotVerification:
    """Re-derive the snapshot: hashes, clause set, and (optionally) the artifacts on disk.

    This is the check that makes "reproducible against the exact snapshot it used"
    meaningful: it fails when a manifest was hand-edited, when a clause set changed, or
    when the stored source text no longer matches the hash the snapshot recorded.
    """
    problems: list[str] = []
    loaded = read_snapshot(root, snapshot_id)
    recomputed = hashing.hash_payload({
        "version_label": loaded.snapshot.version_label,
        "document_hashes": dict(sorted(loaded.snapshot.document_hashes.items())),
        "clause_set_hash": hashing.clause_set_hash([c.payload() for c in loaded.clauses]),
        "rulepack_version": loaded.snapshot.rulepack_version,
    })
    if recomputed != loaded.snapshot.content_hash:
        problems.append(f"content hash drift: recorded {loaded.snapshot.content_hash[:12]}, "
                        f"recomputed {recomputed[:12]}")
    if snapshot_id_for(loaded.snapshot.version_label, recomputed) != loaded.snapshot.corpus_snapshot_id:
        problems.append("snapshot id does not match its own content hash")

    for doc in loaded.documents:
        clauses = [c.payload() for c in loaded.clauses_of(doc.corpus_document_id)]
        artifacts = {"source_text": doc.artifact_sha256} if doc.artifact_sha256 else {}
        recomputed_doc = hashing.document_hash(doc.to_payload(), clauses, artifacts)
        recorded = loaded.snapshot.document_hashes.get(doc.corpus_document_id)
        if recorded != recomputed_doc:
            problems.append(f"{doc.corpus_document_id}: document hash drift")
        if any(c.corpus_document_id != doc.corpus_document_id for c in loaded.clauses_of(
                doc.corpus_document_id)):
            problems.append(f"{doc.corpus_document_id}: a clause is filed under another document")
        if source_root and doc.artifact_path:
            src = pathlib.Path(source_root) / doc.artifact_path
            if not src.exists():
                problems.append(f"{doc.corpus_document_id}: artifact {doc.artifact_path} missing")
            else:
                found = hashing.sha256_bytes(src.read_bytes())
                if doc.artifact_sha256 and found != doc.artifact_sha256:
                    problems.append(
                        f"{doc.corpus_document_id}: artifact changed on disk "
                        f"(recorded {doc.artifact_sha256[:12]}, found {found[:12]})")
    return SnapshotVerification(
        snapshot_id=snapshot_id,
        ok=not problems,
        problems=tuple(problems),
        documents_checked=len(loaded.documents),
        clauses_checked=len(loaded.clauses),
        recomputed_content_hash=recomputed,
    )


def reingest_from_snapshot(source_root: str | pathlib.Path) -> CorpusBuild:
    """Re-ingest the *live* manifests, refusing any artifact that drifted from the snapshot."""
    from .builder import build_corpus
    return build_corpus(source_root)


def reingest_document(spec_path: str | pathlib.Path,
                      expect_artifact_sha256: str | None = None) -> IngestedDocument:
    return ingest_document(load_document_spec(spec_path),
                           expect_artifact_sha256=expect_artifact_sha256)


def _snapshot_payload(snapshot: CorpusSnapshot, build: CorpusBuild) -> dict[str, Any]:
    return {
        "snapshot": snapshot.to_payload(),
        "documents": [
            {
                "corpus_document_id": d.document.corpus_document_id,
                "document_hash": d.document_hash,
                "artifact_sha256": d.artifact_sha256,
                "artifact_path": d.document.artifact_path,
                "clauses": [{"clause_id": c.clause_id, "text_sha256": c.text_sha256}
                            for c in d.clauses],
            }
            for d in build.documents
        ],
        "summary": build.summary(),
    }
