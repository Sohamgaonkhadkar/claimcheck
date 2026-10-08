"""Corpus storage: one interface, two implementations, no infrastructure yet.

The architecture's target is Postgres tables behind a repository (§21.2). Corpus V0
does not need a database, and adding one now would buy nothing but a migration path to
maintain. What it *does* need is that nothing else in the codebase knows the difference —
so the read interface is defined here and both implementations honour it:

*   :class:`FilesystemCorpusRepository` — a deterministic local store of immutable
    snapshots under ``data/corpus/snapshots/<snapshot_id>/``;
*   :class:`CorpusRepository` — an in-memory view, used by tests, the CLI and a future
    database-backed implementation.

The published snapshot is **read-only by design**: reading it never consults the live
manifests, so editing a source file cannot retroactively change what a past run saw.
"""

from __future__ import annotations

import abc
import pathlib
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from ..errors import CorpusImmutable, CorpusSnapshotInvalid
from .builder import CorpusBuild
from .model import CorpusClause, CorpusDocument, CorpusSnapshot
from .snapshot import (
    LoadedSnapshot,
    SnapshotVerification,
    make_snapshot,
    read_snapshot,
    verify_snapshot_disk,
    write_snapshot,
)

DEFAULT_SNAPSHOT_DIR = ("data", "corpus", "snapshots")


class CorpusRepository(abc.ABC):
    """Read access to a set of corpus documents and their clauses."""

    @abc.abstractmethod
    def snapshot(self) -> CorpusSnapshot:
        ...

    @abc.abstractmethod
    def documents(self) -> tuple[CorpusDocument, ...]:
        ...

    @abc.abstractmethod
    def clauses(self) -> tuple[CorpusClause, ...]:
        ...

    def by_id(self, document_id: str) -> CorpusDocument | None:
        for d in self.documents():
            if d.corpus_document_id == document_id:
                return d
        return None

    def clause(self, clause_id: str) -> CorpusClause | None:
        for c in self.clauses():
            if c.clause_id == clause_id:
                return c
        return None

    def clauses_of(self, document_id: str) -> tuple[CorpusClause, ...]:
        return tuple(c for c in self.clauses() if c.corpus_document_id == document_id)

    def find_by_reference(self, reference: str) -> tuple[CorpusDocument, ...]:
        """Documents whose ``reference`` matches, case-insensitively and trimmed."""
        needle = (reference or "").strip().casefold()
        return tuple(d for d in self.documents() if d.reference.strip().casefold() == needle)

    @property
    def snapshot_id(self) -> str:
        return self.snapshot().corpus_snapshot_id


@dataclass(frozen=True)
class InMemoryCorpusRepository(CorpusRepository):
    """A corpus held in memory. Used by tests, by the CLI and by the rule-linker."""

    _snapshot: CorpusSnapshot
    _documents: tuple[CorpusDocument, ...]
    _clauses: tuple[CorpusClause, ...]

    @classmethod
    def from_build(cls, build: CorpusBuild, *, version_label: str = "working",
                   created_on: date | None = None, rulepack_version: str = "",
                   notes: str = "") -> "InMemoryCorpusRepository":
        snapshot = make_snapshot(build, version_label=version_label,
                                 created_on=created_on or date.today(),
                                 rulepack_version=rulepack_version, notes=notes)
        return cls(_snapshot=snapshot,
                   _documents=tuple(d.document for d in build.documents),
                   _clauses=tuple(c for d in build.documents for c in d.clauses))

    @classmethod
    def from_snapshot(cls, loaded: LoadedSnapshot) -> "InMemoryCorpusRepository":
        return cls(_snapshot=loaded.snapshot, _documents=loaded.documents,
                   _clauses=loaded.clauses)

    def snapshot(self) -> CorpusSnapshot:
        return self._snapshot

    def documents(self) -> tuple[CorpusDocument, ...]:
        return self._documents

    def clauses(self) -> tuple[CorpusClause, ...]:
        return self._clauses


