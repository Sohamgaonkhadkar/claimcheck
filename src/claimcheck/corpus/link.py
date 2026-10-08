"""Rule -> corpus linkage: which document, which clause, which snapshot, which quote.

The rule model says what a rule says. The corpus holds the text. This module joins them
and reports, for every rule, four independent facts:

1.  **resolved** — the declared clause exists in the corpus the run is using;
2.  **instrument agrees** — the document the clause belongs to is the document the rule
    names in its ``instrument`` block (a clause in the right corpus but the wrong
    instrument is a citation error, not a citation);
3.  **quote verified** — the rule's verbatim quote is present in that clause's text
    (exact, normalised or fuzzy — see :mod:`claimcheck.corpus.verify`);
4.  **assertable** — the rule engine's own gate (verified + current + in force +
    verdict-capable effect) still holds.

Point 4 is the one that keeps this honest: appearing in a corpus **never** upgrades a
rule. A contested rule remains contested, an unverified rule remains unverified, and a
lapsed verification window still degrades the rule to unassertable, however cleanly its
quote matches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from claimcheck.errors import CorpusSnapshotInvalid
from typing import Any, Mapping, Sequence

from ..rules.model import (
    VERDICT_CAPABLE_EFFECTS,
    RulePack,
    RuleSourceRef,
    RuleStatus,
    RuleVersion,
)
from ..rules.model import pack_summary
from .model import CorpusClause, CorpusDocument
from .repository import CorpusRepository
from .verify import QuoteCheck, check_quote_in_clause

#: Statuses whose rules are allowed to carry no verbatim quote (they decide nothing).
QUOTELESS_OK = (RuleStatus.CONTESTED, RuleStatus.UNVERIFIED, RuleStatus.GATED)


def _norm_ref(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


@dataclass(frozen=True)
class RuleLink:
    """One rule, joined to the corpus record it claims to come from."""

    rule_id: str
    version: int
    status: RuleStatus
    effect: str
    instrument_ref: str
    citation: str
    quote: str
    assertable: bool
    source_ref: RuleSourceRef | None = None
    document: CorpusDocument | None = None
    clause: CorpusClause | None = None
    quote_check: QuoteCheck | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.document is not None and self.clause is not None

    @property
    def instrument_agrees(self) -> bool:
        if self.document is None:
            return False
        a, b = _norm_ref(self.instrument_ref), _norm_ref(self.document.reference)
        return bool(b) and (a in b or b in a)

    @property
    def quote_verified(self) -> bool | None:
        """``None`` means "there was no quote to verify" (contest/unverified rules)."""
        if self.quote_check is None:
            return None
        return self.quote_check.found

    @property
    def verdict_capable(self) -> bool:
        return self.effect in VERDICT_CAPABLE_EFFECTS

    @property
    def ok(self) -> bool:
        return self.resolved and self.instrument_agrees and self.quote_verified is not False \
            and not self.problems

    def describe(self) -> str:
        if not self.resolved:
            return f"{self.rule_id}: no corpus clause ({'; '.join(self.problems) or 'unlinked'})"
        head = (f"{self.rule_id} v{self.version} [{self.status.value}] -> "
                f"{self.clause.clause_id}")
        if self.quote_check is not None:
            head += f" | quote {self.quote_check.describe()}"
        else:
            head += " | no quote to verify"
        if self.problems:
            head += " | PROBLEMS: " + "; ".join(self.problems)
        return head

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "status": self.status.value,
            "effect": self.effect,
            "assertable": self.assertable,
            "instrument_ref": self.instrument_ref,
            "citation": self.citation,
            "corpus_document_id": self.document.corpus_document_id if self.document else None,
            "clause_id": self.clause.clause_id if self.clause else None,
            "document_reference": self.document.reference if self.document else None,
            "document_status": self.document.source_status.value if self.document else None,
            "instrument_agrees": self.instrument_agrees,
            "quote_verified": self.quote_verified,
            "quote_method": (self.quote_check.method_name if self.quote_check else None),
            "quote_score": (round(self.quote_check.score, 4) if self.quote_check else None),
            "quote_span": ([self.quote_check.char_start, self.quote_check.char_end]
                           if self.quote_check and self.quote_check.char_start is not None
                           else None),
            "page": (self.clause.page_number if self.clause else None),
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RuleLinkReport:
    """Every rule in a pack, linked. The failures are the interesting part."""

    snapshot_id: str
    today: date
    links: tuple[RuleLink, ...]
    pack_version: str = ""
    pack_summary: Mapping[str, int] = field(default_factory=dict)

    def by_rule(self, rule_id: str) -> RuleLink | None:
        for link in self.links:
            if link.rule_id == rule_id:
                return link
        return None

    @property
    def unlinked(self) -> tuple[RuleLink, ...]:
        """Rules with no ``[rule.source]`` at all."""
        return tuple(l for l in self.links if l.source_ref is None)

    @property
    def unresolved(self) -> tuple[RuleLink, ...]:
        """Rules that point at a clause the corpus does not contain."""
        return tuple(l for l in self.links if l.source_ref is not None and not l.resolved)

    @property
    def quote_failures(self) -> tuple[RuleLink, ...]:
        """Rules whose quote is not in the clause they point at. Fabrication or drift."""
        return tuple(l for l in self.links if l.quote_verified is False)

    @property
    def instrument_mismatches(self) -> tuple[RuleLink, ...]:
        return tuple(l for l in self.links if l.resolved and not l.instrument_agrees)

    @property
    def verified(self) -> tuple[RuleLink, ...]:
        return tuple(l for l in self.links if l.ok)

    @property
    def risky(self) -> tuple[RuleLink, ...]:
        """Verdict-capable and assertable, but *not* fully linked. The dangerous set.

        A rule in this set can decide money while its quotation cannot be produced from
        the corpus the run is using. This milestone does not fail the build on it; it
        makes it impossible to miss.
        """
        return tuple(l for l in self.links if l.verdict_capable and l.assertable and not l.ok)

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "rulepack_version": self.pack_version,
            "as_of": self.today.isoformat(),
            "rules": len(self.links),
            "linked": sum(1 for l in self.links if l.source_ref is not None),
            "resolved": sum(1 for l in self.links if l.resolved),
            "quote_verified": sum(1 for l in self.links if l.quote_verified is True),
            "quote_failed": len(self.quote_failures),
            "unlinked": len(self.unlinked),
            "unresolved": len(self.unresolved),
            "instrument_mismatched": len(self.instrument_mismatches),
            "assertable": sum(1 for l in self.links if l.assertable),
            "assertable_but_unlinked": len(self.risky),
            "pack": dict(self.pack_summary),
        }

    def describe(self) -> str:
        s = self.summary()
        lines = [f"rule pack {s['rulepack_version']} against {s['snapshot_id']}: "
                 f"{s['resolved']}/{s['rules']} rules resolved to a clause, "
                 f"{s['quote_verified']} quotes verified, {s['quote_failed']} failed, "
                 f"{s['assertable_but_unlinked']} assertable-but-unlinked"]
        for link in self.risky:
            lines.append("  RISK  " + link.describe())
        for link in self.quote_failures:
            lines.append("  QUOTE " + link.describe())
        for link in self.unresolved:
            lines.append("  GAP   " + link.describe())
        return "\n".join(lines)


def _corpus_view(repo: CorpusRepository):
    """A repository with no snapshot open is still a repository: treat it as empty.

    Linking must never raise for a missing corpus -- the whole point of the report is to
    say *what is missing*. ``None`` therefore means "nothing to read", not "error".
    """
    if hasattr(repo, "by_id"):
        return repo
    open_snapshot = getattr(repo, "open", None)
    if open_snapshot is None:
        return None
    try:
        return open_snapshot()
    except CorpusSnapshotInvalid:
        return None


def link_rule(rule: RuleVersion, repo: CorpusRepository, *, today: date) -> RuleLink:
    """Join one rule to the corpus. Never raises for a missing link; reports it."""
    problems: list[str] = []
    notes: list[str] = []
    document: CorpusDocument | None = None
    clause: CorpusClause | None = None
    quote_check: QuoteCheck | None = None
    ref = rule.source
    view = _corpus_view(repo)

    if ref is None:
        problems.append("no [rule.source] declared: the rule names an instrument but not a clause")
    elif view is None:
        problems.append(f"no snapshot is published under {repo.root}: nothing to link against")
    else:
        where = view.snapshot_id
        document = view.by_id(ref.corpus_document_id)
        if document is None:
            problems.append(f"document {ref.corpus_document_id} is not in {where}")
            known = ", ".join(sorted(d.corpus_document_id for d in view.documents()))
            notes.append(f"documents in this snapshot: {known}")
        clause = view.clause(ref.clause_id)
        if clause is None:
            problems.append(f"clause {ref.clause_id} is not in {where}")
            available = [c.clause_id for c in repo.clauses_of(ref.corpus_document_id)]
            if available:
                notes.append("clauses on that document: " + ", ".join(available))
        elif document is not None and clause.corpus_document_id != document.corpus_document_id:
            problems.append(
                f"clause {clause.clause_id} belongs to {clause.corpus_document_id}, not "
                f"{document.corpus_document_id}")
        if ref.corpus_snapshot_id and ref.corpus_snapshot_id != repo.snapshot_id:
            notes.append(
                f"the rule was written against {ref.corpus_snapshot_id}; this report reads "
                f"{repo.snapshot_id}")
        if clause is not None:
            if rule.quote.strip():
                quote_check = check_quote_in_clause(rule.quote, clause)
                if not quote_check.found:
                    problems.append("the rule's quote is not present in the clause it cites")
            elif rule.status not in QUOTELESS_OK:
                problems.append(
                    f"rule status {rule.status.value} requires a verbatim quote, and it has none")

    if rule.status is not RuleStatus.VERIFIED and rule.effect in VERDICT_CAPABLE_EFFECTS:
        notes.append(f"status {rule.status.value}: this rule may not decide anything on its own")
    if not rule.assertable(today):
        notes.append("not assertable today (verification window, force dates or effect)")

    return RuleLink(
        rule_id=rule.rule_id,
        version=rule.version,
        status=rule.status,
        effect=rule.effect,
        instrument_ref=rule.instrument.ref,
        citation=rule.citation,
        quote=rule.quote,
        assertable=rule.assertable(today),
        source_ref=ref,
        document=document,
        clause=clause,
        quote_check=quote_check,
        problems=tuple(problems),
        notes=tuple(notes),
    )


def snapshot_id_of(repo: CorpusRepository) -> str:
    """The snapshot a repository is holding, or an explicit "none" — never an exception."""
    try:
        return repo.snapshot_id
    except Exception:                                  # no snapshot published under this root
        return "(no snapshot published)"


def link_pack(pack: RulePack, repo: CorpusRepository, *, today: date) -> RuleLinkReport:
    links = tuple(link_rule(r, repo, today=today) for r in pack.rules)
    return RuleLinkReport(snapshot_id=snapshot_id_of(repo), today=today, links=links,
                          pack_version=pack.version, pack_summary=pack_summary(pack))


def provenance_for_rule(rule_id: str, pack: RulePack, repo: CorpusRepository, *,
                        today: date) -> Mapping[str, Any]:
    """The answer to §16's six questions, as data — the CLI prints exactly this."""
    rule = pack.by_id(rule_id)
    if rule is None:
        return {"rule_id": rule_id, "found": False,
                "error": f"{rule_id} is not in rule pack {pack.version}"}
    link = link_rule(rule, repo, today=today)
    clause = link.clause
    document = link.document
    return {
        "rule_id": rule.rule_id,
        "found": True,
        "rule_version": rule.version,
        "rule_status": rule.status.value,
        "rule_assertable_today": rule.assertable(today),
        "instrument": {
            "ref": rule.instrument.ref,
            "title": rule.instrument.title,
            "issued_on": rule.instrument.issued_on.isoformat() if rule.instrument.issued_on else None,
            "url": rule.instrument.url,
        },
        "corpus_document": None if document is None else {
            "corpus_document_id": document.corpus_document_id,
            "title": document.title,
            "reference": document.reference,
            "source_status": document.source_status.value,
            "status_basis": document.status_basis,
            "source_url": document.source_url,
            "retrieved_at": document.retrieved_at.isoformat() if document.retrieved_at else None,
            "artifact_sha256": document.artifact_sha256,
            "artifact_path": document.artifact_path,
            "gaps": list(document.gaps),
        },
        "clause": None if clause is None else {
            "clause_id": clause.clause_id,
            "heading_path": clause.heading_path,
            "page_number": clause.page_number,
            "char_span": [clause.char_start, clause.char_end],
            "text_sha256": clause.text_sha256,
            "text": clause.text,
        },
        "corpus_snapshot": {
            "corpus_snapshot_id": repo.snapshot_id,
            "version_label": repo.snapshot().version_label,
            "content_hash": repo.snapshot().content_hash,
        },
        "quote": {
            "text": rule.quote,
            "verified": link.quote_verified,
            "method": link.quote_check.method_name if link.quote_check else None,
            "score": round(link.quote_check.score, 4) if link.quote_check else None,
            "detail": link.quote_check.describe() if link.quote_check else None,
            "mismatch": link.quote_check.mismatch if link.quote_check else None,
            "differences": list(link.quote_check.differences) if link.quote_check else [],
            "artefact_note": link.quote_check.artefact_note if link.quote_check else None,
        },
        "problems": list(link.problems),
        "notes": list(link.notes),
    }


