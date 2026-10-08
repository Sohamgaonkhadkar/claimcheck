"""Indian money parsing, validation and formatting. Deterministic. No model involved.

Contract (Phase-2 architecture §17):

    raw_string -> normalised_string -> parsed_amount(paise) -> validation -> confidence

Rules enforced here:

* Money is integer **paise**. Ratios/percentages are exact ``Fraction``.
* The raw printed string is always retained alongside the parsed value.
* An LLM may transcribe a string; it may never produce a magnitude. This module is
  the only producer of numeric values from document text.
* Ambiguity is flagged, never silently resolved. Flags travel with the value so the
  quality gate can act on them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------
CURRENCY_MARKERS: tuple[str, ...] = (
    "\u20b9",  # ₹
    "inr",
    "rs.",
    "rs",
    "re.",
    "re",
    "rupees",
    "rupaye",
    "rupaiya",
)

_TRAILING_MARKERS: tuple[str, ...] = ("/-", "/=", "only", "onli", ".00/-")

UNIT_WORDS: dict[str, int] = {
    "crore": 10_000_000,
    "crores": 10_000_000,
    "cr": 10_000_000,
    "crs": 10_000_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "lak": 100_000,
    "thousand": 1_000,
    "thousands": 1_000,
    "hazaar": 1_000,
    "hazar": 1_000,
    "hazar": 1_000,
    "k": 1_000,
}

# "L" is only a unit when it is attached/adjacent to a number ("1.5 L"), which is
# also how a litre could be written. We accept it but flag the ambiguity.
_AMBIGUOUS_UNIT_SUFFIXES: dict[str, int] = {"l": 100_000, "lakh": 100_000}

_ONES: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "forty": 40, "fourty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES: dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}
_NUMBER_WORDS = set(_ONES) | set(_TENS) | set(_SCALES)

# OCR confusion pairs seen on Indian bills. Applied only inside an otherwise
# numeric token, and only when the correction produces a valid numeric shape.
_OCR_MAP: dict[str, str] = {
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "l": "1", "I": "1", "|": "1", "i": "1", "!": "1", "L": "1",
    "S": "5", "s": "5",
    "B": "8",
    "Z": "2", "z": "2",
    "G": "6", "b": "6",
    "q": "9", "g": "9",
    "T": "7", "?": "7",
    "A": "4",
}
_OCR_CANDIDATE_CHARS = set("0123456789,.") | set(_OCR_MAP)

_GROUPABLE = re.compile(r"^[0-9][0-9,]*$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedAmount:
    """The full parse record. ``paise`` is ``None`` only when the input failed."""

    raw: str
    normalised: str
    paise: int | None
    format_detected: str = "plain"
    unit_multiplier: int = 1
    ocr_corrected: bool = False
    flags: tuple[str, ...] = ()
    confidence: float = 1.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.paise is not None and self.error is None

    def as_fraction(self) -> Fraction:
        if self.paise is None:
            raise ValueError("no value parsed")
        return Fraction(self.paise, 100)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "warn"  # 'warn' | 'block'


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "block")

    @property
    def ok(self) -> bool:
        return not self.blocking

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalise_text(raw: str) -> str:
    """NFKC, unify spaces/dashes/quotes, strip control chars. Matching only."""
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = "".join(ch for ch in s if ch == " " or unicodedata.category(ch)[0] != "C")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _digits_only(s: str) -> str:
    return re.sub(r"[^0-9]", "", s)


# ---------------------------------------------------------------------------
# Grouping analysis (the lakh/crore trap)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GroupingAnalysis:
    value_digits: str
    style: str  # 'plain' | 'indian' | 'western' | 'ambiguous_equal' | 'invalid'
    flags: tuple[str, ...] = ()


def analyse_grouping(int_part: str) -> GroupingAnalysis:
    """Decide what ``1,83,500`` means, and what ``1,50`` means.

    Indian grouping:  first group 1-3 digits, middle groups exactly 2, final group 3
                      -> 1,83,500 is one hundred eighty-three thousand five hundred.
    Western grouping: first group 1-3 digits, every later group exactly 3
                      -> 183,500 is the same number; 1,500 is ambiguous by style but
                         identical in value.
    Malformed (e.g. ``1,50``): parsed for its digits and **flagged**, never silently
    reinterpreted as a decimal.
    """
    if "," not in int_part:
        return GroupingAnalysis(_digits_only(int_part), "plain")
    groups = int_part.split(",")
    if any(not g.isdigit() for g in groups) or not groups[0] or len(groups[0]) > 3:
        return GroupingAnalysis(_digits_only(int_part), "invalid", ("SUSPICIOUS_GROUPING",))
    tail = groups[1:]
    if not tail:
        return GroupingAnalysis(_digits_only(int_part), "plain")
    if len(tail) == 1 and len(tail[0]) == 3:
        # "1,500" / "183,500": valid under both styles, same digits either way.
        return GroupingAnalysis(_digits_only(int_part), "ambiguous_equal")
    middle, last = tail[:-1], tail[-1]
    indian = all(len(g) == 2 for g in middle) and len(last) == 3
    western = all(len(g) == 3 for g in tail)
    if indian:
        return GroupingAnalysis(_digits_only(int_part), "indian")
    if western:
        return GroupingAnalysis(_digits_only(int_part), "western")
    return GroupingAnalysis(_digits_only(int_part), "invalid", ("SUSPICIOUS_GROUPING",))


# ---------------------------------------------------------------------------
# OCR repair
# ---------------------------------------------------------------------------
def repair_ocr(token: str) -> tuple[str, bool]:
    """Correct OCR confusions inside a numeric-shaped token.

    Guards against rewriting words: every character must be a plausible digit
    substitute, the corrected token must be a valid number with at least two digits,
    and no more than half the characters may change.
    """
    if not token:
        return token, False
    if not set(token) <= _OCR_CANDIDATE_CHARS:
        return token, False
    if not any(ch in _OCR_MAP for ch in token):
        return token, False
    corrected = "".join(_OCR_MAP.get(ch, ch) for ch in token)
    if not re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", corrected):
        return token, False
    if sum(ch.isdigit() for ch in corrected) < 2:
        return token, False
    # Count *distinct* substitutions, not occurrences: "l,OOO" is two confusions
    # repeated, not four independent errors.
    subs = {(a, b) for a, b in zip(token, corrected) if a != b}
    if not subs or len(subs) > max(1, len(token) // 2):
        return token, False
    return corrected, True


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
_UNIT_RE = re.compile(
    r"(?P<num>[0-9][0-9,\s]*(?:\.[0-9]+)?)\s*(?P<unit>crores?|crs?|lakhs?|lacs?|lak|thousands?|haza?r|k)\b",
    re.IGNORECASE,
)
_SUFFIX_UNIT_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[LlKk])\b")
_OCR_ISH = "0-9OoIlSsBbZzGgDqQAT?!|,.\\-"
_AMOUNT_TOKEN_RE = re.compile(rf"[{_OCR_ISH}]*[0-9OoIlSsBbZzGgDqQAT?!|][{_OCR_ISH}]*")


def _strip_markers(s: str) -> tuple[str, list[str]]:
    """Remove currency symbols/words and trailing qualifiers.

    Word markers are matched with word boundaries only, so the "re" inside "crore"
    is never eaten (a bug that silently turned "2.11 crore" into "2.11 co").
    """
    flags: list[str] = []
    out = s
    if "\u20b9" in out or "\u20a8" in out:
        flags.append("CURRENCY_MARKER")
        out = out.replace("\u20b9", " ").replace("\u20a8", " ")
    word_marker = re.compile(r"\b(inr|rs|re|rupees?|rupaye|rupaiya)\b\.?", re.IGNORECASE)
    if word_marker.search(out):
        flags.append("CURRENCY_MARKER")
        out = word_marker.sub(" ", out)
    trailing = re.compile(r"(/\s*-|/\s*=|-\s*/|\bonly\b)\.?\s*$", re.IGNORECASE)
    if trailing.search(out):
        out = trailing.sub("", out)
    return out.strip(" .-:"), flags


def _extract_with_unit(text: str) -> tuple[str, int, bool, bool]:
    """Return (numeric_token, multiplier, unit_found, unit_ambiguous)."""
    m = _UNIT_RE.search(text)
    if m:
        return m.group("num").strip(), UNIT_WORDS[m.group("unit").lower()], True, False
    m2 = _SUFFIX_UNIT_RE.search(text)
    if m2:
        unit = m2.group("unit").lower()
        if unit == "k":
            return m2.group("num"), 1_000, True, False
        return m2.group("num"), _AMBIGUOUS_UNIT_SUFFIXES["l"], True, True
    return text, 1, False, False


def parse_amount(raw: str) -> ParsedAmount:
    """Parse one printed Indian money string into integer paise."""
    if raw is None or not str(raw).strip():
        return ParsedAmount(raw=str(raw), normalised="", paise=None,
                            error="EMPTY", confidence=0.0)

    s = normalise_text(str(raw))
    stripped, flags = _strip_markers(s)
    flags = list(flags)

    # Bare number-words ("Rupees One Lakh Eighty Three Thousand Only").
    tokens = re.findall(r"[A-Za-z]+", stripped.lower())
    if tokens and not any(ch.isdigit() for ch in stripped):
        word_tokens = [t for t in tokens if t in _NUMBER_WORDS or t in ("and", "rupees", "only", "paise")]
        if any(t in _NUMBER_WORDS for t in word_tokens):
            value = _words_to_int(word_tokens)
            if value is None:
                return ParsedAmount(raw=str(raw), normalised=stripped, paise=None,
                                    error="WORDS_UNPARSED", confidence=0.0)
            return ParsedAmount(
                raw=str(raw), normalised=stripped, paise=value * 100,
                format_detected="words", confidence=0.7,
                flags=tuple(sorted(set(flags + ["PARsed_FROM_WORDS"]))),
            )

    body, multiplier, unit_found, unit_ambiguous = _extract_with_unit(stripped)
    if unit_ambiguous:
        flags.append("UNIT_AMBIGUOUS_L")
    if unit_found:
        flags.append("UNIT_WORD")

    token_match = _AMOUNT_TOKEN_RE.search(body)
    if not token_match:
        return ParsedAmount(raw=str(raw), normalised=stripped, paise=None,
                            error="NO_NUMERIC_TOKEN", confidence=0.0)
    token = token_match.group(0).strip().strip(".-")
    token = re.sub(r"\s+", "", token)

    ocr_corrected = False
    if not re.fullmatch(r"\d[\d,]*(\.\d+)?", token):
        token, ocr_corrected = repair_ocr(token)
        if ocr_corrected:
            flags.append("OCR_CORRECTED")
        else:
            return ParsedAmount(raw=str(raw), normalised=stripped, paise=None,
                                error="INVALID_NUMERIC_TOKEN", confidence=0.0)

    if "." in token:
        int_part, frac_part = token.split(".", 1)
        if len(frac_part) > 2:
            flags.append("SUSPICIOUS_DECIMALS")
    else:
        int_part, frac_part = token, ""

    grouping = analyse_grouping(int_part)
    flags.extend(grouping.flags)
    value = Fraction(int(grouping.value_digits or "0"))
    if frac_part:
        value += Fraction(int(frac_part), 10 ** len(frac_part))
    value *= multiplier

    paise_exact = value * 100
    paise = int(paise_exact)
    if paise_exact != paise:  # fractional paise: record, then round half-up
        paise = int(paise_exact + Fraction(1, 2))
        flags.append("ROUNDED_TO_PAISE")

    confidence = 1.0
    confidence -= 0.15 if ocr_corrected else 0.0
    confidence -= 0.30 if "SUSPICIOUS_GROUPING" in flags else 0.0
    confidence -= 0.10 if "SUSPICIOUS_DECIMALS" in flags else 0.0
    confidence -= 0.05 if "UNIT_AMBIGUOUS_L" in flags else 0.0
    confidence -= 0.02 * len(set(flags) - {"CURRENCY_MARKER", "UNIT_WORD"})
    confidence = max(0.0, min(1.0, round(confidence, 3)))

    return ParsedAmount(
        raw=str(raw),
        normalised=stripped,
        paise=paise,
        format_detected=("unit_word" if unit_found else grouping.style),
        unit_multiplier=multiplier,
        ocr_corrected=ocr_corrected,
        flags=tuple(sorted(set(flags))),
        confidence=confidence,
    )


def parse_money(raw: str) -> int:
    """Convenience: paise or raise. Use ``parse_amount`` when you need the flags."""
    p = parse_amount(raw)
    if not p.ok:
        from ..errors import MoneyParseError

        raise MoneyParseError(f"could not parse amount: {raw!r} ({p.error})", raw=raw, flags=p.flags)
    return int(p.paise)


# ---------------------------------------------------------------------------
# Number words
# ---------------------------------------------------------------------------
def _words_to_int(tokens: Sequence[str]) -> int | None:
    total = 0
    current = 0
    seen = False
    for tok in tokens:
        if tok in ("and", "rupees", "only", "paise"):
            continue
        if tok in _ONES or tok in _TENS:
            current += _ONES.get(tok, _TENS.get(tok, 0))
            seen = True
        elif tok == "hundred":
            current = (current or 1) * 100
            seen = True
        elif tok in _SCALES and _SCALES[tok] >= 1000:
            total += (current or 1) * _SCALES[tok]
            current = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + current


def parse_number_words(text: str) -> int | None:
    """Parse English/Indian number words into an integer rupee amount."""
    tokens = re.findall(r"[A-Za-z]+", normalise_text(text).lower())
    tokens = [t for t in tokens if t not in ("rupees", "only", "and")]
    return _words_to_int(tokens)


_ONES_OUT = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
             "seventeen", "eighteen", "nineteen"]
_TENS_OUT = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _two_digit_words(n: int) -> str:
    if n < 20:
        return _ONES_OUT[n]
    return (_TENS_OUT[n // 10] + (" " + _ONES_OUT[n % 10] if n % 10 else "")).strip()


def _int_to_words_indian(n: int) -> str:
    if n == 0:
        return "zero"
    parts: list[str] = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    hundred, rest = divmod(n, 100)
    if crore:
        parts.append(f"{_int_to_words_indian(crore)} crore")
    if lakh:
        parts.append(f"{_two_digit_words(lakh)} lakh")
    if thousand:
        parts.append(f"{_two_digit_words(thousand)} thousand")
    if hundred:
        parts.append(f"{_ONES_OUT[hundred]} hundred")
    if rest:
        parts.append(_two_digit_words(rest))
    return " ".join(parts)


def amount_in_words(paise: int) -> str:
    """Render paise as Indian-system words (used to compare against printed words)."""
    sign = "minus " if paise < 0 else ""
    paise = abs(paise)
    rupees, p = divmod(paise, 100)
    out = f"{sign}{_int_to_words_indian(rupees)} rupees"
    if p:
        out += f" and {_two_digit_words(p)} paise"
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_inr(paise: int, *, decimals: int = 2, symbol: str = "\u20b9") -> str:
    """Format paise with Indian digit grouping: ``₹1,83,500.00``."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, rem = divmod(paise, 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        grouped = ",".join(groups + [tail])
    else:
        grouped = digits
    if decimals == 0:
        return f"{sign}{symbol}{grouped}"
    frac = f"{rem:02d}"[:decimals].ljust(decimals, "0")
    return f"{sign}{symbol}{grouped}.{frac}"


