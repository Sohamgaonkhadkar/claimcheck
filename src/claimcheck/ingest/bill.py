"""Hospital-bill parsing from the structures that were actually observed (Phase 3D §6).

The parser is built from the real bills, not from an idea of what a bill looks like. What
was observed in the acquired corpus, and what is therefore supported:

* lines shaped ``<serial.> <date> <code> <LABEL …> <RATE> x <QTY> <AMOUNT>``, where the label
  is a drug or consumable with a strength, a pack size and sometimes a batch code;
* the same line repeated once per day with a different date (ward and NICU charges);
* category subtotal rows whose words say so (``Total of BED CHARGES``, ``Total of PHARMACY
  CHARGE:``), and totals (``Grand Total``, ``Net Amount Payable``);
* amounts printed with two decimals, Indian grouping (``52,868.25``), and — on the real
  corpus — a decimal comma where the scanner lost the full stop (``350,00``);
* stray artefacts: ``240,00``, ``350,00 x`` with the amount column unreadable, ``240.00.``
  with a doubled full stop.

What the parser will not do:

* assume a fixed column position — the amount is found by the tail rule and cross-checked
  against the reader that has geometry, so a layout with different column counts still works;
* repair a number. A token it cannot read unambiguously is refused and quarantined;
* decide anything. No category, no legality, no verdict: those belong to the rule and
  verdict layers (§10).
"""

from __future__ import annotations

import hashlib
import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .dual import DualMoneyFact, Agreement, FactDisposition, Quarantine, reconcile_money
from .model import EditOp, TextLine
from .money import LineMoney, as_reading, find_tokens, read_line_money
from .records import (
    BillLine,
    BillSubtotal,
    BillTotal,
    MoneyReading,
    ParsedBill,
    SourceEvidence,
)

# --------------------------------------------------------------------------- vocabulary

#: Rows that are a *summary of other rows*, not billable items. Recognised by their words.
#: Phase 3C counted these as line items and produced a ₹59,612 error on one document.
SUMMARY_ROW = re.compile(
    r"\btotal\b|\bsub\s*-?total\b|\bgrand\s+total\b|\bnet\s+amount\b|"
    r"\bamount\s+payable\b|\bamount\s+received\b|\bbalance\s+(?:due|payable)\b|"
    r"\bto\s+be\s+refund\b|\brefund\b|\bcash\s+discount\b|\bround\s*-?\s*off\b|"
    r"\badvance\s+(?:paid|received)\b|\bgross\s+amount\b|\bcompany\s+credit\s+limit\b|"
    r"\bdeposit\b|\bbill\s+amount\b|\bnet\s+payable\b", re.I)

SUBTOTAL_ROW = re.compile(r"\bsub\s*-?total\b|\btotal\s+of\b", re.I)
GRAND_TOTAL_ROW = re.compile(r"\bgrand\s+total\b", re.I)
NET_PAYABLE_ROW = re.compile(r"\bnet\s+amount\s+payable\b|\bnet\s+payable\b|\bnet\s+amount\b", re.I)

#: Header/footer lines that look like bill lines but are not.
NOT_A_LINE = re.compile(
    r"^(?:page\s+\d|bill\s+no|date\b|s\.?\s*no|particulars|description|amount\b|rate\b|"
    r"qty|company\s+amount|service\s+provider|printed\s+on|patient\s+name|address|uhid|"
    r"ip\s+no|mrn|doctor|ward\b|bed\b|speciality|admission|discharge|gstin|pan\b|"
    r"licence|valid\s+upto|authorization|interim\s+bill|bill\s+of\s+supply)\b", re.I)

_DATE_PREFIX = re.compile(r"^\s*(?:\d{1,3}[.,)]?\s+)?"
                         r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[/-][A-Za-z]{3}[/-]\d{2,4}"
                         r"|\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})?\s*")
_CODE = re.compile(r"\b(?:[A-Z]{1,4}[0-9][A-Z0-9]{2,10})\b")
_LEADING_SERIAL = re.compile(r"^\s*(?:\d{1,3}\s*[.,)]?\s+|[A-Za-z]{1,2}\s+(?=\d{1,2}/))")
_TRAILING_QTY = re.compile(r"\s+(?:x|X|\u00d7)\s*$")


# --------------------------------------------------------------------------- line pairing