def explain_provenance(rule_id: str, pack: RulePack, repo: CorpusRepository, *,
                       today: date) -> str:
    """The same answer as prose, for a terminal or a report."""
    p = provenance_for_rule(rule_id, pack, repo, today=today)
    if not p["found"]:
        return str(p["error"])
    doc, clause, quote = p["corpus_document"], p["clause"], p["quote"]
    lines = [
        f"{p['rule_id']} (v{p['rule_version']}, {p['rule_status']}) "
        f"— assertable today: {p['rule_assertable_today']}",
        f"  instrument : {p['instrument']['ref']} — {p['instrument']['title']}",
    ]
    if doc:
        lines += [
            f"  document   : {doc['corpus_document_id']} ({doc['source_status']}) "
            f"— {doc['title']}",
            f"               retrieved {doc['retrieved_at']} from {doc['source_url']}",
            f"               artifact   {doc['artifact_path']} sha256 {str(doc['artifact_sha256'])[:16]}",
        ]
        if doc["gaps"]:
            lines.append("               open gaps: " + "; ".join(doc["gaps"]))
    if clause:
        lines += [
            f"  clause     : {clause['clause_id']} [{clause['heading_path']}] "
            f"chars {clause['char_span'][0]}-{clause['char_span'][1]}"
            + (f", page {clause['page_number']}" if clause["page_number"] else ""),
            f"               > {clause['text'][:200]}{'...' if len(clause['text']) > 200 else ''}",
        ]
    lines.append(f"  snapshot   : {p['corpus_snapshot']['corpus_snapshot_id']} "
                 f"({p['corpus_snapshot']['version_label']})")
    if quote["verified"] is None:
        lines.append("  quote      : none (this rule decides nothing, so it carries no quote)")
    else:
        lines.append(f"  quote      : {'VERIFIED' if quote['verified'] else 'NOT FOUND'} "
                     f"[{quote['method']}] {quote['detail']}")
        if quote["artefact_note"]:
            lines.append(f"               note: {quote['artefact_note']}")
        for d in quote["differences"][:4]:
            lines.append(f"               diff: {d}")
    for problem in p["problems"]:
        lines.append(f"  PROBLEM    : {problem}")
    return "\n".join(lines)


def clause_ids_in(links: Sequence[RuleLink]) -> tuple[str, ...]:
    return tuple(sorted({l.clause.clause_id for l in links if l.clause}))
