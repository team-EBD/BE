"""음식군 검증 질의 V1~V5 — docs/음식군-DB-계약.md §7. 각 단계 끝에 돌린다.

    python -m scripts.verify_food_groups
    python -m scripts.verify_food_groups --baseline 42036:123456   # V1 시작값(행수:max id) 비교

V6(과거 기록 API diff)·V7(3차 평가 세트 재실행)은 SQL 이 아니라 별도 절차.
"""
from __future__ import annotations

import argparse

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import FavoriteFood, FoodCandidate, FoodGroup, MealItem, MealRecord, NutritionItem


def main() -> None:
    ap = argparse.ArgumentParser(description="음식군 검증 V1~V5")
    ap.add_argument("--baseline", default=None, help="시작 시점 'count:max_id' (삭제 전 단계에서 불변 확인)")
    args = ap.parse_args()
    ok = True

    with SessionLocal() as db:
        # V1
        cnt, mx = db.execute(select(func.count(), func.max(NutritionItem.id)).select_from(NutritionItem)).one()
        line = f"V1 nutrition_items {cnt:,}행 · max(id) {mx}"
        if args.baseline:
            b_cnt, b_max = (int(x) for x in args.baseline.split(":"))
            same = (cnt, mx) == (b_cnt, b_max)
            line += f" — 시작값 {b_cnt:,}:{b_max} {'일치' if same else '불일치 (삭제 단계 후라면 정상)'}"
        print(line)

        # V2
        orphans = {}
        for model, label in ((MealItem, "meal_items"), (FoodCandidate, "food_candidates"), (FavoriteFood, "favorite_foods")):
            sub = select(NutritionItem.id).where(NutritionItem.id == model.nutrition_item_id).exists()
            orphans[label] = db.scalar(
                select(func.count()).where(model.nutrition_item_id.isnot(None), ~sub)
            )
        v2_ok = all(v == 0 for v in orphans.values())
        ok &= v2_ok
        print(f"V2 FK 고아 {orphans} {'✓' if v2_ok else '✗'}")

        # V3
        rep_total = db.scalar(select(func.count()).where(NutritionItem.is_representative.is_(True)))
        rep_null = db.scalar(
            select(func.count()).where(NutritionItem.is_representative.is_(True), NutritionItem.food_group_id.is_(None))
        )
        rep_pct = 100 * rep_null / max(rep_total, 1)
        meal_total, meal_null = db.execute(
            select(func.count(), func.count().filter(MealItem.food_group_id.is_(None)))
            .join(MealRecord, MealRecord.id == MealItem.meal_record_id)
            .where(MealRecord.deleted_at.is_(None), MealRecord.is_skipped.is_(False))
        ).one()
        meal_pct = 100 * meal_null / max(meal_total, 1)
        v3_ok = rep_pct < 5 and meal_pct < 10
        ok &= v3_ok
        print(f"V3 미분류 — 대표 행 {rep_null:,}/{rep_total:,} ({rep_pct:.1f}%, 기준<5) · "
              f"기록 {meal_null:,}/{meal_total:,} ({meal_pct:.1f}%, 기준<10) {'✓' if v3_ok else '✗'}")

        # V4
        top = db.execute(
            select(MealItem.food_name, func.count())
            .join(MealRecord, MealRecord.id == MealItem.meal_record_id)
            .where(MealRecord.deleted_at.is_(None), MealItem.food_group_id.is_(None))
            .group_by(MealItem.food_name).order_by(func.count().desc()).limit(200)
        ).all()
        print(f"V4 기록 상위 200 중 미분류 이름 {len(top)}개" + (": " + ", ".join(f"{n}({c})" for n, c in top[:20]) if top else " ✓"))

        # V5
        suspects = db.execute(
            select(FoodGroup.name, FoodGroup.role, FoodGroup.calories).where(
                ((FoodGroup.role == "meal") & (FoodGroup.calories < 100))
                | ((FoodGroup.role == "exclude") & (FoodGroup.calories > 300))
            ).order_by(FoodGroup.role, FoodGroup.calories)
        ).all()
        print(f"V5 role 경계 의심 {len(suspects)}건" + (": " + ", ".join(f"{n}({r},{float(c):.0f})" for n, r, c in suspects[:20]) if suspects else " ✓"))

    print("\n결과:", "통과" if ok else "확인 필요 (✗ 항목)")


if __name__ == "__main__":
    main()
