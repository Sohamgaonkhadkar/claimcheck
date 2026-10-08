"""Reproducibility guards: what the code imports must be what pyproject declares, and no more.

A repository that imports a library it does not declare produces the worst kind of bug: it works
on the machine where someone once ran `pip install` by hand, and fails — or worse, silently
degrades — anywhere else. `pypdf` is the live example: without it the dual-reader cross-check in
`claimcheck.ingest.pdf` switches itself off and the same page reads differently, with no error.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED = ("src", "bench", "eval", "scripts", "tests")

#: import name -> distribution name (the two differ more often than people expect)
IMPORT_TO_DISTRIBUTION = {"PIL": "pillow", "sklearn": "scikit-learn"}

#: local modules/packages that are imported by a bare name from inside a scanned directory
LOCAL_MODULES = {"acquire_claimback_sources", "test_golden_case", "goldstore",
                 "bench", "eval", "scripts", "claimcheck",
                 "model_a_bill_head_v2", "model_a_bill_head_v2_baseline"}

#: declared because the code needs them at run time, without importing them by name
RUNTIME_ONLY = {"pillow", "uvicorn", "httpx", "alembic", "psycopg", "python-multipart"}  # indirect runtime/CLI dependencies

#: ML distributions that must not be added speculatively. The approved policy permits only
#: targeted A/B training; these remain blocked until an actual trainer exists and a minimal,
#: task-specific dependency allowlist is updated alongside its implementation.
FORBIDDEN = {
    "torch", "tensorflow", "transformers", "sentence_transformers", "sentence-transformers",
    "sklearn", "scikit-learn", "xgboost", "lightgbm", "catboost", "spacy", "datasets", "faiss",
    "faiss-cpu", "onnxruntime", "vllm", "accelerate", "keras", "jax", "flair", "timm",
}

# Marker for the actual, user-authorized Model A v2 challenger trainer. The allowlist is the
# minimal ML distribution imported by that implementation; Model B/C remain outside this gate.
TRAINING_IMPLEMENTATION = ROOT / "bench" / "ml" / "model_a" / "train_model_a.py"
APPROVED_ML_DEPENDENCIES: frozenset[str] = frozenset({"scikit-learn"})

BASE_DECLARED_DEPENDENCIES = {
    "fastapi", "pydantic", "uvicorn", "httpx", "sqlalchemy", "alembic", "psycopg",
    "python-multipart", "pyarrow", "zstandard", "pypdfium2", "pillow", "pypdf", "pytesseract", "pytest",
    "starlette",
}


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _declared() -> set[str]:
    names = set()
    project = _project()["project"]
    for entry in project.get("dependencies", []):
        names.add(_normalise(entry))
    for entries in (project.get("optional-dependencies") or {}).values():
        for entry in entries:
            names.add(_normalise(entry))
    return names


def _normalise(requirement: str) -> str:
    for separator in (">", "<", "=", "!", "[", ";", " "):
        requirement = requirement.split(separator)[0]
    return requirement.strip().lower().replace("_", "-")


def _imports() -> dict[str, set[str]]:
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for directory in SCANNED:
        for path in (ROOT / directory).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:                                     # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = ([node.module.split(".")[0]] if node.module and node.level == 0
                             else [])
                else:
                    continue
                for name in names:
                    if not name or name in stdlib or name in LOCAL_MODULES:
                        continue
                    if (ROOT / f"{name}.py").exists() or (ROOT / "src" / name).exists():
                        continue
                    found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_imported_library_is_declared() -> None:
    declared = _declared()
    missing = {}
    for module, files in _imports().items():
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-")
        if distribution not in declared:
            missing[distribution] = sorted(files)[:3]
    assert not missing, (
        f"imported but not declared in pyproject.toml: {missing}. A clean environment cannot "
        f"reproduce this repository with an undeclared import.")


def test_no_declared_dependency_is_unused() -> None:
    used = {IMPORT_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-")
            for module in _imports()}
    declared = _declared()
    # pytest is the test runner and is used through the `pytest` command, not an import in src
    unused = sorted(declared - used - {"pytest"} - RUNTIME_ONLY)
    assert not unused, f"declared but never imported: {unused}"


def test_ml_dependencies_require_an_actual_approved_training_implementation() -> None:
    """The policy permits targeted A/B training, but not speculative ML packages."""
    declared = _declared()
    ml_declared = declared & FORBIDDEN
    if not TRAINING_IMPLEMENTATION.is_file():
        assert not ml_declared, (
            f"ML dependencies {sorted(ml_declared)} are allowed only with an actual approved "
            "Model A/B training implementation; no entry point exists yet. Do not add speculative "
            "dependencies (docs/MODEL-TRAINING-POLICY.md).")
    else:
        assert ml_declared <= APPROVED_ML_DEPENDENCIES, (
            f"unapproved ML dependencies: {sorted(ml_declared - APPROVED_ML_DEPENDENCIES)}; "
            "update the minimal allowlist only alongside the implementation that imports them.")
        assert APPROVED_ML_DEPENDENCIES <= declared


def test_declared_dependencies_are_minimal_for_the_implemented_scope() -> None:
    """Keep the declared set exact to implemented dataset, API, PDF and persistence paths."""
    expected = set(BASE_DECLARED_DEPENDENCIES)
    if TRAINING_IMPLEMENTATION.is_file():
        expected.update(APPROVED_ML_DEPENDENCIES)
    assert _declared() == expected


def test_the_ocr_binary_is_declared_as_a_system_package() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "tesseract-ocr" in text, "the OCR path needs the tesseract binary, not only the wrapper"


def test_gate_thresholds_are_the_agreed_ones() -> None:
    """Keep human-gold pathway thresholds pinned; they do not block synthetic A/B training."""
    from eval import run_gold

    assert run_gold.GATES == {"ocr_money_token_exact_match": 0.90,
                              "retrieval_recall_at_5": 0.80,
                              "bill_head_macro_f1": 0.70}


def test_a_gate_without_a_measurement_is_undecided() -> None:
    from eval import run_gold

    for value in (None,):
        gate = run_gold._gate(0.90, value, on_pass="keep", on_fail="benchmark", metric="m")
        assert gate["verdict"] == "UNDECIDED"
        assert gate["value"] is None
    assert run_gold._gate(0.90, 0.95, on_pass="keep", on_fail="benchmark",
                          metric="m")["verdict"] == "SUPPORTED"
    assert run_gold._gate(0.90, 0.89, on_pass="keep", on_fail="benchmark",
                          metric="m")["verdict"] == "NOT_SUPPORTED"