def format_ratio(r: Fraction) -> str:
    """Render an exact ratio for humans (``5/8`` -> ``0.625``) without losing exactness."""
    if r.denominator == 1:
        return str(r.numerator)
    dec = r.numerator / r.denominator  # display only
    return f"{dec:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_amount(
    amount_paise: int,
    *,
    raw: str | None = None,
    words_text: str | None = None,
    tolerance_paise: int = 100,
    sum_insured_paise: int | None = None,
    aggregate_paise: int | None = None,
) -> ValidationResult:
    """Cross-checks that catch the failures dual reading alone would miss."""
    issues: list[ValidationIssue] = []
    if amount_paise < 0:
        issues.append(ValidationIssue("NEGATIVE_AMOUNT", "amount is negative", "block"))
    if amount_paise == 0 and raw and any(ch.isdigit() for ch in raw):
        issues.append(ValidationIssue("ZERO_FROM_NONZERO_TEXT", "non-zero text parsed as zero", "block"))
    if words_text:
        words_value = parse_number_words(words_text)
        if words_value is not None and abs(words_value * 100 - amount_paise) > tolerance_paise:
            issues.append(
                ValidationIssue(
                    "WORDS_DIGITS_DISAGREE",
                    f"words say {words_value} rupees, digits say {amount_paise / 100:.2f}",
                    "block",
                )
            )
    if sum_insured_paise is not None and amount_paise > 5 * sum_insured_paise:
        issues.append(ValidationIssue("IMPLAUSIBLE_VS_SI", "amount exceeds 5x sum insured", "block"))
    if aggregate_paise is not None and abs(amount_paise - aggregate_paise) > tolerance_paise:
        issues.append(ValidationIssue("ARITHMETIC_MISMATCH", "value does not match the arithmetic check", "warn"))
    return ValidationResult(tuple(issues))


