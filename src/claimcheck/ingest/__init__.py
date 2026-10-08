"""CLAIMCHECK ingestion: real documents in, addressable evidence out.

Pipeline (Phase 3D):

.. code-block:: text

    PDF bytes
      -> registry         DocumentAsset / PageAsset / PageQuality  (model.py, registry.py)
      -> text layer       character boxes, no model                (pdf.py)
      -> rendering        deterministic scale                      (pdf.py)
      -> layout / blocks  visual lines with coordinates            (pdf.py)
      -> OCR fallback     only where the text layer is insufficient(pdf.py)
      -> two readers      layout + native, cross-checked           (dual.py)
      -> extraction       policy clauses, bill lines, totals        (policy.py, bill.py)
      -> evidence spans   span-addressed, geometry-carrying        (records.py, addressing.py)
      -> existing core    evidence.EvidenceBuilder, rules, verdict

Nothing in this package decides an outcome. It produces text, numbers, spans and explicit
refusals; the deterministic core remains the only source of financial conclusions.
"""

from .addressing import (
    ClauseAddress,
    CollisionReport,
    address_clause,
    clause_id_for,
    collision_report,
    declared_clause_address,
    find_headings,
)
from .bill import AlignedLine, BillParseResult, align_lines, as_read_paise, bill_stats, parse_bill
from .dual import Agreement, DualMoneyFact, FactDisposition, Quarantine, reconcile_money
from .model import (
    BBox,
    DocumentAsset,
    DocumentFingerprint,
    DocumentSource,
    DocumentType,
    EditOp,
    LicenceStatus,
    LocalFileStorage,
    PageAsset,
    PageExtraction,
    PageQuality,
    PiiStatus,
    SourceType,
    Storage,
    StorageMetadata,
    TextLine,
)
from .money import LineMoney, TokenClass, TokenRead, classify_token, find_tokens, read_line_money
from .pdf import OcrPolicy, PageRead, PdfDocumentReader, classify_document, read_page
from .policy import ParsedPolicy, PolicyMetadata, clause_stats, parse_policy
from .settlement import ParsedSettlement, parse_settlement
from .records import (
    BillLine,
    BillSubtotal,
    BillTotal,
    MoneyFormat,
    MoneyReading,
    ParsedBill,
    PolicyClauseRecord,
    PolicyFact,
    SettlementLine,
    SourceEvidence,
    span_fingerprint,
)
from .registry import DocumentRegistry, independence_report
from .sanitize import (
    PiiLeak,
    Redacted,
    SafeRecord,
    assert_safe,
    classify_pii,
    find_identifiers,
    looks_like_pii,
    redact_reason,
    redact_text,
    safe_log,
)

__all__ = [
    # model
    "BBox", "DocumentAsset", "DocumentFingerprint", "DocumentSource", "DocumentType",
    "EditOp", "LicenceStatus", "LocalFileStorage", "PageAsset", "PageExtraction",
    "PageQuality", "PiiStatus", "SourceType", "Storage", "StorageMetadata", "TextLine",
    # registry
    "DocumentRegistry", "independence_report",
    # pdf
    "OcrPolicy", "PageRead", "PdfDocumentReader", "classify_document", "read_page",
    # readers / contract
    "Agreement", "DualMoneyFact", "FactDisposition", "Quarantine", "reconcile_money",
    "AlignedLine", "align_lines",
    # money
    "LineMoney", "TokenClass", "TokenRead", "classify_token", "find_tokens", "read_line_money",
    # records
    "BillLine", "BillSubtotal", "BillTotal", "MoneyFormat", "MoneyReading", "ParsedBill",
    "PolicyClauseRecord", "PolicyFact", "SettlementLine", "SourceEvidence", "span_fingerprint",
    "ParsedSettlement", "parse_settlement",
    # parsers
    "BillParseResult", "ParsedPolicy", "PolicyMetadata", "as_read_paise", "bill_stats",
    "clause_stats", "parse_bill", "parse_policy",
    # addressing
    "ClauseAddress", "CollisionReport", "address_clause", "clause_id_for", "collision_report",
    "declared_clause_address", "find_headings",
    # privacy
    "PiiLeak", "Redacted", "SafeRecord", "assert_safe", "classify_pii", "find_identifiers",
    "looks_like_pii", "redact_reason", "redact_text", "safe_log",
]
