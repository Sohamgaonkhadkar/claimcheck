"""Owner-scoped Phase 5 analysis bridge over a pinned trusted input snapshot."""
from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from claimcheck.application.errors import ApplicationError
from claimcheck.application.identity import OwnerIdentity
from claimcheck.application.review_readiness import evaluate_case_readiness
from claimcheck.application.trusted_case import TrustedCaseAdapter
from claimcheck.ingest.model import Storage
from claimcheck.persistence.models import AnalysisRun, Case, utc_now
from claimcheck.persistence.repositories import CaseRepository
from claimcheck.pipeline import CaseRun, run_case


class AnalysisApplicationService:
    """Build trusted inputs, run the deterministic core, and persist safe run results."""

    def __init__(self, session: Session, storage: Storage,
                 principal: OwnerIdentity) -> None:
        self.session = session
        self.storage = storage
        self.principal = principal
        self.owner_id = principal.owner_id

    def analyze(self, case_id: UUID) -> dict:
        # The adapter checks explicit roles, source hashes, verified evidence and effective
        # review events. It commits its read transaction before the deterministic core runs.
        trusted_case = TrustedCaseAdapter(
            self.session, self.storage, self.principal
        ).build(case_id)
        input_revision = trusted_case.input_revision
        if input_revision is None:
            raise ApplicationError(
                409, "TRUSTED_INPUT_REVISION_MISSING", "Trusted input unavailable",
                "The trusted case does not identify a persisted input revision.",
            )

        analysis_run_id = uuid4()
        started_at = utc_now()
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            if case is None:
                raise _not_found()
            # Avoid starting a run against a snapshot that became stale between adapter
            # construction and reservation. Later edits remain independent historical revisions.
            if case.input_revision != input_revision:
                raise ApplicationError(
                    409, "ANALYSIS_INPUT_CHANGED", "Case inputs changed",
                    "The case changed while its trusted analysis input was being prepared; retry analysis.",
                )
            active_run = self.session.scalar(select(AnalysisRun).where(
                AnalysisRun.owner_id == self.owner_id,
                AnalysisRun.case_id == case_id,
                AnalysisRun.status == "RUNNING",
            ).with_for_update())
            if active_run is not None:
                raise ApplicationError(
                    409, "ANALYSIS_ALREADY_RUNNING", "Analysis in progress",
                    "An analysis run is already in progress for this case.", retryable=True,
                )
            previous_number = self.session.scalar(select(func.max(AnalysisRun.run_number)).where(
                AnalysisRun.owner_id == self.owner_id,
                AnalysisRun.case_id == case_id,
            )) or 0
            analysis_run = AnalysisRun(
                analysis_run_id=analysis_run_id,
                owner_id=self.owner_id,
                case_id=case_id,
                run_number=previous_number + 1,
                status="RUNNING",
                input_revision=input_revision,
                corpus_snapshot_id=trusted_case.corpus_snapshot_id,
                created_at=started_at,
                started_at=started_at,
            )
            self.session.add(analysis_run)
            case.status = "PROCESSING"
            case.version += 1
            case.updated_at = started_at

        try:
            case_run = run_case(trusted_case)
            result_payload = _safe_result(case_run)
            failure_code = None
            final_status = "SUCCEEDED"
        except Exception as e:
            # Do not expose a core exception, stack trace, source text, or driver detail.
            result_payload = None
            failure_code = "ANALYSIS_FAILED"
            final_status = "FAILED"
            case_run = None

        completed_at = utc_now()
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id, for_update=True)
            if case is None:
                raise _not_found()
            analysis_run = self.session.scalar(select(AnalysisRun).where(
                AnalysisRun.owner_id == self.owner_id,
                AnalysisRun.case_id == case_id,
                AnalysisRun.analysis_run_id == analysis_run_id,
            ).with_for_update())
            if analysis_run is None:
                raise _not_found()
            analysis_run.status = final_status
            analysis_run.finished_at = completed_at
            analysis_run.error_code = failure_code
            analysis_run.result_payload = result_payload
            if case_run is not None:
                analysis_run.rulepack_version = case_run.rules.pack_version
                analysis_run.corpus_snapshot_id = case_run.graph.corpus_snapshot_id

            if case.input_revision == input_revision:
                case.latest_analysis_run_id = analysis_run_id
                case.status = "ANALYZED" if final_status == "SUCCEEDED" else "FAILED"
            else:
                # The completed run remains retrievable under its pinned revision, but it does
                # not mark newer inputs as analyzed or replace their current readiness state.
                case.status = evaluate_case_readiness(
                    self.session, self.owner_id, case
                ).status.value
            case.version += 1
            case.updated_at = completed_at
            self.session.flush()
            return _analysis_run_view(analysis_run)

    def list_runs(self, case_id: UUID) -> dict:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id)
            if case is None:
                raise _not_found()
            rows = list(self.session.scalars(select(AnalysisRun).where(
                AnalysisRun.owner_id == self.owner_id,
                AnalysisRun.case_id == case_id,
            ).order_by(AnalysisRun.run_number.desc())))
            return {
                "case_id": case_id,
                "current_input_revision": case.input_revision,
                "items": [_analysis_run_view(row) for row in rows],
            }

    def get_run(self, case_id: UUID, analysis_run_id: UUID) -> dict:
        with self.session.begin():
            case = CaseRepository(self.session, self.owner_id).get(case_id)
            if case is None:
                raise _not_found()
            row = self.session.scalar(select(AnalysisRun).where(
                AnalysisRun.owner_id == self.owner_id,
                AnalysisRun.case_id == case_id,
                AnalysisRun.analysis_run_id == analysis_run_id,
            ))
            if row is None:
                raise _not_found()
            return _analysis_run_view(row)


