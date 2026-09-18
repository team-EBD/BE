"""meal_items.food_group_id backfill — docs/음식군-DB-계약.md §3 H.

    python -m scripts.backfill_meal_item_groups            # 리포트만 (dry-run)
    python -m scripts.backfill_meal_item_groups --apply    # 반영

규칙 (확신 있는 것만 — 어미 추정은 하지 않는다. 오탐이 개인 빈도를 오염시킨다):
  1. nutrition_item_id 가 있고 그 상품에 군이 있으면 → 그 군
  2. 없으면 normalize_name(food_name) 이 alias 표에 정확히 있으면 → 그 군
  3. 없으면 정규화 이름이 군명과 정확히 같으면 → 그 군
  4. 그 외 NULL (미분류) — 리포트의 '미분류 상위' 목록을 보고 alias 를 추가한 뒤 다시 돌린다

이미 채워진 행은 건드리지 않는다 (--force 로 재계산).
"""
from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.models import FoodGroup, FoodGroupAlias, MealItem, NutritionItem
from app.services.matching import normalize_name


def resolve_group_id(
    normalized: str, item_group: int | None, alias_map: dict[str, int], name_map: dict[str, int]
) -> tuple[int | None, str]:
    if item_group is not None:
        return item_group, "item"
    if normalized in alias_map:
        return alias_map[normalized], "alias"
    if normalized in name_map:
        return name_map[normalized], "group_name"
    return None, "unresolved"


def main() -> None:
    ap = argparse.ArgumentParser(description="meal_items 군 backfill")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true", help="이미 채워진 행도 재계산")
    args = ap.parse_args()

    with SessionLocal() as db:
        alias_map = {a.alias: a.group_id for a in db.scalars(select(FoodGroupAlias))}
        name_map = {normalize_name(g.name): g.id for g in db.scalars(select(FoodGroup))}
        item_group = dict(
            db.execute(
                select(NutritionItem.id, NutritionItem.food_group_id).where(
                    NutritionItem.food_group_id.isnot(None)
                )
            ).all()
        )
        stmt = select(MealItem.id, MealItem.food_name, MealItem.nutrition_item_id, MealItem.food_group_id)
        if not args.force:
            stmt = stmt.where(MealItem.food_group_id.is_(None))
        rows = db.execute(stmt).all()

        stats: Counter = Counter()
        unresolved: Counter = Counter()
        updates = []
        for item_id, food_name, nid, current in rows:
            gid, how = resolve_group_id(
                normalize_name(food_name), item_group.get(nid) if nid else None, alias_map, name_map
            )
            stats[how] += 1
            if gid is None:
                unresolved[food_name] += 1
            if gid != current:
                updates.append({"id": item_id, "food_group_id": gid})

        total = len(rows)
        resolved = total - stats["unresolved"]
        print(f"대상 {total:,}행 → 배정 {resolved:,} ({100 * resolved / max(total, 1):.0f}%) · {dict(stats)}")
        print("미분류 상위 30 (alias 추가 후보):")
        for name, n in unresolved.most_common(30):
            print(f"  {name} ({n})")

        if args.apply and updates:
            for i in range(0, len(updates), 5000):
                db.execute(update(MealItem), updates[i:i + 5000])
            db.commit()
            print(f"\n[apply] {len(updates):,}행 갱신")
        elif not args.apply:
            print("\n[dry-run] --apply 로 반영")


if __name__ == "__main__":
    main()