def qty_times_rate_matches(qty: Fraction, rate_paise: int, amount_paise: int, tolerance_paise: int = 100) -> bool:
    return abs(qty * rate_paise - amount_paise) <= tolerance_paise


def parse_percent(raw: str) -> Fraction | None:
    """Parse '10%', '10 %', '0.1' (as 10%? no — as-is) into a Fraction percentage."""
    if raw is None:
        return None
    s = normalise_text(str(raw))
    if "%" in s:
        token = re.search(r"(\d+(?:\.\d+)?)", s)
        if not token:
            return None
        return Fraction(token.group(1))
    token = re.search(r"(\d+(?:\.\d+)?)", s)
    if not token:
        return None
    return Fraction(token.group(1))


def percent_to_fraction(pct: Fraction) -> Fraction:
    return pct / 100


def as_ratio(numerator: int, denominator: int) -> Fraction | None:
    """cap/actual ratio, exact. Returns None when the denominator is zero."""
    if denominator == 0:
        return None
    return Fraction(numerator, denominator)


def detect_ocr_suspicion(text: str) -> tuple[float, tuple[str, ...]]:
    """Score how suspicious a numeric token looks, for the dual-read trigger."""
    flags: list[str] = []
    score = 0.0
    if re.search(r"(?<=[1-9])(?:[OoIlSsBbZzGgDTQq])(?=\d)", text):
        score += 0.4
        flags.append("AMBIGUOUS_CHAR_IN_NUMBER")
    if re.search(r"\d,\d{1}(?!\d)", text):
        score += 0.2
        flags.append("SHORT_GROUP")
    if re.search(r"\.\d{3,}", text):
        score += 0.3
        flags.append("LONG_DECIMAL")
    return min(score, 1.0), tuple(flags)