def _safe_result(run: CaseRun) -> dict:
    """Persist a deliberately small result projection with no source text or storage data."""
    reconciliation = run.reconciliation
    return {
        "verdict": {
            "state": run.adjudication.worst_state(),
            "head_states": dict(run.adjudication.head_states),
        },
        "reconciliation": {
            "lawful_payable_paise": reconciliation.lawful_payable_paise,
            "paid_paise": reconciliation.paid_paise,
            "difference_paise": reconciliation.difference_paise,
            "supported_difference_paise": reconciliation.supported_paise,
            "unexplained_paise": reconciliation.unexplained_paise,
            "identity_ok": reconciliation.identity_ok,
        },
        "calculation": {
            "gross_bill_paise": run.execution.gross_paise,
            "lawful_payable_paise": run.execution.payable_paise,
            "patient_share_paise": run.execution.patient_share_paise,
            "reductions_paise": run.execution.reductions_paise,
            "steps": [
                {
                    "step_id": step.node_id,
                    "step_type": step.step_type,
                    "output_paise": step.output_paise,
                }
                for step in run.execution.run.trace
            ],
        },
        "findings": [
            {
                "finding_id": finding.finding_id,
                "type": finding.type,
                "state": finding.state,
                "head": finding.head,
                "amount_paise": finding.amount_paise,
                "calculation_step_ids": list(finding.calculation_steps),
            }
            for finding in run.adjudication.findings
        ],
        "withheld_findings_count": len(run.adjudication.withheld),
        "rulepack_version": run.rules.pack_version,
        "corpus_snapshot_id": run.graph.corpus_snapshot_id,
    }


def _analysis_run_view(row: AnalysisRun) -> dict:
    return {
        "analysis_run_id": row.analysis_run_id,
        "case_id": row.case_id,
        "run_number": row.run_number,
        "input_revision": row.input_revision,
        "status": row.status,
        "rulepack_version": row.rulepack_version,
        "corpus_snapshot_id": row.corpus_snapshot_id,
        "error_code": row.error_code,
        "created_at": row.created_at,
        "completed_at": row.finished_at,
        "result": row.result_payload,
    }


def _not_found() -> ApplicationError:
    return ApplicationError(404, "NOT_FOUND", "Not found", "The requested resource was not found.")
