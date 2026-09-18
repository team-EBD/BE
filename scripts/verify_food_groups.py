"""음식군 구축 검증. 실패 시 종료 코드 1, 영양값 경계(V5)는 검토용 안내.

    python -m scripts.verify_food_groups
    python -m scripts.verify_food_groups --baseline 42036:123456

baseline 은 삭제 전 행 수·최대 id 보존을 확인할 때만 지정한다.
V6(과거 기록 응답)·V7(매칭 회귀)은 별도의 테스트로 확인한다.
"""
from __future__ import annotations

import argparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    FavoriteFood, FoodCandidate, FoodGroup, FoodGroupAlias, MealItem, MealRecord,
    NutritionItem, RecommendationItem,
)
from scripts.food_group_taxonomy import FAMILIES, norm


def verify(db: Session, baseline: tuple[int, int] | None = None) -> bool:
    """읽기 전용 구축 검증. 빈 DB·불완전한 분류를 통과시키지 않는다."""
    ok = True

    def check(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok &= passed
        print(f"{label} {detail} {'✓' if passed else '✗'}")

    groups = list(db.scalars(select(FoodGroup)))
    by_id = {g.id: g for g in groups}
    cnt, mx = db.execute(select(func.count(), func.max(NutritionItem.id)).select_from(NutritionItem)).one()
    rep_total = db.scalar(select(func.count()).where(NutritionItem.is_representative.is_(True)))
    check("V0 구축", bool(groups) and cnt > 0 and rep_total > 0,
          f"군 {len(groups):,} · 상품 {cnt:,} · 대표 {rep_total:,}")
    check("V1 행 보존", baseline is None or (cnt, mx) == baseline,
          f"nutrition_items {cnt:,}행 · max(id) {mx}" + (f" · 시작값 {baseline}" if baseline else ""))

    orphans = {}
    for model, column, target in (
        (MealItem, MealItem.nutrition_item_id, NutritionItem.id),
        (FoodCandidate, FoodCandidate.nutrition_item_id, NutritionItem.id),
        (FavoriteFood, FavoriteFood.nutrition_item_id, NutritionItem.id),
        (NutritionItem, NutritionItem.food_group_id, FoodGroup.id),
        (MealItem, MealItem.food_group_id, FoodGroup.id),
        (FoodGroupAlias, FoodGroupAlias.group_id, FoodGroup.id),
        (RecommendationItem, RecommendationItem.food_group_id, FoodGroup.id),
    ):
        exists = select(target).where(target == column).exists()
        orphans[f"{model.__tablename__}.{column.key}"] = db.scalar(
            select(func.count()).select_from(model).where(column.isnot(None), ~exists)
        )
    check("V2 FK 고아", not any(orphans.values()), str(orphans))

    rep_null = db.scalar(select(func.count()).where(
        NutritionItem.is_representative.is_(True), NutritionItem.food_group_id.is_(None)
    ))
    active_meals = (MealRecord.deleted_at.is_(None), MealRecord.is_skipped.is_(False))
    meal_total, meal_null = db.execute(
        select(func.count(), func.count().filter(MealItem.food_group_id.is_(None)))
        .select_from(MealItem).join(MealRecord, MealRecord.id == MealItem.meal_record_id)
        .where(*active_meals)
    ).one()
    rep_pct = 100 * rep_null / max(rep_total, 1)
    meal_pct = 100 * meal_null / max(meal_total, 1)
    check("V3 미분류", rep_pct < 5 and meal_pct < 10,
          f"대표 {rep_null:,}/{rep_total:,} ({rep_pct:.1f}%, 기준<5) · "
          f"기록 {meal_null:,}/{meal_total:,} ({meal_pct:.1f}%, 기준<10)")

    # 전체 상위 200개 이름에서 미분류를 확인한다. 미분류만 먼저 필터하면
    # '상위 200'이 아닌 롱테일 전체를 실패 처리하게 된다.
    top = db.execute(
        select(MealItem.food_name, func.count(),
               func.count().filter(MealItem.food_group_id.is_(None)))
        .join(MealRecord, MealRecord.id == MealItem.meal_record_id).where(*active_meals)
        .group_by(MealItem.food_name).order_by(func.count().desc(), MealItem.food_name).limit(200)
    ).all()
    unresolved = [(name, count) for name, _, count in top if count]
    check("V4 기록 상위 200 미분류", not unresolved,
          f"{len(unresolved)}개" + (": " + ", ".join(f"{n}({c})" for n, c in unresolved[:20]) if unresolved else ""))

    # 저열량 식사와 고열량 양념은 실제로 존재하므로 영양값만으로 오분류를
    # 확정하지 않는다. role·계열·참조 위반은 아래 V8에서 실패 처리한다.
    suspects = [g for g in groups if g.calories is not None and (
        (g.role == "meal" and g.calories < 100) or (g.role == "exclude" and g.calories > 300)
    )]
    print(f"V5 영양값 경계 검토(안내) {len(suspects)}건" + (
        ": " + ", ".join(f"{g.name}({g.role},{float(g.calories):.0f})" for g in suspects[:20]) if suspects else ""
    ))

    issues = []
    normalized_groups: dict[str, str] = {}
    for g in groups:
        key = norm(g.name)
        if key in normalized_groups:
            issues.append(f"군명 정규화 충돌: {normalized_groups[key]} / {g.name}")
        else:
            normalized_groups[key] = g.name
        if g.family not in FAMILIES:
            issues.append(f"{g.name}: 잘못된 계열 {g.family}")
        if g.role not in {"meal", "companion", "snack", "exclude"}:
            issues.append(f"{g.name}: 잘못된 role {g.role}")
        if g.member_count < 0:
            issues.append(f"{g.name}: 음수 member_count")
        if g.companion_group_id is not None:
            companion = by_id.get(g.companion_group_id)
            if (g.role != "meal" or companion is None or companion.id == g.id
                    or companion.role != "companion"):
                issues.append(f"{g.name}: 잘못된 동반 군")
    for alias in db.scalars(select(FoodGroupAlias)):
        if not alias.alias or alias.alias != norm(alias.alias):
            issues.append(f"alias {alias.alias}: 정규화 불일치")
        if alias.kind not in {"synonym", "seed", "manual", "auto"}:
            issues.append(f"alias {alias.alias}: 잘못된 kind")
    basis_invalid = db.scalar(select(func.count()).where(
        NutritionItem.serving_basis.isnot(None),
        NutritionItem.serving_basis.notin_(("per_serving", "per_100g")),
    ))
    basis_missing = db.scalar(select(func.count()).where(NutritionItem.serving_basis.is_(None)))
    if basis_invalid:
        issues.append(f"serving_basis 잘못된 값 {basis_invalid}행")
    if basis_missing:
        issues.append(f"serving_basis 미판정 {basis_missing}행")
    check("V8 구조 무결성", not issues,
          f"{len(issues)}건" + (": " + " · ".join(issues[:20]) if issues else ""))
    print("\n결과:", "통과" if ok else "확인 필요 (✗ 항목)")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="음식군 구축 검증")
    ap.add_argument("--baseline", default=None, help="삭제 전 시작값 'count:max_id' (불일치 시 실패)")
    args = ap.parse_args(argv)
    baseline = None
    if args.baseline:
        try:
            count, maximum = args.baseline.split(":")
            baseline = (int(count), int(maximum))
        except ValueError:
            ap.error("--baseline 은 count:max_id 형식이어야 합니다")
    with SessionLocal() as db:
        return 0 if verify(db, baseline) else 1


if __name__ == "__main__":
    raise SystemExit(main())
