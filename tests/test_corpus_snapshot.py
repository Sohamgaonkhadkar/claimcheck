"""Snapshots and the repository: reproducible, immutable, and checkable.

The three claims these tests make good on:

    same sources + same metadata + same clause extraction  =>  same snapshot id
    a changed source                                       =>  a *new* snapshot
    a published snapshot                                   =>  never overwritten
"""

from __future__ import annotations

import json
import pathlib
import shutil
from datetime import date

import pytest

from claimcheck.corpus import (
    FilesystemCorpusRepository,
    build_corpus,
    make_snapshot,
    read_snapshot,
    write_snapshot,
)
from claimcheck.errors import CorpusImmutable, CorpusSnapshotInvalid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "data" / "corpus"
CREATED = date(2026, 10, 3)


def copy_sources(target: pathlib.Path) -> pathlib.Path:
    """Copy the corpus *sources* only: published snapshots are not part of the fixtures."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("regulations", "policies"):
        shutil.copytree(SHIPPED / name, target / name)
    return target


@pytest.fixture()
def corpus_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    return copy_sources(tmp_path / "corpus")


def publish(root: pathlib.Path, **kw):
    repo = FilesystemCorpusRepository(root)
    build = build_corpus(root)
    snapshot = repo.publish(build, version_label=kw.pop("label", "v0.1"),
                            created_on=kw.pop("created", CREATED),
                            rulepack_version=kw.pop("rulepack", "2026.10.1"), **kw)
    return repo, build, snapshot


# ---- creation and reproducibility -----------------------------------------
def test_the_shipped_corpus_publishes_a_content_addressed_snapshot():
    repo = FilesystemCorpusRepository(SHIPPED)
    build = build_corpus(SHIPPED)
    snapshot = make_snapshot(build, version_label="v0.1", created_on=CREATED,
                             rulepack_version="2026.10.1")
    assert snapshot.corpus_snapshot_id == "SNAP-v0.1-D685D386F98A"
    assert snapshot.corpus_snapshot_id.startswith(f"SNAP-v0.1-{snapshot.content_hash[:12].upper()}")
    assert len(snapshot.document_ids) == 10
    assert sum(snapshot.clause_counts.values()) == 56
    assert repo.verify(snapshot.corpus_snapshot_id).ok


def test_the_same_sources_produce_the_same_snapshot_id(corpus_copy, tmp_path):
    _, _, first = publish(corpus_copy, label="v0.1")
    second_root = copy_sources(tmp_path / "corpus-two")
    _, _, second = publish(second_root, label="v0.1")
    assert first.corpus_snapshot_id == second.corpus_snapshot_id
    assert first.content_hash == second.content_hash


def test_a_changed_source_produces_a_different_snapshot(corpus_copy):
    repo, _, first = publish(corpus_copy, label="check")
    source = corpus_copy / "regulations/irdai-hlt-reg-cir-151-06-2020/source.txt"
    original = source.read_text(encoding="utf-8")
    # a change that keeps every declared anchor intact: the signature block is re-dated
    assert "(D V S Ramesh)" in original
    source.write_text(original.replace("(D V S Ramesh)", "(D V S Ramesh, GM (Health))"),
                      encoding="utf-8")
    _, _, second = publish(corpus_copy, label="check")
    assert second.corpus_snapshot_id != first.corpus_snapshot_id
    assert second.document_hashes["irdai-hlt-reg-cir-151-06-2020"] != \
        first.document_hashes["irdai-hlt-reg-cir-151-06-2020"]
    # the older snapshot is still on disk, and still describes what it described
    assert repo.current_snapshot_id() == second.corpus_snapshot_id
    older = repo.load(first.corpus_snapshot_id)
    assert "(D V S Ramesh)" in older.clause("irdai-hlt-reg-cir-151-06-2020#10").text
    assert repo.load(second.corpus_snapshot_id).clause(
        "irdai-hlt-reg-cir-151-06-2020#10").text != older.clause(
        "irdai-hlt-reg-cir-151-06-2020#10").text


def test_a_label_change_is_a_different_snapshot_not_an_edit(corpus_copy):
    _, _, a = publish(corpus_copy, label="v0.1")
    _, _, b = publish(corpus_copy, label="v0.2")
    assert a.corpus_snapshot_id != b.corpus_snapshot_id
    assert a.clause_set_hash == b.clause_set_hash       # same knowledge, different release


# ---- immutability ---------------------------------------------------------
def test_a_published_snapshot_is_never_overwritten(corpus_copy):
    repo, build, snapshot = publish(corpus_copy, label="frozen")
    with pytest.raises(CorpusImmutable):
        write_snapshot(repo.snapshot_dir(), _changed(build), snapshot)
    # re-publishing identical content is a no-op, not an error
    write_snapshot(repo.snapshot_dir(), build, snapshot)


def test_re_publishing_the_same_snapshot_directory_is_idempotent(corpus_copy):
    repo, build, snapshot = publish(corpus_copy, label="twice")
    again = write_snapshot(repo.snapshot_dir(), build, snapshot)
    assert (again / "manifest.json").exists()
    assert len(repo.list_snapshots()) == 1


def _changed(build):
    """A build whose first document says something else — same snapshot id, new content."""
    from dataclasses import replace
    docs = list(build.documents)
    docs[0] = replace(docs[0], document=replace(docs[0].document, title="Tampered title"))
    return replace(build, documents=tuple(docs))


# ---- verification ---------------------------------------------------------
def test_verification_re_derives_every_hash(corpus_copy):
    repo, _, snapshot = publish(corpus_copy, label="check")
    result = repo.verify(snapshot.corpus_snapshot_id)
    assert result.ok and result.documents_checked == 10 and result.clauses_checked == 56
    assert result.recomputed_content_hash == snapshot.content_hash


def test_verification_detects_an_edited_clause_in_the_published_snapshot(corpus_copy):
    repo, _, snapshot = publish(corpus_copy, label="check")
    target = repo.snapshot_dir(snapshot.corpus_snapshot_id) / "documents" \
        / "irdai-hlt-reg-cir-151-06-2020.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    index = next(i for i, c in enumerate(payload["clauses"])
                 if "not recover any" in c["text"])
    payload["clauses"][index]["text"] = payload["clauses"][index]["text"].replace(
        "not recover any", "never recover any")
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                      encoding="utf-8")
    result = repo.verify(snapshot.corpus_snapshot_id)
    assert result.ok is False
    assert any("drift" in p for p in result.problems)
    with pytest.raises(CorpusSnapshotInvalid):
        repo.assert_verifiable(snapshot.corpus_snapshot_id)


def test_verification_detects_a_source_file_edited_after_publication(corpus_copy):
    repo, _, snapshot = publish(corpus_copy, label="check")
    source = corpus_copy / "regulations/irdai-hlt-reg-cir-151-06-2020/source.txt"
    source.write_text(source.read_text(encoding="utf-8") + "\n11. An extra paragraph.\n",
                      encoding="utf-8")
    assert repo.verify(snapshot.corpus_snapshot_id).ok is False
    assert repo.verify(snapshot.corpus_snapshot_id, check_artifacts=False).ok is True
    # ... and the snapshot's own records are untouched: the change is in the live tree
    still = repo.load(snapshot.corpus_snapshot_id).clause("irdai-hlt-reg-cir-151-06-2020#10").text
    assert "competentauthority" in still and "An extra paragraph" not in still


def test_a_missing_snapshot_is_a_typed_error(tmp_path):
    repo = FilesystemCorpusRepository(tmp_path)
    with pytest.raises(CorpusSnapshotInvalid):
        read_snapshot(repo.snapshot_dir(), "SNAP-nope-000000000000")


# ---- the repository read interface ----------------------------------------
def test_the_repository_round_trips_documents_clauses_and_references():
    repo = FilesystemCorpusRepository(SHIPPED).open()
    assert repo.snapshot_id == "SNAP-v0.1-D685D386F98A"
    assert len(repo.documents()) == 10
    assert len(repo.clauses()) == 56
    doc = repo.by_id("irdai-hlt-reg-cir-152-06-2020")
    assert doc.reference == "IRDAI/HLT/REG/CIR/152/06/2020"
    assert len(repo.clauses_of(doc.corpus_document_id)) == 8
    assert repo.clause("irdai-hlt-reg-cir-152-06-2020#A1.12").page_number is None
    found = repo.find_by_reference("irdai/hlt/reg/cir/152/06/2020")
    assert [d.corpus_document_id for d in found] == ["irdai-hlt-reg-cir-152-06-2020"]
    assert repo.find_by_reference("nothing like this") == ()


def test_a_run_can_pin_a_snapshot_and_ignore_later_changes(corpus_copy):
    repo, _, first = publish(corpus_copy, label="pinned")
    pinned = repo.open(first.corpus_snapshot_id)
    before = pinned.clause("irdai-hlt-reg-cir-151-06-2020#7").text
    source = corpus_copy / "regulations/irdai-hlt-reg-cir-151-06-2020/source.txt"
    source.write_text(source.read_text(encoding="utf-8").replace("ICU charges", "ICU CHARGES"),
                      encoding="utf-8")
    publish(corpus_copy, label="pinned")
    assert pinned.clause("irdai-hlt-reg-cir-151-06-2020#7").text == before


def test_snapshot_manifests_carry_their_documents_and_counts(corpus_copy):
    repo, _, snapshot = publish(corpus_copy, label="check")
    manifest = json.loads((repo.snapshot_dir(snapshot.corpus_snapshot_id) /
                           "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot"]["corpus_snapshot_id"] == snapshot.corpus_snapshot_id
    assert manifest["summary"]["documents"] == 10
    assert {d["corpus_document_id"] for d in manifest["documents"]} == set(snapshot.document_ids)
    for entry in manifest["documents"]:
        assert entry["document_hash"]
        assert len(entry["clauses"]) == snapshot.clause_counts[entry["corpus_document_id"]]


def test_the_current_pointer_follows_the_newest_publication(corpus_copy):
    repo, _, first = publish(corpus_copy, label="a")
    _, _, second = publish(corpus_copy, label="b")
    assert repo.current_snapshot_id() == second.corpus_snapshot_id
    assert set(repo.list_snapshots()) == {first.corpus_snapshot_id, second.corpus_snapshot_id}
