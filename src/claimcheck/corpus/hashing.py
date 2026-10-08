"""Deterministic hashing for the corpus (milestone §9).

Everything the corpus publishes is content-addressed:

    same source bytes + same metadata + same clause extraction
        =>  same document hash, same clause-set hash, same snapshot id

The hash functions here are the only ones the corpus uses, and they are pure: no
clock, no locale, no dictionary order, no floating point. Floats are *refused*
rather than rounded — a hash that depends on repr(float) is not reproducible, and
money in this project never passes through one anyway (Phase-3 rule 6).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


class Unhashable(ValueError):
    """A payload that cannot be hashed reproducibly (float, bytes, set, ...)."""


def _reject(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        raise Unhashable(
            f"{path}: a float cannot be hashed reproducibly; use an int (paise) or a string"
        )
    if isinstance(obj, (bytes, bytearray)):
        raise Unhashable(f"{path}: bytes are hashed explicitly, not as part of a payload")
    if isinstance(obj, (set, frozenset)):
        raise Unhashable(f"{path}: an unordered set has no canonical order; sort it first")
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise Unhashable(f"{path}: key {k!r} is not a string")
            _reject(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject(v, f"{path}[{i}]")


def canonical_json(payload: Any) -> str:
    """A canonical serialisation: sorted keys, no whitespace, UTF-8, no floats."""
    _reject(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_payload(payload: Any) -> str:
    return sha256_text(canonical_json(payload))


def short(hash_hex: str, n: int = 12) -> str:
    return hash_hex[:n].upper()


# ---------------------------------------------------------------------------
# Domain hashes
# ---------------------------------------------------------------------------
def document_hash(document_payload: Mapping[str, Any], clause_payloads: Iterable[Mapping[str, Any]],
                  artifacts: Mapping[str, str]) -> str:
    """The identity of a corpus document *as stored*: metadata + clauses + artifacts.

    ``artifacts`` maps a role (``"source_text"``) to the sha256 of the stored bytes.
    The documents hash changes if any of the three changes — which is precisely why a
    changed source cannot hide inside an old snapshot.
    """
    return hash_payload({
        "document": dict(document_payload),
        "artifacts": dict(sorted(artifacts.items())),
        "clauses": [dict(c) for c in clause_payloads],
    })


def clause_set_hash(clause_payloads: Iterable[Mapping[str, Any]]) -> str:
    """Order-independent hash of a clause set (each clause is hashed by identity)."""
    items = sorted(hash_payload(dict(c)) for c in clause_payloads)
    return hash_payload({"clause_hashes": items, "count": len(items)})
