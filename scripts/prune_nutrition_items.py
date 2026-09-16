"""nutrition_items 쳐내기 — docs/음식군-DB-계약.md §6.

    python -m scripts.prune_nutrition_items            # 대상 집계만 (dry-run)
    python -m scripts.prune_nutrition_items --apply    # 참조 재지정 → 아카이브 → 삭제 (트랜잭션 1개)

대상:
  duplicate       비대표인데 같은 normalized_name 의 대표 행이 있음 → 그 대표 행(survivor)으로 참조를 넘기고 삭제
  exclude_family  비대표이고 군의 계열이 '기타'·'소스·양념' 이면서 role=exclude → 기록 가치 없음, 삭제
                  (김치·절임은 사용자가 기록하므로 남긴다)
유지:
  100g 유일 행(비대표·대표 없음) — 검색 커버리지. serving_basis=per_100g 로 추천에서만 제외

전제: build_food_groups 가 먼저 돌아 군 대표값(G)이 채워져 있어야 한다 — 구성원이 사라지면 재계산 근거가 없다.
세 FK(meal_items·food_candidates·favorite_foods)는 ondelete=SET NULL 이라 삭제 자체는 안전하지만,
연결을 잃지 않도록 survivor 로 먼저 옮긴다. 삭제 행은 JSON 으로 nutrition_items_pruned 에 남긴다.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    FavoriteFood,
    FoodCandidate,
    FoodGroup,
    MealItem,
    NutritionItem,
    NutritionItemPruned,
)

PRUNE_EXCLUDE_FAMILIES = ("기타", "소스·양념")


def _payload(item: NutritionItem) -> dict:
    out = {}
    for col in NutritionItem.__table__.columns:
        v = getattr(item, col.name)
        out[col.name] = float(v) if isinstance(v, Decimal) else (v.isoformat() if hasattr(v, "isoformat") else v)
    return out


def find_targets(db: Session) -> tuple[dict[int, int], list[int]]:
    """(중복 행 id → survivor id, exclude_family 행 id 목록)"""
    # 대표 행: normalized_name → 가장 낮은 id (시드 우선 — 시드 id 가 가장 작다)
    rep_by_name: dict[str, int] = {}
    for item_id, name in db.execute(
        select(NutritionItem.id, NutritionItem.normalized_name)
        .where(NutritionItem.is_representative.is_(True))
        .order_by(NutritionItem.id)
    ):
        rep_by_name.setdefault(name, item_id)

    duplicates: dict[int, int] = {}
    for item_id, name in db.execute(
        select(NutritionItem.id, NutritionItem.normalized_name).where(NutritionItem.is_representative.is_(False))
    ):
        if name in rep_by_name:
            duplicates[item_id] = rep_by_name[name]

    exclude_ids = [
        r[0]
        for r in db.execute(
            select(NutritionItem.id)
            .join(FoodGroup, FoodGroup.id == NutritionItem.food_group_id)
            .where(
                NutritionItem.is_representative.is_(False),
                FoodGroup.role == "exclude",
                FoodGroup.family.in_(PRUNE_EXCLUDE_FAMILIES),
            )
        )
        if r[0] not in duplicates
    ]
    return duplicates, exclude_ids


def _repoint(db: Session, mapping: dict[int, int]) -> Counter:
    stats: Counter = Counter()
    for model, label in ((MealItem, "meal_items"), (FoodCandidate, "food_candidates"), (FavoriteFood, "favorite_foods")):
        rows = db.execute(
            select(model.id, model.nutrition_item_id).where(model.nutrition_item_id.in_(list(mapping)))
        ).all()
        if rows:
            db.execute(
                update(model),
                [{"id": rid, "nutrition_item_id": mapping[nid]} for rid, nid in rows],
            )
        stats[label] = len(rows)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="nutrition_items 쳐내기 (§6)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with SessionLocal() as db:
        macros_missing = db.scalar(
            select(func.count()).where(FoodGroup.role.in_(("meal", "snack", "companion")), FoodGroup.calories.is_(None))
        )
        duplicates, exclude_ids = find_targets(db)
        total = db.scalar(select(func.count()).select_from(NutritionItem))
        print(f"전체 {total:,} · 중복(duplicate) {len(duplicates):,} · 기록 가치 없음(exclude_family) {len(exclude_ids):,}"
              f" → 남는 행 {total - len(duplicates) - len(exclude_ids):,}")
        # 삭제 대상은 전부 비대표(per_100g) 행이라 군 대표값(per_serving 구성원 절사평균)에는 영향이 없다.
        # 미계산 군은 per_serving 구성원이 원래 없는 군 — 삭제와 무관하게 대표값을 못 만든다 (정보용).
        print(f"군 대표값 미계산(meal/snack/companion) {macros_missing}개 — per_serving 구성원 없음, 삭제와 무관")

        ref_counts = {}
        for model, label in ((MealItem, "meal_items"), (FoodCandidate, "food_candidates"), (FavoriteFood, "favorite_foods")):
            ref_counts[label] = db.scalar(
                select(func.count()).where(model.nutrition_item_id.in_(list(duplicates) + exclude_ids))
            )
        print(f"삭제 대상을 참조하는 행: {ref_counts} (중복은 survivor 로 재지정, exclude 는 SET NULL)")

        if not args.apply:
            print("\n[dry-run] --apply 로 실행")
            return
        groups_exist = db.scalar(select(func.count()).select_from(FoodGroup))
        if not groups_exist:
            print("\n[중단] food_groups 가 비어 있다 — build_food_groups 를 먼저 돌린다 (exclude_family 판정 불가)")
            return

        repointed = _repoint(db, duplicates)
        ids = list(duplicates) + exclude_ids
        archived = 0
        for i in range(0, len(ids), 2000):
            chunk = ids[i:i + 2000]
            for item in db.scalars(select(NutritionItem).where(NutritionItem.id.in_(chunk))):
                db.add(
                    NutritionItemPruned(
                        original_id=item.id,
                        survivor_id=duplicates.get(item.id),
                        reason="duplicate" if item.id in duplicates else "exclude_family",
                        payload=_payload(item),
                    )
                )
                archived += 1
            db.flush()
            db.execute(NutritionItem.__table__.delete().where(NutritionItem.id.in_(chunk)))
        db.commit()
        remaining = db.scalar(select(func.count()).select_from(NutritionItem))
        print(f"\n[apply] 참조 재지정 {dict(repointed)} · 아카이브 {archived:,} · 삭제 후 {remaining:,}행")


if __name__ == "__main__":
    main()