@dataclass(frozen=True)
class AlignedLine:
    """One visual line as seen by reader A, matched to reader B when possible."""

    layout: TextLine
    native: TextLine | None
    match_score: float

    @property
    def readers(self) -> tuple[str, ...]:
        return ("layout", "native") if self.native is not None else ("layout",)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def align_lines(layout_lines: Sequence[TextLine], native_lines: Sequence[TextLine],
                *, min_score: float = 0.55) -> list[AlignedLine]:
    """Match the two readers' lines in order, with a small lookahead window.

    Order is preserved on both sides, so a greedy walk with a bounded window is both
    correct in practice and linear; a full similarity matrix would be quadratic on an
    830-line bill. A line that cannot be matched stays single-reader, which by the
    contract means any fact read from it is quarantined rather than trusted.
    """
    out: list[AlignedLine] = []
    j = 0
    window = 6
    for i, line in enumerate(layout_lines):
        if not line.text.strip():
            continue
        best: tuple[float, int] | None = None
        limit = min(len(native_lines), j + window)
        for k in range(j, limit):
            cand = native_lines[k]
            if not cand.text.strip():
                continue
            score = difflib.SequenceMatcher(None, _norm(line.text), _norm(cand.text),
                                            autojunk=False).quick_ratio()
            if score >= min_score and (best is None or score > best[0]):
                best = (score, k)
        if best is not None:
            score, k = best
            match = difflib.SequenceMatcher(None, _norm(line.text), _norm(native_lines[k].text),
                                            autojunk=False).ratio()
            if match >= min_score:
                out.append(AlignedLine(layout=line, native=native_lines[k],
                                       match_score=round(match, 4)))
                j = k + 1
                continue
        out.append(AlignedLine(layout=line, native=None, match_score=0.0))
    return out


# --------------------------------------------------------------------------- line reading


def _label_of(text: str) -> tuple[str, str | None, str | None]:
    """Split a line into label, date and code. Structure only — no interpretation."""
    body = _LEADING_SERIAL.sub("", text.strip())
    date_text = None
    m = _DATE_PREFIX.match(body)
    if m and m.group(1):
        date_text = m.group(1)
        body = body[m.end():]
    else:
        dm = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4}\b",
                       body)
        if dm:
            date_text = dm.group(0)
            body = body[:dm.start()] + " " + body[dm.end():]
    code = None
    cm = _CODE.search(body)
    if cm:
        code = cm.group(0)
    # the label is everything before the first money-shaped token
    cut = re.search(r"\d[\d,]*[.,]\d{2}", body)
    label = body[:cut.start()] if cut else body
    label = re.sub(r"\s{2,}", " ", label).strip(" |-:_;,")
    return label, date_text, code


#: A tariff/product code is a bare integer; on the real corpus they appear as `999311`,
#: `999316` (a department code) and inside parentheses. A bare integer that is the *only*
#: numeric token on a line, with no date and no `x quantity` structure, is a code — reading
#: it as money would produce a line of "Rs 9,99,311" out of a department identifier. That is
#: a refusal, not a parse.
_CODE_SHAPED = re.compile(r"^\d{5,8}$")

#: Labels that mark a row as belonging to the *summary* of a bill rather than to the list of
#: charges. On the real corpus these rows carry a two-decimal money value and would otherwise
#: pass every other test: `Service Amount 2,20,976.30`, `Paid Amount 18,500.00`,
#: `ADVANCE RECEIVED (BEARER 750) -`, `Balance Amt : 2,067.70`. Counting them as charges
#: inflated one bill's line sum to Rs 1.07 crore.
#: Deliberately narrow: the bare words "discount" and "payable" are *not* here, because a
#: charge row can legitimately say "payable at discharge"; only the unambiguous summary
#: phrases are listed.
_SUMMARY_VOCABULARY = re.compile(
    r"\b(?:service\s+amount|bill\s+amount|amount\s+(?:payable|paid|received|refund\w*)|"
    r"paid\s+amount|net\s+(?:amount|payable|amt)|balance\s+(?:amt|amount|due|payable)|"
    r"advance\s+(?:received|paid|payment)|after\s+discount|total\s+discount|"
    r"grand\s+total|sub\s*[- ]?total|refund(?:ed)?\s+to\s+(?:the\s+)?patient)\b",
    re.IGNORECASE)


