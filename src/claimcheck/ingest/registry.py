"""The document registry (Phase 3D §2).

A storage-neutral index of what has been ingested. It knows documents, pages and their
measured quality; it does not know where the bytes live beyond an opaque artifact key, and
it never holds document text.

Two properties the rest of the phase depends on:

* **Idempotence.** Registering the same bytes twice yields the same ``document_id`` and does
  not create a second entry. ``document_id`` is derived from the content hash.
* **Independence is computed, never asserted.** ``independence_report`` groups documents by
  content hash and by the bill number/identifier seen inside them, so a corpus cannot claim
  more independent cases than it has. Phase 3C found 34 distinct bill numbers spread across
  files that had all been counted separately; the registry makes that check routine.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import DocumentAsset, DocumentType, PageAsset, PageQuality


@dataclass
class DocumentEntry:
    asset: DocumentAsset
    pages: dict[int, PageAsset] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages.values() if p.used_ocr)


class DocumentRegistry:
    """In-memory registry with JSON persistence. Text is never persisted here."""

    def __init__(self) -> None:
        self._docs: dict[str, DocumentEntry] = {}

    # ------------------------------------------------------------------ writing
    def add_document(self, asset: DocumentAsset) -> DocumentAsset:
        entry = self._docs.get(asset.document_id)
        if entry is None:
            self._docs[asset.document_id] = DocumentEntry(asset=asset)
            return asset
        # Same content, possibly richer metadata: keep the first identity, merge the rest.
        merged = entry.asset
        if merged.page_count != asset.page_count and asset.page_count:
            entry.asset = _replace(merged, page_count=asset.page_count, fingerprint=_replace(
                merged.fingerprint, page_count=asset.page_count))
        if merged.artifact_key is None and asset.artifact_key:
            entry.asset = _replace(entry.asset, artifact_key=asset.artifact_key)
        return entry.asset

    def add_page(self, page: PageAsset) -> None:
        entry = self._docs.get(page.document_id)
        if entry is None:
            raise KeyError(f"unknown document {page.document_id}; register the document first")
        entry.pages[page.page_number] = page

    # ------------------------------------------------------------------ reading
    def get(self, document_id: str) -> DocumentAsset | None:
        entry = self._docs.get(document_id)
        return entry.asset if entry else None

    def entry(self, document_id: str) -> DocumentEntry | None:
        return self._docs.get(document_id)

    def pages(self, document_id: str) -> list[PageAsset]:
        entry = self._docs.get(document_id)
        return [entry.pages[k] for k in sorted(entry.pages)] if entry else []

    def documents(self) -> list[DocumentAsset]:
        return [self._docs[k].asset for k in sorted(self._docs)]

    def by_type(self, document_type: DocumentType) -> list[DocumentAsset]:
        return [d for d in self.documents() if d.document_type == document_type]

    def find_by_sha256(self, sha256: str) -> DocumentAsset | None:
        for d in self.documents():
            if d.fingerprint.sha256 == sha256:
                return d
        return None

    def __len__(self) -> int:
        return len(self._docs)

    def __contains__(self, document_id: object) -> bool:
        return document_id in self._docs

    # ------------------------------------------------------------------ totals
    def totals(self) -> dict[str, Any]:
        docs = self.documents()
        pages = [p for d in docs for p in self.pages(d.document_id)]
        return {
            "documents": len(docs),
            "pages": len(pages),
            "pages_without_text_layer": sum(1 for p in pages if not p.text_layer_present),
            "pages_ocr": sum(1 for p in pages if p.used_ocr),
            "pages_with_images": sum(1 for p in pages if p.image_count > 0),
            "chars": sum(p.char_count for p in pages),
            "by_type": _counts(d.document_type.value for d in docs),
            "by_pii_status": _counts(d.pii_status.value for d in docs),
        }

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> str:
        return json.dumps({
            "documents": [
                {**e.asset.as_dict(),
                 "pages": [e.pages[k].as_dict() for k in sorted(e.pages)]}
                for e in (self._docs[k] for k in sorted(self._docs))
            ]
        }, indent=2, sort_keys=True)

    def save(self, path) -> None:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_json(cls, text: str) -> "DocumentRegistry":
        raise NotImplementedError(
            "the registry is rebuilt by ingesting; loading page records without their "
            "documents would create assets with no verifiable content hash")


def _replace(obj, **changes):
    import dataclasses

    return dataclasses.replace(obj, **changes)


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- independence

#: Identifier-shaped tokens that are evidence *of the same document* when they recur.
# A bill number has a letter prefix and a digit run. The tail must contain at least one
# digit: without that requirement the prefix "INT" matches the word INTERNET, and every
# document that mentions the internet is then recorded as sharing an identifier with every
# other one — which silently invents a corpus-wide family out of nothing.
_BILL_NO = re.compile(r"\b(?:INT|AMHL|BIDHAYAK|BID)\d[A-Z0-9]{3,}\b"       # APC/TPA prefixes
                      r"|\b[A-Z]{2,6}\d{4,}\b"                            # any letters+digits
                      r"|\bB-?\d{4,}\b|\bCLM-?\d{4,}\b")               # claim numbers
_APOLLO_ISH = re.compile(r"\bAMHL[A-Z]*\d+\b")

#: An identifier must appear in at least this many documents before a high document
#: frequency is read as boilerplate rather than as genuine evidence of sameness.
_BOILERPLATE_MIN_DOCS = 4


@dataclass(frozen=True)
class IndependenceFinding:
    document_id: str
    sha256: str
    identifiers: tuple[str, ...]
    is_exact_duplicate_of: str | None = None
    shares_identifier_with: tuple[str, ...] = ()
    counted_as_independent: bool = True
    reason: str = ""


def independence_report(registry: DocumentRegistry,
                        texts: dict[str, str]) -> dict[str, Any]:
    """Decide, from evidence, which documents are *independent observations*.

    ``texts`` maps ``document_id`` to its concatenated text. A document is not counted as
    an independent case when its bytes are identical to another's, or when it shares a
    document identifier (a bill number) with another — either condition means the corpus is
    smaller than its file count suggests.
    """
    findings: list[IndependenceFinding] = []
    by_sha: dict[str, str] = {}
    by_identifier: dict[str, list[str]] = {}

    for doc in registry.documents():
        text = texts.get(doc.document_id, "")
        ids = tuple(sorted(set(_BILL_NO.findall(text)) |
                           set(_APOLLO_ISH.findall(text))))
        duplicate_of = by_sha.get(doc.fingerprint.sha256)
        if duplicate_of is None:
            by_sha[doc.fingerprint.sha256] = doc.document_id
        findings.append(IndependenceFinding(
            document_id=doc.document_id, sha256=doc.fingerprint.sha256,
            identifiers=ids, is_exact_duplicate_of=duplicate_of))
        for ident in ids:
            by_identifier.setdefault(ident, []).append(doc.document_id)

    out: list[IndependenceFinding] = []
    for f in findings:
        sharers = sorted({d for ident in f.identifiers
                          for d in by_identifier.get(ident, []) if d != f.document_id})
        counted = not f.is_exact_duplicate_of
        reason = ""
        if f.is_exact_duplicate_of:
            reason = f"byte-identical to {f.is_exact_duplicate_of}"
        elif sharers:
            counted = False
            reason = ("shares a document identifier with "
                      + ", ".join(sharers[:3]) + ("…" if len(sharers) > 3 else ""))
        elif not f.identifiers:
            reason = "no document identifier found: independence cannot be evidenced"
        out.append(_replace_finding(f, shares_identifier_with=tuple(sharers),
                                    counted_as_independent=counted, reason=reason))

    # An identifier that appears in most of the corpus does not separate anything: it is
    # boilerplate (a form number, a scheme name). Counting it would merge unrelated documents
    # into one "family" and understate the corpus, so it is dropped — and reported as dropped,
    # because a silent filter is indistinguishable from a bug.
    document_frequency: dict[str, int] = {}
    for f in out:
        for ident in f.identifiers:
            document_frequency[ident] = document_frequency.get(ident, 0) + 1
    # Both conditions must hold: a token needs a real document count *and* a large share, so
    # a small corpus is never collapsed by an accidental repeat of two.
    boilerplate = {i for i, df in document_frequency.items()
                   if df >= _BOILERPLATE_MIN_DOCS and df >= 0.5 * len(out)}
    if boilerplate:
        out = [_replace_finding(f, identifiers=tuple(i for i in f.identifiers
                                                     if i not in boilerplate))
               for f in out]
        by_identifier = {}
        for f in out:
            for ident in f.identifiers:
                by_identifier.setdefault(ident, []).append(f.document_id)
        rebuilt: list[IndependenceFinding] = []
        for f in out:
            sharers = tuple(sorted({d for ident in f.identifiers
                                    for d in by_identifier.get(ident, [])
                                    if d != f.document_id}))
            if f.is_exact_duplicate_of:
                reason = f"byte-identical to {f.is_exact_duplicate_of}"
            elif sharers:
                reason = ("shares a document identifier with "
                          + ", ".join(sharers[:3]) + ("…" if len(sharers) > 3 else ""))
            elif not f.identifiers:
                reason = "no document identifier found: independence cannot be evidenced"
            else:
                reason = "identifier is unique in this corpus"
            rebuilt.append(_replace_finding(
                f, shares_identifier_with=sharers,
                counted_as_independent=(not sharers and not f.is_exact_duplicate_of),
                reason=reason))
        out = rebuilt

    families = _family_clusters(out, by_identifier)
    return {
        "boilerplate_identifiers_dropped": sorted(_ident_hash(i) for i in boilerplate),
        "documents": len(out),
        "counted_independent": sum(1 for f in out if f.counted_as_independent),
        "exact_duplicates": sum(1 for f in out if f.is_exact_duplicate_of),
        "sharing_an_identifier": sum(1 for f in out if f.shares_identifier_with),
        "without_any_identifier": sum(1 for f in out if not f.identifiers),
        "identifier_families": len(families),
        "families": families[:20],
        # Document identifiers (bill numbers) are evidence of sameness, but they are still
        # document content. Published artefacts carry a hash of each identifier, never the
        # identifier itself; the grouping they justify is unchanged.
        "findings": [
            {"document_id": f.document_id, "sha16": f.sha256[:16],
             "identifier_hashes": [_ident_hash(i) for i in f.identifiers],
             "duplicate_of": f.is_exact_duplicate_of,
             "shares_with_count": len(f.shares_identifier_with),
             "independent": f.counted_as_independent, "reason": f.reason}
            for f in out],
    }


def _replace_finding(f: IndependenceFinding, **changes) -> IndependenceFinding:
    import dataclasses

    return dataclasses.replace(f, **changes)


def _family_clusters(findings: list[IndependenceFinding],
                     by_identifier: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Union-find over shared identifiers: the honest size of the corpus."""
    index = {f.document_id: i for i, f in enumerate(findings)}
    parent = list(range(len(findings)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for docs in by_identifier.values():
        roots = [find(index[d]) for d in docs if d in index]
        for r in roots[1:]:
            if find(r) != find(roots[0]):
                parent[find(r)] = find(roots[0])
    groups: dict[int, list[str]] = {}
    for f in findings:
        groups.setdefault(find(index[f.document_id]), []).append(f.document_id)
    return sorted(
        ({"size": len(v), "documents": sorted(v),
          "identifier_hashes": sorted({_ident_hash(i) for d in v for i in
                                       next(f for f in findings
                                            if f.document_id == d).identifiers})}
         for v in groups.values()),
        key=lambda g: (-g["size"], g["documents"][0]))


def _ident_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
