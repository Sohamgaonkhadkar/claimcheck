"""Token-level money reading, and the refusals the existing parser does not make.

``claimcheck.calc.money.parse_amount`` is the trust core's only producer of magnitudes and
it is not modified by this module. But it does not *refuse*:

    >>> parse_amount("350,00").paise, parse_amount("350,00").ok
    (3500000, True)          # rupees 35,000 — from a token that means rupees 350.00

``SUSPICIOUS_GROUPING`` is raised as a warning while ``paise`` is still populated, so a
caller that checks ``ok`` accepts a value that is wrong by a factor of 100. That token shape
is not hypothetical: it is exactly the defect that produced the ₹1,706 discrepancy on the
real bill in Phase 3C (``350,00``, ``240,00``, ``320,00``, ``800,00`` — all in the amount
column, all printed as two-decimal money).

So the ingestion layer classifies tokens *before* any magnitude is produced, and refuses
ambiguous ones. A refusal is a first-class outcome here, not an error path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .records import MoneyFormat, MoneyReading

#: A permissive money-ish token: digits with optional group separators and optional
#: 1–2 decimals. Deliberately wider than the trust core's rule, so that *malformed* money
#: is seen and refused rather than not seen at all.
PERMISSIVE_TOKEN = re.compile(r"\d[\d,]*(?:\.\d{1,2})?|\d[\d.]*,\d{1,2}")

#: The rule Phase 3C used and whose results this phase must reproduce: two decimals, full
#: stop, last such token on the line is the amount.
STRICT_TOKEN = re.compile(r"\d[\d,]*\.\d{2}")

#: `350,00` — one comma, two trailing digits, no full stop.
_DECIMAL_COMMA_SHAPE = re.compile(r"^\d{1,3}(?:,\d{2,3})*,\d{2}$")

_GROUPED = re.compile(r"^\d{1,3}(?:,\d{2,3})+$")
_PLAIN_INT = re.compile(r"^\d+$")
_DECIMAL_POINT = re.compile(r"^\d[\d,]*(?:\.\d{1,2})$")


class TokenClass(str, Enum):
    STRICT_DECIMAL = "strict_decimal"        # 1,180.00 — unambiguous two-decimal money
    DECIMAL_COMMA = "decimal_comma"          # 350,00  — ambiguous, refused
    GROUPED_INTEGER = "grouped_integer"      # 1,50,000 / 1,500 — whole rupees
    PLAIN_INTEGER = "plain_integer"          # 350 — whole rupees, no separators
    MALFORMED = "malformed"


@dataclass(frozen=True)
class TokenRead:
    """The classification of one token, before any magnitude is produced."""

    raw: str
    start: int
    end: int
    token_class: TokenClass
    paise: int | None
    alternative_paise: int | None = None
    alternative_reason: str | None = None
    money_format: MoneyFormat = MoneyFormat.PLAIN
    flags: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.paise is None


def _int_paise(digits: str, frac: str = "") -> int:
    """Exact integer paise. No float ever touches a magnitude."""
    value = int(digits or "0") * 100
    if frac:
        value += int((frac + "00")[:2])
    return value


def classify_token(raw: str, start: int = 0, end: int | None = None) -> TokenRead:
    """Classify a money-shaped token. Never repairs, never guesses the separator's role."""
    end = len(raw) if end is None else end
    if _DECIMAL_POINT.match(raw):
        int_part, frac = raw.rsplit(".", 1)
        groups = int_part.replace(",", "")
        if "," in int_part:
            # A validated group structure is required; anything else is ambiguous.
            if not _GROUPED.match(int_part):
                return TokenRead(raw, start, end, TokenClass.MALFORMED, None,
                                 money_format=MoneyFormat.AMBIGUOUS_GROUPING,
                                 flags=("UNPARSEABLE_GROUPING",))
            fmt = MoneyFormat.INDIAN_GROUPED if re.match(r"^\d{1,3}(?:,\d{2})+,\d{3}$",
                                                        int_part) else MoneyFormat.WESTERN_GROUPED
        else:
            fmt = MoneyFormat.PLAIN
        return TokenRead(raw, start, end, TokenClass.STRICT_DECIMAL, _int_paise(groups, frac),
                         money_format=fmt)

    if _DECIMAL_COMMA_SHAPE.match(raw):
        # `350,00`: could be rupees 350.00 (decimal comma) or a malformed grouping of 35000.
        # Both readings are reported; neither is chosen.
        head, tail = raw.rsplit(",", 1)
        decimal_reading = _int_paise(head.replace(",", ""), tail)
        grouped_reading = _int_paise(raw.replace(",", ""))
        return TokenRead(
            raw, start, end, TokenClass.DECIMAL_COMMA, None,
            alternative_paise=decimal_reading,
            alternative_reason=("token has one comma and two trailing digits: 'decimal comma' "
                                f"reads {decimal_reading / 100:.2f} but 'grouped integer' reads "
                                f"{grouped_reading / 100:.2f}; the document does not say which"),
            money_format=MoneyFormat.DECIMAL_COMMA,
            flags=("DECIMAL_COMMA_AMBIGUOUS",))

    if _GROUPED.match(raw):
        return TokenRead(raw, start, end, TokenClass.GROUPED_INTEGER,
                         _int_paise(raw.replace(",", "")),
                         money_format=MoneyFormat.INDIAN_GROUPED)

    if _PLAIN_INT.match(raw):
        return TokenRead(raw, start, end, TokenClass.PLAIN_INTEGER, _int_paise(raw))

    return TokenRead(raw, start, end, TokenClass.MALFORMED, None,
                     money_format=MoneyFormat.UNPARSABLE, flags=("UNPARSEABLE_AMOUNT",))