def item_line_verdict(text: str, money: LineMoney | None = None) -> tuple[bool, str]:
    """Is this visual line a bill item? Returns ``(is_item, reason_when_not)``.

    The gate exists because a tail-money rule alone will happily turn a department code into
    a charge. Each rejection names its reason so the count of rejected lines can be reported
    rather than guessed at.
    """
    money = money or read_line_money(text)
    if _SUMMARY_VOCABULARY.search(text) and summary_kind(text) is None:
        return False, "SUMMARY_VOCABULARY"
    has_strict_money = money.strict_tail is not None
    has_qty_structure = money.quantity_milli is not None and money.rate is not None
    has_date = bool(re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|"
                              r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4}\b", text))
    if not (has_strict_money or has_qty_structure):
        if not has_date:
            return False, "NO_MONEY_NO_DATE"
    if has_date:
        return True, ""
    if has_qty_structure:
        return True, ""
    # no date and no quantity: only accept it if a two-decimal amount is present AND the
    # remaining numeric tokens are not all code-shaped
    if not has_strict_money:
        return False, "CODE_SHAPED_ONLY"
    others = [t.raw for t in find_tokens(text) if t is not money.amount]
    if others and all(_CODE_SHAPED.match(o) for o in others) and money.amount is not None \
            and _CODE_SHAPED.match(money.amount.raw or ""):
        return False, "CODE_SHAPED_ONLY"
    return True, ""


def summary_kind(text: str) -> str | None:
    if GRAND_TOTAL_ROW.search(text):
        return "grand_total"
    if NET_PAYABLE_ROW.search(text):
        return "net_payable"
    if SUBTOTAL_ROW.search(text):
        return "subtotal"
    if SUMMARY_ROW.search(text):
        return "other_summary"
    return None


def _evidence(document_id: str, page: int, line: TextLine, reader: str, method: EditOp,
              source_sha256: str, page_sha: str) -> SourceEvidence:
    return SourceEvidence(
        document_id=document_id, page_number=page, text=line.text,
        char_start=line.char_start, char_end=line.char_end, reader=reader,
        extraction_method=method, source_sha256=source_sha256, page_text_sha256=page_sha,
        bbox=line.bbox, bbox_is_measured=(reader == "layout"),
        ocr_confidence=line.ocr_confidence)