class FilesystemCorpusRepository:
    """Publish and read immutable snapshots on the local filesystem."""

    def __init__(self, root: str | pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    # -- publishing ---------------------------------------------------------
    def snapshot_dir(self, snapshot_id: str | None = None) -> pathlib.Path:
        return self.root / "snapshots" / snapshot_id if snapshot_id else self.root / "snapshots"

    def publish(self, build: CorpusBuild, *, version_label: str, created_on: date,
                rulepack_version: str = "", created_by: str = "corpus-builder",
                notes: str = "", metadata: Mapping[str, object] | None = None) -> CorpusSnapshot:
        snapshot = make_snapshot(build, version_label=version_label, created_on=created_on,
                                 rulepack_version=rulepack_version, created_by=created_by,
                                 notes=notes, metadata=metadata)
        write_snapshot(self.snapshot_dir(), build, snapshot)
        self._write_pointer(snapshot)
        return snapshot

    def _write_pointer(self, snapshot: CorpusSnapshot) -> None:
        """A ``CURRENT`` pointer, for convenience only — never a source of truth.

        A run records the snapshot id it used; the pointer exists so a human can find the
        newest one. Anything that needs *the* snapshot reads the id from the finding.
        """
        self.snapshot_dir().mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir() / "CURRENT"
        if path.exists() and path.read_text(encoding="utf-8").strip() == snapshot.corpus_snapshot_id:
            return
        path.write_text(snapshot.corpus_snapshot_id + "\n", encoding="utf-8")

    # -- reading ------------------------------------------------------------
    def list_snapshots(self) -> tuple[str, ...]:
        d = self.snapshot_dir()
        if not d.exists():
            return ()
        return tuple(sorted(p.name for p in d.iterdir()
                            if p.is_dir() and (p / "manifest.json").exists()))

    def current_snapshot_id(self) -> str:
        path = self.snapshot_dir() / "CURRENT"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        ids = self.list_snapshots()
        if not ids:
            raise CorpusSnapshotInvalid(f"no published snapshots under {self.snapshot_dir()}",
                                        path=str(self.snapshot_dir()))
        return ids[-1]

    def open(self, snapshot_id: str | None = None) -> InMemoryCorpusRepository:
        sid = snapshot_id or self.current_snapshot_id()
        return InMemoryCorpusRepository.from_snapshot(read_snapshot(self.snapshot_dir(), sid))

    def load(self, snapshot_id: str | None = None) -> LoadedSnapshot:
        return read_snapshot(self.snapshot_dir(), snapshot_id or self.current_snapshot_id())

    def verify(self, snapshot_id: str | None = None, *, check_artifacts: bool = True):
        sid = snapshot_id or self.current_snapshot_id()
        return verify_snapshot_disk(self.snapshot_dir(), sid,
                                    source_root=self.root if check_artifacts else None)

    def assert_verifiable(self, snapshot_id: str | None = None) -> SnapshotVerification:
        result = self.verify(snapshot_id)
        if not result.ok:
            raise CorpusSnapshotInvalid(result.describe(), snapshot_id=result.snapshot_id,
                                        problems=list(result.problems))
        return result

    # -- integrity ----------------------------------------------------------
    def expect_artifact_hash(self, snapshot_id: str, document_id: str) -> str | None:
        """The artifact hash a snapshot recorded, for re-ingestion checks."""
        loaded = self.load(snapshot_id)
        doc = loaded.by_id(document_id)
        return doc.artifact_sha256 if doc else None

    def refuse_if_snapshot_exists_with_other_content(self, snapshot: CorpusSnapshot) -> None:
        target = self.snapshot_dir(snapshot.corpus_snapshot_id)
        if target.exists():
            raise CorpusImmutable(
                f"{snapshot.corpus_snapshot_id} is already published",
                snapshot_id=snapshot.corpus_snapshot_id)


def default_snapshot_root(repo_root: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(repo_root).joinpath(*DEFAULT_SNAPSHOT_DIR)
