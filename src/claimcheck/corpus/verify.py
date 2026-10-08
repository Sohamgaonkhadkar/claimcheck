"""Quote verification against the stored source (milestone §8).

"Does this quotation actually exist in the document we say it came from?" is a
**deterministic** question, and this module is the only place that answers it. No model
is involved, and none may be: a quote that cannot be located is a fabricated quote, and
a fabricated quote must never reach a finding (Phase-3 rule 2).

The matching engine is not re-implemented here. :func:`claimcheck.evidence.spans.verify_quote`
already implements exact -> normalised -> fuzzy -> rejected, with two properties this
layer depends on:

*   **uniqueness**: a quote that occurs twice is AMBIGUOUS, never a coin toss;
*   **numbers must match**: a fuzzy match is refused outright if the digits in the
    matched window differ from the digits in the quote.

What this module adds is *corpus context*: which document and clause were checked, the
page, the character span, a readable mismatch report, and — because the regulator's PDFs
lose inter-word spaces in their text layer — a named diagnostic that distinguishes a
**typographic artefact** from a **different sentence**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from typing import Iterable, Sequence

from ..evidence.spans import load_bearing_signature, normalise_for_match, verify_quote
from ..schema import VerifyMethod
from .model import CorpusClause, CorpusDocument


#: Methods that count as "the quote was found in this source".
ACCEPTED_METHODS = (VerifyMethod.EXACT, VerifyMethod.NORMALISED, VerifyMethod.FUZZY)

#: Ordering used to pick the best result: stronger evidence first.
_METHOD_ORDER = {VerifyMethod.EXACT: 0, VerifyMethod.NORMALISED: 1, VerifyMethod.FUZZY: 2,
                 VerifyMethod.AMBIGUOUS: 3, VerifyMethod.REJECTED: 4}


@dataclass(frozen=True)
class QuoteCheck:
    """The result of looking for a quote in one piece of source text."""

    quote: str
    found: bool
    method: VerifyMethod
    score: float
    document_id: str | None = None
    clause_id: str | None = None
    page_number: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    text_sha256: str | None = None
    mismatch: str | None = None
    differences: tuple[str, ...] = ()
    artefact_note: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_exact(self) -> bool:
        return self.method is VerifyMethod.EXACT

    @property
    def method_name(self) -> str:
        return self.method.value if hasattr(self.method, "value") else str(self.method)

    @property
    def method_rank(self) -> int:
        return _METHOD_ORDER.get(self.method, 9)

    def describe(self) -> str:
        where = self.clause_id or self.document_id or "source"
        if self.found:
            return (f"verified {self.method_name} ({self.score:.2f}) in {where}"
                    + (f" at chars {self.char_start}-{self.char_end}"
                       if self.char_start is not None else ""))
        return f"NOT FOUND in {where}: {self.mismatch or self.method_name}"


_LEADING_LABEL = re.compile(r"^\s*\d{1,3}[\.,\)]\s*")


def _elide_ws(text: str) -> str:
    """Every non-whitespace character, in order. Used only as a *diagnostic*."""
    return "".join(ch for ch in text if not ch.isspace())


def _elide_norm(text: str) -> str:
    """Normalised (case/quotes/dashes folded) and whitespace-elided. Diagnostic only."""
    s = normalise_for_match(_LEADING_LABEL.sub("", text or "", count=1))
    return "".join(ch for ch in s if not ch.isspace())


def explain_differences(quote: str, text: str, *, limit: int = 6) -> tuple[str, ...]:
    """The fragments where the two strings differ, in order. Diagnostic, not matching."""
    out: list[str] = []
    sm = SequenceMatcher(None, text, quote, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append(f"{tag}: source {text[i1:i2]!r} vs quote {quote[j1:j2]!r}")
        if len(out) >= limit:
            break
    return tuple(out)


def _artefact_note(quote: str, text: str, differences: Sequence[str]) -> str | None:
    """Name the case where the only disagreement is whitespace at word boundaries.

    The regulator's PDFs are extracted with inter-word spaces missing
    (``designinsurers`` for ``design insurers``). That is a property of *our text
    capture*, not of the instrument — and saying so is not the same as excusing a
    mismatch, so this note never changes the verdict of the check, only explains it.
    """
    if not differences:
        return None
    elided_q, elided_t = _elide_norm(quote), _elide_norm(text)
    if elided_q and elided_q in elided_t:
        return ("the quote and the stored source agree once whitespace is elided: the "
                "disagreement is at word boundaries lost by the PDF text layer, not in "
                "the words themselves")
    if not elided_q or not elided_t:
        return None
    if SequenceMatcher(None, elided_t, elided_q, autojunk=False).ratio() >= 0.99:
        opcodes = [op for op in
                   SequenceMatcher(None, elided_t, elided_q, autojunk=False).get_opcodes()
                   if op[0] != "equal"]
        if any(load_bearing_signature(elided_t[i1:i2])
               or load_bearing_signature(elided_q[j1:j2])
               for _tag, i1, i2, j1, j2 in opcodes):
            # "shall not recover" vs "shall recover" is three characters. It is also a
            # different rule, and calling it a spacing artefact would be exactly the kind
            # of quiet exoneration this layer exists to prevent.
            return None
        frags = [f"source {elided_t[i1:i2]!r} vs quote {elided_q[j1:j2]!r}"
                 for _tag, i1, i2, j1, j2 in opcodes]
        return ("the quoted words are present in the capture apart from "
                f"{len(frags)} character-level difference(s) ({'; '.join(frags[:2])}); "
                "this is a defect of the stored capture, reported rather than excused")
    return None


def check_quote(quote: str, text: str, *, document_id: str | None = None,
                clause_id: str | None = None, page_number: int | None = None,
                fuzzy_threshold: float = 0.92,
                with_differences: bool = True) -> QuoteCheck:
    """Locate ``quote`` in ``text`` and report how, where, and — if not — why not."""
    outcome = verify_quote(quote, text, fuzzy_threshold=fuzzy_threshold)
    from ..schema import sha256_text

    found = outcome.method in ACCEPTED_METHODS and bool(outcome.method.accepted)
    differences: tuple[str, ...] = ()
    mismatch = outcome.note
    artefact = None
    if with_differences and outcome.method is not VerifyMethod.EXACT:
        # Anything short of an exact hit gets a diagnosis: a reader is entitled to know
        # *why* the words differ, and a reviewer is entitled to see that a genuine
        # difference was not waved through as a capture artefact.
        differences = explain_differences(quote, text or "")
        artefact = _artefact_note(quote, text or "", differences)
        if location := _locate_by_elision(quote, text or ""):
            differences = differences + (f"elided match at chars {location[0]}-{location[1]}",)
    notes: tuple[str, ...] = ()
    if artefact and found:
        notes = ("the quote is present; the span reported is the captured text, which "
                 "differs at word boundaries only",)
    elif artefact:
        notes = ("this is a capture artefact to be repaired at the source, not a licence "
                 "to treat the quote as present",)
    return QuoteCheck(
        quote=quote,
        found=found,
        method=outcome.method,
        score=outcome.score,
        document_id=document_id,
        clause_id=clause_id,
        page_number=page_number,
        char_start=outcome.char_start,
        char_end=outcome.char_end,
        text_sha256=sha256_text(text) if text is not None else None,
        mismatch=mismatch,
        differences=differences,
        artefact_note=artefact,
        notes=notes,
    )


def _locate_by_elision(quote: str, text: str) -> tuple[int, int] | None:
    """Where the whitespace-elided forms align, mapped back to the original text."""
    elided_q = _elide_ws(quote)
    if not elided_q:
        return None
    keep = [i for i, ch in enumerate(text) if not ch.isspace()]
    elided_t = "".join(text[i] for i in keep)
    idx = elided_t.find(elided_q)
    if idx < 0 or elided_t.count(elided_q) != 1:
        return None
    start = keep[idx]
    end = keep[idx + len(elided_q) - 1] + 1
    return (start, end)


def check_quote_in_clause(quote: str, clause: CorpusClause,
                          document: CorpusDocument | None = None, **kw) -> QuoteCheck:
    """Check a quote inside one clause, reporting the span in **artifact** coordinates.

    A reader who wants to see the quote in the source reads the artifact, not the clause
    slice, so the offsets are translated: ``clause.char_start + local_start``.
    """
    check = check_quote(
        quote, clause.text,
        document_id=clause.corpus_document_id,
        clause_id=clause.clause_id,
        page_number=clause.page_number,
        **kw,
    )
    if check.char_start is None or check.char_end is None:
        return check
    return replace(check,
                   char_start=clause.char_start + check.char_start,
                   char_end=clause.char_start + check.char_end,
                   notes=check.notes + (
                       f"offsets are artifact coordinates; the clause itself spans "
                       f"{clause.char_start}-{clause.char_end}",))


def best_clause(quote: str, clauses: Iterable[CorpusClause],
                document: CorpusDocument | None = None) -> QuoteCheck | None:
    """The best check across a document's clauses; ``None`` when there are no clauses.

    "Best" is ordered: an accepted match first (highest score), then the closest refusal.
    A document with no stored clauses returns ``None`` — which is not "verified".
    """
    checks = [check_quote_in_clause(quote, c, document) for c in clauses]
    if not checks:
        return None
    accepted = [c for c in checks if c.found]
    if accepted:
        return max(accepted, key=lambda c: (c.score, -c.method_rank))
    return max(checks, key=lambda c: c.score)


def normalise(text: str) -> str:
    """The same normalisation the span verifier uses. Exposed for callers that need it."""
    return normalise_for_match(text)
