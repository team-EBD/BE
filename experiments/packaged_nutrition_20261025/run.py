"""Offline contract experiment; inputs are synthetic, not real meal observations."""
from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

# Set before importing application modules. No production database or AI is used.
os.environ["DATABASE_URL"] = "sqlite://"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ai_client.base import AICandidate  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services import analyze  # noqa: E402

BASELINE = "dc11453"
CASES = Path(__file__).with_name("cases.json")


def historical_resolver(ref: str):
    """Load only the old pure calculation function, without checking out files."""
    source = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "show",
         f"{ref}:app/services/analyze.py"],
        cwd=ROOT, encoding="utf-8",
    )
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "resolve_nutrition")
    namespace = dict(vars(analyze))
    exec(compile(ast.Module(body=[function], type_ignores=[]), f"{ref}:resolve_nutrition", "exec"), namespace)
    return namespace["resolve_nutrition"], hashlib.sha256(
        ast.get_source_segment(source, function).strip().encode("utf-8")
    ).hexdigest()


def evaluate(resolver, case: dict) -> dict:
    settings = get_settings()
    previous = settings.count_half_steps
    settings.count_half_steps = case.get("half_steps", False)
    try:
        candidate = AICandidate(food_name="합성 검증 음식", confidence=0.9, **case["candidate"])
        matched = SimpleNamespace(**case["matched"]) if "matched" in case else None
        result = resolver(candidate, matched)
        total = round(result.nutrition.calories * result.estimated_serving, 2) if result.nutrition else None
        return {
            "total_kcal": total, "grams": result.grams, "source": result.source,
            "display_count": result.count,
            "passed": (total == case["expected_total_kcal"]
                       and result.grams == case["expected_grams"]
                       and result.source == case["expected_source"]),
        }
    finally:
        settings.count_half_steps = previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("results.json"))
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    before, before_sha = historical_resolver(args.baseline_ref)
    results = [{
        "id": case["id"], "hypothesis": case["hypothesis"],
        "expected_total_kcal": case["expected_total_kcal"],
        "before": evaluate(before, case), "after": evaluate(analyze.resolve_nutrition, case),
    } for case in cases]
    baseline_commit = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", args.baseline_ref],
        cwd=ROOT, encoding="utf-8",
    ).strip()
    report = {
        "experiment": "packaged_nutrition_20261025",
        "data_type": "synthetic contract cases; not image recognition accuracy",
        "baseline_commit": baseline_commit,
        "before_function_sha256": before_sha,
        "after_function_sha256": hashlib.sha256(inspect.getsource(analyze.resolve_nutrition).strip().encode("utf-8")).hexdigest(),
        "case_file_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "cases": len(results),
        "before_passed": sum(r["before"]["passed"] for r in results),
        "after_passed": sum(r["after"]["passed"] for r in results),
        "regressions": [r["id"] for r in results if r["before"]["passed"] and not r["after"]["passed"]],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("cases", "before_passed", "after_passed", "regressions")}, ensure_ascii=False))
    return 0 if report["after_passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
