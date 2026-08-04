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

# 대표식품명별 1인분 기준(g) — 대분류보다 **먼저** 적용한다 (PM 확정 2026-08-04).
#
# 왜 필요한가: '빵 및 과자류' 8,600건(음식편의 44%)은 대분류 하나로 묶기엔 섭취량 편차가
# 너무 크다 — 피자 1조각(120g)과 마카롱 1개(25g)를 같은 기준으로 둘 수 없다.
# 그래서 사람이 실제로 세는 단위(1조각·1개·1쪽)를 기준으로 대표식품명마다 잡았다.
# 피자는 두께 편차가 크지만 '조각'이 실제 섭취 단위라 100g 당보다 훨씬 낫다.
SERVING_BY_REPR = {
    # 빵 및 과자류 — 상위 항목이 전체의 80% 를 덮는다
    "피자": 120,        # 레귤러 1조각
    "케이크": 100,      # 1조각
    "버거": 200,        # 1개
    "햄버거": 200,
    "샌드위치": 180,
    "핫도그": 120,
    "토스트": 100,
    "허니브레드": 150,
    "도넛": 60,
    "와플": 80,
    "크로플": 90,
    "베이글": 90,
    "머핀": 90,
    "스콘": 70,
    "페이스트리": 70,
    "파이/만주": 70,
    "크로켓(고로케)": 80,
    "크림빵": 80,
    "팥빵": 80,
    "소보로빵": 80,
    "치즈빵": 80,
    "번": 80,
    "기타빵": 80,
    "크로와상": 60,
    "츄러스": 60,
    "프레즐": 60,
    "바게트": 60,       # 2~3조각
    "식빵": 35,         # 1쪽
    "마카롱": 25,       # 1개
    "비스킷/쿠키/크래커": 30,
    # 유제품류 및 빙과류
    "아이스크림": 100,  # 콘/바 1개
    "빙수": 300,
    "팥빙수": 300,
    "샤베트": 100,
    "밀크쉐이크": 300,
    "요구르트(액상)": 150,
    "요구르트(호상)": 100,
    "우유": 200,
    "치즈": 20,         # 슬라이스 1장
}

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


def _is_franchise(row: dict) -> bool:
    return "프랜차이즈" in (row.get("식품기원명") or "")


def _kcal_density(row: dict) -> float | None:
    """100g(기준량) 당 열량. 기준량이 다른 행은 그대로 쓰지 않는다."""
    try:
        return float(row["에너지(kcal)"])
    except (KeyError, TypeError, ValueError):
        return None


def _hybrid_calories(franchise: list[dict]) -> float | None:
    """프랜차이즈 그룹의 기준량당 열량 중앙값 (실측치)."""
    values = [d for d in (_kcal_density(r) for r in franchise) if d is not None]
    return statistics.median(values) if values else None


def run(csv_path: Path, session_factory=SessionLocal) -> dict:
    rows = read_rows(csv_path)
    usable = [
        r for r in rows
        if r["데이터구분코드"] == "D" and (r.get("에너지(kcal)") or "").strip()
    ]

    # 프랜차이즈를 버리지 않고 같은 그룹에 담는다. 대표 행은 비프랜차이즈에서 고르되
    # (탄단지 100% 실측), 열량은 프랜차이즈 실측 중앙값으로 덮는다 — 비프랜차이즈는
    # 재료량 기반 산출이라 튀김류에서 최대 50% 과소로 나온다(2026-08-04 실측:
    # 호떡 147 vs 312, 새우튀김 145 vs 244). 식단 앱에서 과소 기록이 더 위험하다.
    groups: dict[str, list[dict]] = {}
    for r in usable:
        groups.setdefault(normalize_name(_display_name(r["식품명"])), []).append(r)

    stats = {"seed_marked": 0, "curated": 0, "skipped_seed": 0,
             "skipped_no_serving": 0, "already": 0, "not_in_db": 0,
             "hybrid": 0, "franchise_only": 0}

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
            franchise = [r for r in group if _is_franchise(r)]
            general = [r for r in group if not _is_franchise(r)]
            # 대표 행은 비프랜차이즈 우선 — 탄단지가 100% 실측이다.
            # 프랜차이즈뿐인 음식(피자·버거·도넛 등 8,455건)은 그쪽에서 고른다.
            rep = _pick_representative(general or franchise)
            if not general:
                stats["franchise_only"] += 1
            # 대표식품명 기준이 대분류 기준보다 우선 (피자·마카롱처럼 편차가 큰 것 보정)
            serving = SERVING_BY_REPR.get((rep.get("대표식품명") or "").strip())
            if serving is None:
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

            base_amount = float(item.base_amount)
            factor = serving / base_amount  # 통상 100 기준
            for field in _SCALED_FIELDS:
                value = getattr(item, field)
                if value is not None:
                    setattr(item, field, round(float(value) * factor, 2))

            # 하이브리드: 열량은 프랜차이즈 실측, 탄단지는 비프랜차이즈 **비율** 유지.
            # 양쪽이 다 있을 때만 적용한다 (PM 확정 2026-08-04).
            if general and franchise:
                density = _hybrid_calories(franchise)
                if density is not None:
                    new_calories = round(density * serving / base_amount, 2)
                    carbs = float(item.carbs or 0)
                    protein = float(item.protein or 0)
                    fat = float(item.fat or 0)
                    macro_kcal = 4 * carbs + 4 * protein + 9 * fat
                    if macro_kcal > 0 and new_calories > 0:
                        # 세 값에 같은 계수를 곱하면 비율은 그대로고 합은 새 열량이 된다
                        k = new_calories / macro_kcal
                        item.carbs = round(carbs * k, 2)
                        item.protein = round(protein * k, 2)
                        item.fat = round(fat * k, 2)
                        item.calories = new_calories
                        stats["hybrid"] += 1

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
    print(f"[curate]   ├ 하이브리드(열량=프랜차이즈, 탄단지 비율=급식): {stats['hybrid']}건")
    print(f"[curate]   └ 프랜차이즈만 있어 그쪽에서 선정: {stats['franchise_only']}건")
    print(f"[curate] 건너뜀 — 시드 우선: {stats['skipped_seed']} / 기준표 밖 분류: {stats['skipped_no_serving']}"
          f" / 이미 대표: {stats['already']} / DB에 없음(필터 제외분): {stats['not_in_db']}")


if __name__ == "__main__":
    main()
