#!/usr/bin/env python3
"""Corpus V0 command line: build, verify, link, and answer "where does this rule come from?".

    python scripts/corpus.py build --label v0.1 --created 2026-10-03
    python scripts/corpus.py verify
    python scripts/corpus.py link
    python scripts/corpus.py provenance PD.AME.DEFINE
    python scripts/corpus.py clauses irdai-hlt-reg-cir-151-06-2020
    python scripts/corpus.py show irdai-hlt-reg-cir-151-06-2020#4

No model, no network: the corpus path is deterministic from stored artifacts.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claimcheck.corpus import (                     # noqa: E402
    FilesystemCorpusRepository,
    build_corpus,
    explain_provenance,
    link_pack,
    provenance_for_rule,
)
from claimcheck.rules.model import load_default_rulepack   # noqa: E402

DEFAULT_ROOT = ROOT / "data" / "corpus"
TODAY = date(2026, 10, 3)


def cmd_build(args: argparse.Namespace) -> int:
    build = build_corpus(args.root)
    repo = FilesystemCorpusRepository(args.root)
    snapshot = repo.publish(build, version_label=args.label, created_on=args.created,
                            rulepack_version=load_default_rulepack().version,
                            notes=args.notes)
    print(f"ingested {len(build.documents)} documents, {len(build.all_clauses())} clauses")
    for d in build.documents:
        doc = d.document
        print(f"  {doc.corpus_document_id:38s} {doc.source_status.value:10s} "
              f"clauses={len(d.clauses):2d} doc_hash={d.document_hash[:12]} "
              f"artifact={(d.artifact_sha256 or '-')[:12]}")
    print(f"published {snapshot.corpus_snapshot_id} (content {snapshot.content_hash[:12]})")
    verification = repo.verify(snapshot.corpus_snapshot_id)
    print(verification.describe())
    return 0 if verification.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    repo = FilesystemCorpusRepository(args.root)
    sid = args.snapshot or repo.current_snapshot_id()
    result = repo.verify(sid)
    print(result.describe())
    for problem in result.problems:
        print("  -", problem)
    return 0 if result.ok else 1


def cmd_link(args: argparse.Namespace) -> int:
    repo = FilesystemCorpusRepository(args.root).open(args.snapshot)
    pack = load_default_rulepack()
    report = link_pack(pack, repo, today=args.today)
    print(report.describe())
    print()
    for link in report.links:
        print(link.describe())
    return 0 if not report.quote_failures else 1


def cmd_provenance(args: argparse.Namespace) -> int:
    repo = FilesystemCorpusRepository(args.root).open(args.snapshot)
    pack = load_default_rulepack()
    if args.json:
        import json
        print(json.dumps(provenance_for_rule(args.rule_id, pack, repo, today=args.today),
                         indent=2, ensure_ascii=False))
    else:
        print(explain_provenance(args.rule_id, pack, repo, today=args.today))
    return 0


def cmd_clauses(args: argparse.Namespace) -> int:
    repo = FilesystemCorpusRepository(args.root).open(args.snapshot)
    doc = repo.by_id(args.document_id)
    if doc is None:
        print(f"no such document: {args.document_id}", file=sys.stderr)
        return 2
    print(f"{doc.corpus_document_id} — {doc.title} ({doc.source_status.value})")
    print(f"  reference {doc.reference} | artifact {doc.artifact_path} "
          f"| sha256 {(doc.artifact_sha256 or '-')[:16]}")
    for clause in repo.clauses_of(doc.corpus_document_id):
        print(f"  {clause.clause_id:52s} [{clause.heading_path}] "
              f"chars {clause.char_start}-{clause.char_end}"
              + (f" page {clause.page_number}" if clause.page_number else ""))
    if not repo.clauses_of(doc.corpus_document_id):
        print("  (no clauses stored)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = FilesystemCorpusRepository(args.root).open(args.snapshot)
    clause = repo.clause(args.clause_id)
    if clause is None:
        print(f"no such clause: {args.clause_id}", file=sys.stderr)
        return 2
    doc = repo.by_id(clause.corpus_document_id)
    print(f"{clause.clause_id} [{clause.heading_path}] "
          f"page {clause.page_number if clause.page_number else 'not recorded'}")
    print(f"artifact {doc.artifact_path if doc else '?'} chars "
          f"{clause.char_start}-{clause.char_end} sha256 {clause.text_sha256[:16]}")
    print("-" * 72)
    print(clause.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--today", type=date.fromisoformat, default=TODAY)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="ingest the manifests and publish a snapshot")
    b.add_argument("--label", default="v0.1")
    b.add_argument("--created", type=date.fromisoformat, default=TODAY)
    b.add_argument("--notes", default="")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="re-derive a published snapshot and report drift")
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("link", help="link every rule in the pack to its corpus clause")
    l.set_defaults(func=cmd_link)

    p = sub.add_parser("provenance", help="which document, clause, quote and snapshot back a rule")
    p.add_argument("rule_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_provenance)

    c = sub.add_parser("clauses", help="list a document's clauses")
    c.add_argument("document_id")
    c.set_defaults(func=cmd_clauses)

    s = sub.add_parser("show", help="print one clause with its offsets")
    s.add_argument("clause_id")
    s.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
