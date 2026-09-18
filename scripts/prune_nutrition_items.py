"""nutrition_items 쳐내기 — docs/음식군-DB-계약.md §6.

    python -m scripts.prune_nutrition_items            # 대상 집계만 (dry-run)
    python -m scripts.prune_nutrition_items --apply    # 참조 재지정 → 아카이브 → 삭제 (트랜잭션 1개)

대상:
  duplicate       비대표인데 같은 normalized_name·food_group_id 의 대표 행이 있음 → 참조를 넘기고 삭제
유지:
  100g 유일 행(비대표·대표 없음) — 검색 커버리지. serving_basis=per_100g 로 추천에서만 제외
  exclude 역할의 음식 — 추천 제외는 기록·검색에서 불필요하다는 의미가 아니다

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

def _payload(item: NutritionItem) -> dict:
    out = {}
    for col in NutritionItem.__table__.columns:
        v = getattr(item, col.name)
        out[col.name] = float(v) if isinstance(v, Decimal) else (v.isoformat() if hasattr(v, "isoformat") else v)
    return out


def find_targets(db: Session) -> tuple[dict[int, int], list[int]]:
    """(중복 행 id → survivor id, 빈 목록). 기존 호출 형식을 유지한다."""
    # 이름이 같아도 서로 다른 군(코코아 음료/분말 등)은 중복이 아니다.
    rep_by_name: dict[tuple[str, int], int] = {}
    for item_id, name, group_id in db.execute(
        select(NutritionItem.id, NutritionItem.normalized_name, NutritionItem.food_group_id)
        .where(NutritionItem.is_representative.is_(True))
        .order_by(NutritionItem.id)
    ):
        if group_id is not None:
            rep_by_name.setdefault((name, group_id), item_id)

    duplicates: dict[int, int] = {}
    for item_id, name, group_id in db.execute(
        select(NutritionItem.id, NutritionItem.normalized_name, NutritionItem.food_group_id)
        .where(NutritionItem.is_representative.is_(False))
    ):
        key = (name, group_id)
        if key in rep_by_name:
            duplicates[item_id] = rep_by_name[key]

    return duplicates, []


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
        print(f"전체 {total:,} · 같은 군·이름의 중복(duplicate) {len(duplicates):,}"
              f" → 남는 행 {total - len(duplicates) - len(exclude_ids):,}")
        # 비대표 브랜드도 per_serving일 수 있다. 삭제 전 군 대표값을 계산하고 보존해야 한다.
        print(f"군 대표값 미계산(meal/snack/companion) {macros_missing}개 — 삭제 전 build 결과 확인 필요")

        ref_counts = {}
        for model, label in ((MealItem, "meal_items"), (FoodCandidate, "food_candidates"), (FavoriteFood, "favorite_foods")):
            ref_counts[label] = db.scalar(
                select(func.count()).where(model.nutrition_item_id.in_(list(duplicates) + exclude_ids))
            )
        print(f"삭제 대상을 참조하는 행: {ref_counts} (중복은 survivor 로 재지정)")

        if not args.apply:
            print("\n[dry-run] --apply 로 실행")
            return
        groups_exist = db.scalar(select(func.count()).select_from(FoodGroup))
        if not groups_exist:
            print("\n[중단] food_groups 가 비어 있다 — 같은 군의 중복만 정리하도록 build_food_groups 를 먼저 돌린다")
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
        counts = dict(db.execute(select(NutritionItem.food_group_id, func.count())
                                .group_by(NutritionItem.food_group_id)).all())
        for group in db.scalars(select(FoodGroup)):
            group.member_count = counts.get(group.id, 0)
        db.commit()
        remaining = db.scalar(select(func.count()).select_from(NutritionItem))
        print(f"\n[apply] 참조 재지정 {dict(repointed)} · 아카이브 {archived:,} · 삭제 후 {remaining:,}행")


if __name__ == "__main__":
    main()