def read_bill_line(*, document_id: str, page_number: int, line_no: int, aligned: AlignedLine,
                   source_sha256: str, page_sha: str,
                   method: EditOp = EditOp.OCR) -> BillLine:
    """Read one line: label structure, the dual-read amount fact, and the refusals."""
    layout_text = aligned.layout.text
    layout_money = read_line_money(layout_text)
    native_money = read_line_money(aligned.native.text) if aligned.native else None

    readings: list[MoneyReading] = []
    # Rule 1 — the Phase 3C tail rule, applied to both readers. Determines the *as read*
    # value used to reproduce the previous phase's measurement.
    r_layout_strict = as_reading(layout_money, "layout", "strict_tail")
    r_layout_strict = _with_evidence(r_layout_strict, _evidence(
        document_id, page_number, aligned.layout, "layout", method, source_sha256, page_sha))
    readings.append(r_layout_strict)
    if aligned.native is not None and native_money is not None:
        r_native_strict = as_reading(native_money, "native", "strict_tail")
        r_native_strict = _with_evidence(r_native_strict, _evidence(
            document_id, page_number, aligned.native, "native", method, source_sha256,
            page_sha))
        readings.append(r_native_strict)

    # Rule 2 — the permissive tail rule, which *sees* a token the strict rule cannot and
    # therefore refuses rather than falling back to the quantity.
    r_layout_perm = as_reading(layout_money, "layout", "permissive_tail")
    r_layout_perm = _with_evidence(r_layout_perm, _evidence(
        document_id, page_number, aligned.layout, "layout", method, source_sha256, page_sha))
    readings.append(r_layout_perm)
    if aligned.native is not None and native_money is not None:
        r_native_perm = as_reading(native_money, "native", "permissive_tail")
        r_native_perm = _with_evidence(r_native_perm, _evidence(
            document_id, page_number, aligned.native, "native", method, source_sha256,
            page_sha))
        readings.append(r_native_perm)

    fact_strict = reconcile_money(subject=f"billline:{page_number}:{line_no}", rule="strict_tail",
                                  readings=[r for r in readings if r.rule == "strict_tail"])
    fact_perm = reconcile_money(subject=f"billline:{page_number}:{line_no}",
                                rule="permissive_tail",
                                readings=[r for r in readings if r.rule == "permissive_tail"])

    label, date_text, code = _label_of(layout_text)
    kind = summary_kind(layout_text)
    hidden = layout_money.hidden_tokens

    flags: list[str] = list(layout_money.flags)
    if aligned.native is None:
        flags.append("LINE_UNMATCHED_BETWEEN_READERS")
    if fact_perm.disposition is not FactDisposition.USABLE:
        flags.append(f"AMOUNT_{fact_perm.disposition.value.upper()}")
    if hidden:
        flags.append("MONEY_TOKEN_MISSED_BY_STRICT_RULE")
    if _TRAILING_QTY.search(layout_text):
        flags.append("AMOUNT_COLUMN_UNREADABLE_TRAILING_X")
    if re.search(r"\d\.\d{2}\.(?=\s|$)", layout_text):
        flags.append("DOUBLED_FULL_STOP")
    if kind is not None:
        flags.append(f"SUMMARY_ROW:{kind}")

    line_id = "BL-" + hashlib.sha256(
        f"{document_id}|{page_number}|{line_no}|{layout_text}".encode("utf-8")
    ).hexdigest()[:16].upper()

    amount_paise = fact_perm.paise if fact_perm.disposition is FactDisposition.USABLE else None
    evidence = _evidence(document_id, page_number, aligned.layout, "layout", method,
                         source_sha256, page_sha)
    return BillLine(
        line_id=line_id, document_id=document_id, page_number=page_number,
        line_number=line_no, raw_label=layout_text, label=label,
        rate_paise=(layout_money.rate.paise if layout_money.rate else None),
        quantity_milli=layout_money.quantity_milli, amount_paise=amount_paise,
        amount_readings=tuple(readings),
        printed_amount_tokens=tuple(t for _s, _e, t in hidden),
        date_text=date_text, code=code, is_summary_row=kind is not None,
        summary_kind=kind, evidence=evidence, flags=tuple(flags))


def _with_evidence(reading: MoneyReading, evidence: SourceEvidence) -> MoneyReading:
    import dataclasses

    return dataclasses.replace(reading, evidence=evidence)


def as_read_paise(line: BillLine) -> int | None:
    """The **Phase 3C tail rule's** value for this line, if it produced one.

    This is a *measurement of the previous phase's rule*, kept so the known real-bill
    discrepancy can be reproduced and so a repair can be shown to fix it. It is **not** a
    fact the trust core may consume: where the readers disagreed, or the token was
    ambiguous, the contract's own ``amount_paise`` is ``None`` and the line is quarantined.
    Everywhere the two agree, they are equal.
    """
    for r in line.amount_readings:
        if r.rule == "strict_tail" and r.reader == "layout":
            return r.paise
    return None


# --------------------------------------------------------------------------- document


@dataclass
class BillParseResult:
    bill: ParsedBill
    quarantine: Quarantine
    amount_facts: list[DualMoneyFact]
    line_alignment: dict[str, float]
    rejected_lines: tuple[dict[str, Any], ...] = ()

    @property
    def usable_amounts(self) -> int:
        return sum(1 for f in self.amount_facts if f.disposition is FactDisposition.USABLE)


