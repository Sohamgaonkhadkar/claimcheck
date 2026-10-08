"""Application boundary for the existing synthetic GOLDEN-001 vertical slice.

This service only orchestrates the established fixture, deterministic pipeline and report builder.
It projects their output into a small safe response; it does not calculate money or adjudicate.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any

from claimcheck.cases.golden_001 import build_case
from claimcheck.explain.report import build_report
from claimcheck.pipeline import run_case


SYNTHETIC_NOTICE = (
    "Synthetic demonstration only. No real insurer, policy, hospital, or patient is represented."
)


class GoldenDemoService:
    """Run GOLDEN-001 through the existing core and report builder, then safely project it."""

    def run(self) -> dict[str, Any]:
        case = build_case()
        case_run = run_case(case)
        report = build_report(case_run)

        document_roles = {document_id: role for role, document_id in case.documents.items()}
        public_references: dict[str, dict[str, Any]] = {}

        def public_reference(source_id: str) -> str:
            """Map report evidence IDs to non-path API references."""
            evidence = case.evidence.get(source_id)
            if evidence is not None:
                public_id = f"evidence:{source_id}"
                public_references.setdefault(public_id, {
                    "reference_id": public_id,
                    "kind": "span",
                    "document_role": document_roles.get(evidence.document_id, "unspecified"),
                    "page_number": evidence.page_number,
                    "verification_status": evidence.verification_status,
                    "verification_method": evidence.verify_method,
                })
                return public_id

            for document_id, role in document_roles.items():
                if source_id == document_id or source_id.startswith(f"{document_id}:"):
                    public_id = f"document:{role}"
                    public_references.setdefault(public_id, {
                        "reference_id": public_id,
                        "kind": "document",
                        "document_role": role,
                        "page_number": None,
                        "verification_status": "DOCUMENT_REFERENCE",
                        "verification_method": None,
                    })
                    return public_id

            # Preserve an unknown report reference without revealing its raw value. The
            # Golden fixture currently uses only the two cases above.
            digest = sha256(source_id.encode("utf-8")).hexdigest()[:12]
            public_id = f"source-reference:{digest}"
            public_references.setdefault(public_id, {
                "reference_id": public_id,
                "kind": "source_reference",
                "document_role": "unspecified",
                "page_number": None,
                "verification_status": "REFERENCE_ONLY",
                "verification_method": None,
            })
            return public_id

        def project_finding(finding: dict[str, Any]) -> dict[str, Any]:
            citation_views = [
                {
                    "reference_type": citation["ref_type"],
                    "reference_id": citation["ref_id"],
                    "title": citation["title"] or None,
                    "verified": bool(citation["verified"]),
                }
                for citation in finding["citations"]
            ]
            evidence_ids = [public_reference(source_id) for source_id in finding["evidence"]]
            return {
                "finding_id": finding["finding_id"],
                "type": finding["type"],
                "state": finding["state"],
                "head": finding["head"],
                "why": finding["why"],
                "amount_paise": finding["amount_paise"],
                "amount_display": finding["amount_display"],
                "amount_gross_paise": finding["amount_gross_paise"],
                "amount_gross_display": finding["amount_gross_display"],
                "amount_basis": finding["amount_basis"],
                "evidence_reference_ids": evidence_ids,
                "calculation_step_ids": list(finding["steps"]),
                "citations": citation_views,
                "missing": list(finding["missing"]),
                "limitations": list(finding["limitations"]),
                "gates_failed": list(finding["gates_failed"]),
            }

        findings = [project_finding(finding) for finding in report["findings"]]
        reconciliation = report["reconciliation"]
        calculation_steps = [
            {
                "step_id": step["node_id"],
                "step_type": step["step_type"],
                "label": step["label"],
                "base_label": step["base_label"],
                "base_paise": step["base_paise"],
                "base_display": step["base_display"],
                "output_paise": step["output_paise"],
                "output_display": step["output_display"],
                "effect": step["effect"],
            }
            for step in report["money_flow"]["steps"]
        ]
        readings = [
            {
                "reading_id": reading["reading_id"],
                "payable_paise": reading["payable_paise"],
                "payable_display": reading["payable_display"],
                "difference_paise": reading["difference_paise"],
            }
            for reading in reconciliation["readings"]
        ]

        all_limitations = list(dict.fromkeys(
            limitation
            for finding in report["findings"]
            for limitation in finding["limitations"]
        ))
        provenance = report["provenance"]
        return {
            "case_id": report["case"]["case_id"],
            "run_id": f"RUN-{report['case']['case_id']}",
            "synthetic_demo": report["case"]["origin"] == "synthetic",
            "synthetic_notice": SYNTHETIC_NOTICE,
            "overall_result": {
                "state": report["verdict"]["state"],
                "head_states": dict(report["verdict"]["head_states"]),
                "supported_difference_display": report["verdict"]["supported_difference_display"],
            },
            "financial_findings": [item for item in findings if item["type"] == "FINANCIAL"],
            "procedural_findings": [item for item in findings if item["type"] == "PROCEDURAL"],
            "evidence_gaps": [item for item in findings if item["type"] == "EVIDENCE_GAP"],
            "informational_findings": [item for item in findings if item["type"] == "INFO"],
            "evidence_references": list(public_references.values()),
            "reconciliation": {
                "lawful_payable_paise": reconciliation["lawful_payable_paise"],
                "lawful_payable_display": reconciliation["lawful_payable_display"],
                "paid_paise": reconciliation["paid_paise"],
                "paid_display": reconciliation["paid_display"],
                "difference_paise": reconciliation["difference_paise"],
                "difference_display": reconciliation["difference_display"],
                "supported_difference_paise": reconciliation["supported_difference_paise"],
                "supported_difference_display": reconciliation["supported_difference_display"],
                "unexplained_paise": reconciliation["unexplained_paise"],
                "unexplained_display": reconciliation["unexplained_display"],
                "restore_recompute_paise": reconciliation["restore_recompute_paise"],
                "restore_recompute_display": reconciliation["restore_recompute_display"],
                "identity_ok": reconciliation["identity_ok"],
                "summary": reconciliation["summary"],
                "readings": readings,
            },
            "calculation_steps": calculation_steps,
            "limitations": all_limitations,
            "provenance": {
                "as_of": provenance["as_of"],
                "rulepack_version": provenance["rulepack_version"],
                "arithmetic": provenance["arithmetic"],
            },
        }
