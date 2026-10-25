"""Read the real FE functions and compare their display before/after BE projection."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ai_client.base import AICandidate  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.schemas.meal import PackageInfo  # noqa: E402
from app.services.analyze import _package_for_response, resolve_nutrition  # noqa: E402

HERE = Path(__file__).resolve().parent


def prepare_case(case, nutrition_cases):
    base = nutrition_cases[case["nutrition_case"]]
    raw = {**base["candidate"], "package": {
        **base["candidate"].get("package", {}), "size_text": case["size_text"],
        "brand": "synthetic brand", "product_name": "synthetic food", "label_text": "original label",
    }}
    if "package_size_g" in case:
        raw["package"]["size_g"] = case["package_size_g"]
    candidate = AICandidate(food_name="synthetic food", confidence=0.9, **raw)
    matched = SimpleNamespace(**base["matched"]) if "matched" in base else None
    return candidate, matched, base.get("half_steps", False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fe-root", type=Path, default=ROOT.parent / "FE")
    parser.add_argument("--output", type=Path, default=HERE / "display_results.json")
    args = parser.parse_args()
    nutrition_cases = {case["id"]: case for case in json.loads((HERE / "cases.json").read_text(encoding="utf-8"))}
    cases = json.loads((HERE / "display_cases.json").read_text(encoding="utf-8"))
    source_paths = [args.fe_root / "src/features/meal/utils" / name
                    for name in ("candidateToFood.js", "servingBasis.js")]
    sources = [path.read_text(encoding="utf-8") for path in source_paths]
    rows = []
    settings = get_settings()
    previous = settings.count_half_steps
    try:
        for case in cases:
            candidate, matched, settings.count_half_steps = prepare_case(case, nutrition_cases)
            original = candidate.package.model_dump()
            resolved = resolve_nutrition(candidate, matched)
            before = PackageInfo(**original)
            after = _package_for_response(candidate.package, resolved)
            assert candidate.package.model_dump() == original
            assert before.model_dump(exclude={"size_text"}) == after.model_dump(exclude={"size_text"})
            response = {
                "food_candidate_id": 1, "normalized_name": "synthetic food",
                "estimated_serving": resolved.estimated_serving,
                "quantity": resolved.count, "quantity_unit": resolved.count_unit,
                "grams": resolved.grams, "nutrition_source": resolved.source,
                "nutrition": resolved.nutrition.model_dump(),
            }
            rows.append({
                "id": case["id"], "original_size_text": original["size_text"],
                "expected_fe_text": case["expected_fe_text"],
                "before": {**response, "package": before.model_dump()},
                "after": {**response, "package": after.model_dump()},
            })
    finally:
        settings.count_half_steps = previous
    # The FE files have no imports. Load the exact source as ES modules, without edits/builds.
    node_code = """
import fs from 'node:fs';
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const modules = await Promise.all(data.sources.map(source =>
  import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))));
const [{candidateToFood}, {servingBasisText}] = modules;
console.log(JSON.stringify(data.rows.map(row => ({
  id: row.id,
  before_text: servingBasisText(candidateToFood(row.before)),
  after_text: servingBasisText(candidateToFood(row.after)),
}))));
"""
    rendered = json.loads(subprocess.check_output(
        ["node", "--input-type=module", "-e", node_code],
        input=json.dumps({"sources": sources, "rows": rows}), text=True, encoding="utf-8",
    ))
    results = [{
        "id": row["id"],
        "original_size_text": row["original_size_text"],
        "package_size_g": row["before"]["package"]["size_g"],
        "grams": row["after"]["grams"],
        "display_count": row["after"]["quantity"],
        "per_unit_kcal": row["after"]["nutrition"]["calories"],
        "total_kcal": row["after"]["nutrition"]["calories"] * row["after"]["estimated_serving"],
        "after_size_text": row["after"]["package"]["size_text"],
        "expected_fe_text": row["expected_fe_text"],
        "before_text": display["before_text"], "after_text": display["after_text"],
        "before_passed": display["before_text"] == row["expected_fe_text"],
        "after_passed": display["after_text"] == row["expected_fe_text"],
    } for row, display in zip(rows, rendered, strict=True)]
    report = {
        "data_type": "synthetic display contract; real FE functions read without modification",
        "comparison": "same corrected nutrition calculation; before uses the unmodified package text",
        "fe_source_sha256": {path.name: hashlib.sha256(source.encode("utf-8")).hexdigest()
                             for path, source in zip(source_paths, sources, strict=True)},
        "cases": len(results),
        "before_passed": sum(row["before_passed"] for row in results),
        "after_passed": sum(row["after_passed"] for row in results),
        "original_package_mutated": False,
        "results": results,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("cases", "before_passed", "after_passed", "original_package_mutated")}))
    return 0 if report["after_passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
