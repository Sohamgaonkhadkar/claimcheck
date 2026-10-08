"""Typed errors for CLAIMCHECK.

Every failure that can affect a user-visible state is a *typed* error with a code
that matches the refusal catalogue in the Phase-2 architecture (``08-...`` §15.2)
and the API error vocabulary (§22.3). Nothing raises a bare Exception into a
verdict path.
"""

from __future__ import annotations


class ClaimCheckError(Exception):
    """Base class. `code` is part of the public contract."""

    code: str = "CC-ERROR"

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {"error_code": self.code, "message": self.message, "details": self.details}


# --------------------------------------------------------------------------
# Ingestion / parsing
# --------------------------------------------------------------------------
class IngestError(ClaimCheckError):
    code = "EX-DOC-INGEST"


class UnsupportedDocument(IngestError):
    code = "EX-DOC-UNSUPPORTED"


class MaliciousDocument(IngestError):
    code = "EX-DOC-MALICIOUS"


class UnreadableDocument(IngestError):
    code = "EX-DOC-UNREADABLE"


class LowQualityRegion(IngestError):
    code = "EX-LOW-CONFIDENCE"


# --------------------------------------------------------------------------
# Money / numbers
# --------------------------------------------------------------------------
class MoneyParseError(ClaimCheckError):
    code = "EX-AMOUNT-UNPARSED"


class AmountDisagreement(MoneyParseError):
    code = "EX-AMOUNT-DISAGREEMENT"


# --------------------------------------------------------------------------
# Calculation
# --------------------------------------------------------------------------
class CalcError(ClaimCheckError):
    code = "CALC-ERROR"


class CalcMissingBase(CalcError):
    code = "CALC_MISSING_BASE"


class CalcMissingParam(CalcError):
    code = "CALC_MISSING_PARAM"


class CalcUnverifiedInput(CalcError):
    code = "CALC_UNVERIFIED_INPUT"


class CalcAmbiguousOrder(CalcError):
    code = "CALC_AMBIGUOUS_ORDER"


class CalcInvariantViolation(CalcError):
    code = "CALC_INVARIANT_VIOLATION"


class CalcGraphInvalid(CalcError):
    code = "GRAPH_INCOMPLETE"


# --------------------------------------------------------------------------
# Rules / evidence / retrieval
# --------------------------------------------------------------------------
class RuleError(ClaimCheckError):
    code = "RULE-ERROR"


class RuleNotAssertable(RuleError):
    """Rule exists but its in-force status is unverified, so it cannot carry a verdict."""

    code = "RULE_STATUS_UNVERIFIED"


class EvidenceError(ClaimCheckError):
    code = "EVIDENCE-ERROR"


class FabricatedQuote(EvidenceError):
    code = "EX-FABRICATED-QUOTE"


class RetrievalNotLocated(ClaimCheckError):
    """Nothing crossed the retrieval threshold. A first-class outcome, not an error."""

    code = "EX-CLAUSE-NOT_FOUND"


class PolicySilent(ClaimCheckError):
    code = "EX-POLICY-SILENT"


class PolicyConflict(ClaimCheckError):
    code = "EX-POLICY-CONFLICT"


class RegulationConflict(ClaimCheckError):
    code = "EX-REG-CONFLICT"


# --------------------------------------------------------------------------
# Scope refusals (by design, see architecture §15.2)
# --------------------------------------------------------------------------
class OutOfScope(ClaimCheckError):
    code = "EX-INSCOPE"


class MedicalNecessityOutOfScope(OutOfScope):
    code = "EX-MEDNECESSITY"


class FraudOutOfScope(OutOfScope):
    code = "EX-FRAUD"


class RCSUnverifiable(OutOfScope):
    code = "EX-RCS-UNVERIFIABLE"


class PrivateContractNeeded(OutOfScope):
    code = "EX-PRIVATE-CONTRACT"


class MoUUnverifiable(OutOfScope):
    code = "EX-MOU-UNVERIFIABLE"


# --------------------------------------------------------------------------
# Trust boundary
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Corpus (knowledge base): provenance is mandatory, snapshots are immutable
# --------------------------------------------------------------------------
class CorpusError(ClaimCheckError):
    code = "EX-CORPUS"


class CorpusProvenanceMissing(CorpusError):
    """A document or clause was presented for ingestion without its provenance.

    There is no default: a missing source URL, licence, status basis or anchor is a
    refusal, never a guess (architecture §20.4, corpus §12).
    """

    code = "EX-CORPUS-PROVENANCE"


class CorpusAnchorNotFound(CorpusError):
    """A declared clause anchor does not appear in the stored source text.

    The clause index is built from anchors that are *found*, never from anchors that
    are assumed; an anchor that cannot be located means the stored artifact and the
    clause table disagree about what the document says.
    """

    code = "EX-CORPUS-ANCHOR"


class CorpusImmutable(CorpusError):
    """An attempt to overwrite a published snapshot, or to alter an artifact in place.

    A change to any source is a *new* snapshot, never an edit of an old one
    (architecture §27.4; milestone §9).
    """

    code = "EX-CORPUS-IMMUTABLE"


class CorpusSnapshotInvalid(CorpusError):
    """A snapshot manifest does not reproduce: a hash, a clause or a document drifted."""

    code = "EX-CORPUS-SNAPSHOT"


class CorpusLinkError(CorpusError):
    """A rule's declared source (document/clause) could not be resolved or checked."""

    code = "EX-CORPUS-LINK"


class TrustBoundaryViolation(ClaimCheckError):
    """A model-produced value tried to enter the deterministic core."""

    code = "TRUST-BOUNDARY-VIOLATION"


class InjectionDetected(ClaimCheckError):
    """Instruction-like content in untrusted document text. Flagged, never obeyed."""

    code = "EX-INJECTION-FLAGGED"