def find_tokens(text: str, *, strict: bool = False) -> list[TokenRead]:
    """Every money-shaped token on a line, in order, classified.

    ``strict=True`` returns only tokens matching the Phase 3C rule — this is the reading
    that reproduces the previously measured numbers, and it is never the default.
    """
    pattern = STRICT_TOKEN if strict else PERMISSIVE_TOKEN
    out: list[TokenRead] = []
    for m in pattern.finditer(text or ""):
        out.append(classify_token(m.group(0), m.start(), m.end()))
    return out


def moneyish_token_spans(text: str) -> list[tuple[int, int, str]]:
    """Offsets of money-shaped tokens that a strict rule would *miss*.

    Used to explain, from evidence alone, why a line's amount could not be read.
    """
    strict = {(m.start(), m.end()) for m in STRICT_TOKEN.finditer(text or "")}
    out: list[tuple[int, int, str]] = []
    for m in PERMISSIVE_TOKEN.finditer(text or ""):
        if (m.start(), m.end()) not in strict:
            out.append((m.start(), m.end(), m.group(0)))
    return out


# --------------------------------------------------------------------------- pair rule


_QTY_AFTER_X = re.compile(r"[xX×]\s*(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class LineMoney:
    """The money structure of one line, under the tail rule, with refusals intact."""

    amount: TokenRead | None
    amount_rule: str
    rate: TokenRead | None
    quantity_milli: int | None
    strict_tail: TokenRead | None
    hidden_tokens: tuple[tuple[int, int, str], ...] = ()
    flags: tuple[str, ...] = ()

    @property
    def amount_paise(self) -> int | None:
        return self.amount.paise if self.amount else None


def read_line_money(text: str) -> LineMoney:
    """Read the rate / quantity / amount structure of a bill line.

    The rule is the one Phase 3C documented and measured: the amount is the **last**
    money-shaped token on the line. Here the permissive tokeniser is used, so a token the
    strict rule cannot see (``350,00``) is *found and refused* instead of silently falling
    back to the quantity column.
    """
    flags: list[str] = []
    tokens = find_tokens(text)
    strict_tokens = find_tokens(text, strict=True)
    hidden = tuple(moneyish_token_spans(text))
    if not tokens:
        return LineMoney(amount=None, amount_rule="none", rate=None, quantity_milli=None,
                         strict_tail=strict_tokens[-1] if strict_tokens else None)

    amount = tokens[-1]
    rule = "permissive_tail"
    strict_tail = strict_tokens[-1] if strict_tokens else None
    if amount.ambiguous:
        flags.append("AMOUNT_NOT_READABLE")
        if hidden:
            flags.append("TOKEN_MISSED_BY_STRICT_RULE")
    elif strict_tail is not None and (strict_tail.start, strict_tail.end) != (amount.start,
                                                                             amount.end):
        # The strict tail and the permissive tail disagree about which token is the amount.
        flags.append("TAIL_RULE_AMBIGUOUS")

    # rate / quantity: the money token immediately before "x <number>"
    rate: TokenRead | None = None
    quantity_milli: int | None = None
    for m in _QTY_AFTER_X.finditer(text):
        qty_text = m.group(1)
        before = [t for t in tokens if t.end <= m.start()]
        if before:
            rate = before[-1]
            try:
                quantity_milli = int(round(float(qty_text) * 1000))
            except ValueError:                       # pragma: no cover - regex guarantees
                quantity_milli = None
            break

    return LineMoney(amount=amount, amount_rule=rule, rate=rate,
                     quantity_milli=quantity_milli, strict_tail=strict_tail,
                     hidden_tokens=hidden, flags=tuple(flags))


def as_reading(line_money: LineMoney, reader: str, rule: str) -> MoneyReading:
    """Convert a line's money structure into a :class:`MoneyReading` for the dual contract."""
    if rule == "strict_tail":
        tok = line_money.strict_tail
        if tok is None:
            return MoneyReading(reader=reader, rule=rule, raw="", paise=None,
                                money_format=MoneyFormat.UNPARSABLE,
                                flags=("STRICT_RULE_FOUND_NO_TOKEN",))
        return MoneyReading(reader=reader, rule=rule, raw=tok.raw, paise=tok.paise,
                            money_format=tok.money_format,
                            flags=tok.flags + ("STRICT_TAIL_FALLBACK",)
                            if line_money.amount_rule == "permissive_tail"
                            and line_money.amount and line_money.amount.ambiguous else tok.flags)
    tok = line_money.amount
    if tok is None:
        return MoneyReading(reader=reader, rule=rule, raw="", paise=None,
                            money_format=MoneyFormat.UNPARSABLE,
                            flags=("NO_MONEY_TOKEN",))
    return MoneyReading(reader=reader, rule=rule, raw=tok.raw, paise=tok.paise,
                        money_format=tok.money_format,
                        alternative_paise=tok.alternative_paise,
                        alternative_reason=tok.alternative_reason,
                        flags=tok.flags)