def parse_bill(document_id: str, *, pages_layout: dict[int, Any], pages_native: dict[int, Any],
               source_sha256: str) -> BillParseResult:
    """Parse a bill from both readers' page extractions.

    ``pages_layout`` / ``pages_native`` map page number to :class:`PageExtraction`. Lines
    the layout reader found that the native reader did not are kept — and their monetary
    facts are quarantined, not dropped, so the count of refused facts stays visible.
    """
    lines: list[BillLine] = []
    subtotals: list[BillSubtotal] = []
    totals: list[BillTotal] = []
    facts: list[DualMoneyFact] = []
    quarantine = Quarantine()
    alignment_scores: dict[str, float] = {}
    rejected: list[dict[str, Any]] = []
    line_no = 0

    for page_number in sorted(pages_layout):
        layout = pages_layout[page_number]
        native = pages_native.get(page_number)
        layout_lines = list(layout.lines)
        native_lines = list(native.lines) if native is not None else []
        page_sha = hashlib.sha256(layout.text.encode("utf-8")).hexdigest()
        method = layout.method
        aligned = align_lines(layout_lines, native_lines)
        matched = sum(1 for a in aligned if a.native is not None)
        alignment_scores[str(page_number)] = round(matched / len(aligned), 4) if aligned else 0.0

        for a in aligned:
            text = a.layout.text.strip()
            if len(text) < 4 or NOT_A_LINE.match(text):
                continue
            money = read_line_money(text)
            if not money.amount and not money.strict_tail:
                continue
            # need at least three letters to be a label rather than a stray figure
            if len(re.findall(r"[A-Za-z]", text)) < 3:
                continue
            is_item, reject_reason = item_line_verdict(text, money)
            kind_probe = summary_kind(text)
            if not is_item and kind_probe is None:
                rejected.append({"page": page_number, "text_is_redacted": True,
                                 "reason": reject_reason, "chars": len(text)})
                continue
            line_no += 1
            bl = read_bill_line(document_id=document_id, page_number=page_number,
                                line_no=line_no, aligned=a, source_sha256=source_sha256,
                                page_sha=page_sha, method=method)
            lines.append(bl)
            if bl.is_summary_row:
                if bl.summary_kind == "subtotal":
                    subtotals.append(_subtotal(bl, text))
                else:
                    totals.append(_total(bl, text, bl.summary_kind or "other_summary"))
            for rule in ("strict_tail", "permissive_tail"):
                reading_set = [r for r in bl.amount_readings if r.rule == rule]
                if not reading_set:
                    continue
                fact = reconcile_money(subject=bl.line_id, rule=rule, readings=reading_set)
                facts.append(fact)
                quarantine.add(fact)

    bill = ParsedBill(document_id=document_id, lines=lines, subtotals=subtotals, totals=totals,
                      pages_read=len(pages_layout),
                      readers_used=tuple(sorted({p.reader for p in pages_layout.values()})))
    return BillParseResult(bill=bill, quarantine=quarantine, amount_facts=facts,
                           line_alignment=alignment_scores, rejected_lines=tuple(rejected))


def _subtotal(line: BillLine, text: str) -> BillSubtotal:
    category = SUBTOTAL_ROW.sub("", text)
    category = re.sub(r"[^A-Za-z &/\-]", " ", category)
    category = re.sub(r"\s{2,}", " ", category).strip(" :-")
    return BillSubtotal(subtotal_id=line.line_id, document_id=line.document_id,
                        page_number=line.page_number, category_text=category,
                        amount_paise=as_read_paise(line) if as_read_paise(line) is not None
                        else line.amount_paise,
                        amount_readings=line.amount_readings, evidence=line.evidence)


def _total(line: BillLine, text: str, kind: str) -> BillTotal:
    return BillTotal(total_id=line.line_id, document_id=line.document_id,
                     page_number=line.page_number, kind=kind,
                     amount_paise=(as_read_paise(line) if as_read_paise(line) is not None
                                   else line.amount_paise),
                     amount_readings=line.amount_readings, evidence=line.evidence)


def bill_stats(result: BillParseResult) -> dict[str, Any]:
    bill = result.bill
    labels = [l.label for l in bill.item_lines if l.label]
    return {
        "document_id": bill.document_id,
        "pages": bill.pages_read,
        "lines": len(bill.lines),
        "item_lines": len(bill.item_lines),
        "summary_rows": len(bill.lines) - len(bill.item_lines),
        "subtotals": len(bill.subtotals),
        "totals": len(bill.totals),
        "distinct_labels": len({_norm(l) for l in labels}),
        "lines_with_usable_amount": sum(1 for l in bill.item_lines if l.has_usable_amount),
        "quarantined_lines": sum(1 for l in bill.item_lines if not l.has_usable_amount),
        "amount_facts": len(result.amount_facts),
        "usable_amount_facts": result.usable_amounts,
        "quarantined_amount_facts": result.quarantine.count,
        "quarantine_by_agreement": result.quarantine.by_agreement(),
        "line_alignment": result.line_alignment,
        "lines_rejected_as_not_items": len(result.rejected_lines),
        "rejection_reasons": _counts(r.get("reason", "") for r in result.rejected_lines),
    }


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))
