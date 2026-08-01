"""대표 음식 큐레이션 — 공공 음식편에서 음식당 1건을 골라 1인분 기준으로 승격.

사용법 (BE 루트에서):
    python -m scripts.curate_representative_foods <음식편 CSV 경로>

배경 (2026-08-01 결정):
    공공DB 적재 후 같은 음식이 브랜드·조사건별로 수십 행씩 생겨 검색이 혼탁해졌다.
    "일반적인 김치찌개 1인분"을 검색 최상위에 두기 위해, 음식편의 비(非)프랜차이즈
    데이터(급식·가정식·외식 조사 — 재료량 기반이라 신뢰도 높음)에서 음식 이름당
    대표 1건을 골라 `is_representative=true` + 1인분 기준으로 환산한다.

규칙:
    - 시드(source='seed') 40건은 전부 대표로 지정한다 (이미 1인분 기준).
    - 시드와 같은 이름의 공공 그룹은 건너뛴다 (시드 우선).
    - 그룹 대표는 "그룹 중앙값 칼로리에 가장 가까운 행" — 실제 존재하는 행만 쓴다
      (합성 평균값을 만들지 않아 출처 추적이 가능).
    - 1인분 환산: 아래 분류별 기준표(PM 확정 2026-08-01)로 per-100g 값을 곱해 저장.
      기준표에 없는 분류(빵·유제품 등)는 이번 판에서 대표를 만들지 않는다.
    - 멱등: 이미 is_representative=true 인 행은 다시 환산하지 않는다(이중 스케일 방지).
      기준표를 바꿔 재환산하려면 원본이 CSV에 있으므로 플래그를 내리고 재실행한다.
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem
from scripts.import_public_nutrition import normalize_name, read_rows

# 분류별 1인분 기준(g/ml) — PM 확정안 (ref/기록/0801.md)
SERVING_BY_MAJOR = {
    "밥류": 300,
    "죽 및 스프류": 300,
    "국 및 탕류": 400,
    "찌개 및 전골류": 400,
    "면 및 만두류": 500,
    "구이류": 150,
    "볶음류": 150,
    "튀김류": 150,
    "찜류": 150,
    "조림류": 50,
    "전·적 및 부침류": 50,
    "나물·숙채류": 50,
    "생채·무침류": 50,
    "김치류": 40,
    "장아찌·절임류": 40,
    "젓갈류": 40,
    "음료 및 차류": 350,
}

# 환산 대상 영양 컬럼 (per-100g → per-1인분)
_SCALED_FIELDS = (
    "calories", "carbs", "protein", "fat",
    "sugar", "fiber", "sodium", "cholesterol", "saturated_fat", "trans_fat",
)


def _display_name(raw_name: str) -> str:
    # import_public_nutrition.transform 의 음식(D) 이름 규칙과 동일하게 유지할 것
    name = raw_name.strip()
    if "_" in name:
        prefix, suffix = (part.strip() for part in name.split("_", 1))
        if suffix:
            if normalize_name(prefix) in normalize_name(suffix):
                return suffix
            return f"{prefix} {suffix}"
    return name


def _pick_representative(group: list[dict]) -> dict:
    """그룹 중앙값 칼로리에 가장 가까운 행 (동률이면 식품코드 순으로 결정적)."""
    med = statistics.median(float(r["에너지(kcal)"]) for r in group)
    return min(
        group,
        key=lambda r: (abs(float(r["에너지(kcal)"]) - med), r["식품코드"]),
    )


def run(csv_path: Path, session_factory=SessionLocal) -> dict:
    rows = read_rows(csv_path)
    general = [
        r for r in rows
        if r["데이터구분코드"] == "D"
        and "프랜차이즈" not in (r.get("식품기원명") or "")
        and (r.get("에너지(kcal)") or "").strip()
    ]

    groups: dict[str, list[dict]] = {}
    for r in general:
        groups.setdefault(normalize_name(_display_name(r["식품명"])), []).append(r)

    stats = {"seed_marked": 0, "curated": 0, "skipped_seed": 0,
             "skipped_no_serving": 0, "already": 0, "not_in_db": 0}

    with session_factory() as session:
        # 1) 시드는 전부 대표 (이미 1인분 기준)
        seed_names = set()
        for item in session.scalars(select(NutritionItem).where(NutritionItem.source == "seed")):
            seed_names.add(item.normalized_name)
            if not item.is_representative:
                item.is_representative = True
                stats["seed_marked"] += 1

        # 2) 공공 음식편 그룹별 대표 선정·환산
        for norm_name, group in groups.items():
            if norm_name in seed_names:
                stats["skipped_seed"] += 1
                continue
            rep = _pick_representative(group)
            serving = SERVING_BY_MAJOR.get(rep["식품대분류명"])
            if serving is None:
                stats["skipped_no_serving"] += 1
                continue

            item = session.scalar(
                select(NutritionItem).where(
                    NutritionItem.external_id == rep["식품코드"].strip()[:40]
                )
            )
            if item is None:
                stats["not_in_db"] += 1
                continue
            if item.is_representative:
                stats["already"] += 1
                continue

            factor = serving / float(item.base_amount)  # 통상 100 기준
            for field in _SCALED_FIELDS:
                value = getattr(item, field)
                if value is not None:
                    setattr(item, field, round(float(value) * factor, 2))
            item.base_amount = serving
            item.is_representative = True
            stats["curated"] += 1

        session.commit()
    return stats


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("사용법: python -m scripts.curate_representative_foods <음식편 CSV>")
    path = Path(sys.argv[1])
    if not path.exists():
        raise SystemExit(f"[curate] 파일 없음: {path}")
    stats = run(path)
    print(f"[curate] 시드 대표 지정: {stats['seed_marked']}건")
    print(f"[curate] 공공 대표 선정·1인분 환산: {stats['curated']}건")
    print(f"[curate] 건너뜀 — 시드 우선: {stats['skipped_seed']} / 기준표 밖 분류: {stats['skipped_no_serving']}"
          f" / 이미 대표: {stats['already']} / DB에 없음(필터 제외분): {stats['not_in_db']}")


if __name__ == "__main__":
    main()
