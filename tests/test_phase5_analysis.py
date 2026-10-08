"""Pure Phase 5 result-safety checks against the unchanged deterministic core."""
from __future__ import annotations

import json

from claimcheck.api.schemas import AnalysisResultView
from claimcheck.application.analysis import _safe_result
from claimcheck.cases.golden_001 import build_case
from claimcheck.pipeline import run_case


def test_analysis_result_projection_is_json_safe_and_contains_no_source_quotes() -> None:
    result = _safe_result(run_case(build_case()))
    validated = AnalysisResultView.model_validate(result)
    assert validated.verdict.state in {
        "CONSISTENT", "POTENTIALLY_INCONSISTENT", "UNDETERMINED",
    }
    assert validated.reconciliation.identity_ok is True
    assert validated.rulepack_version
    rendered = json.dumps(result, sort_keys=True)
    assert "quoted_text" not in rendered
    assert "raw_text" not in rendered
    assert "storage_key" not in rendered
    assert "object_key" not in rendered
    assert "SYNTHETIC GENERAL INSURANCE CO." not in rendered
