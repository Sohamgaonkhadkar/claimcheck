#!/usr/bin/env python3
"""Run the golden case end to end and write its artefacts.

    python scripts/run_golden.py            # write cases/golden/GOLDEN-001-*
    python scripts/run_golden.py --stdout   # print the report instead

No model, no network, no corpus download: this is the deterministic path only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claimcheck.cases.golden_001 import build_case          # noqa: E402
from claimcheck.explain.report import (                     # noqa: E402
    build_report,
    render_letter,
    render_markdown,
)
from claimcheck.pipeline import run_case                    # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run the golden deterministic case")
    ap.add_argument("--out", default=str(ROOT / "cases" / "golden"))
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)

    case = build_case()
    report = run_case(case)
    markdown = render_markdown(report)
    letter = render_letter(report)
    data = build_report(report)

    if args.stdout:
        print(markdown)
        return 0

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{case.case_id}-report.md").write_text(markdown)
    (out / f"{case.case_id}-report.json").write_text(json.dumps(data, indent=2, default=str))
    (out / f"{case.case_id}-letter.md").write_text(letter)
    print(f"wrote {out}/{case.case_id}-report.md, -report.json, -letter.md")
    print(f"lawful payable {data['money_flow']['lawful_payable_display']} vs paid "
          f"{data['insurer_model']['net_payable_display']}; "
          f"supported difference {data['verdict']['supported_difference_display']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
