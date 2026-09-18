"""분류를 재구축해도 수동 판정·참조·영양 기준이 유지되는지 검증한다."""
import json
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import FoodGroup, FoodGroupAlias, NutritionItem
from scripts import build_food_groups as build


def group(db, name, family="밥류", role="meal", **kwargs):
    row = FoodGroup(name=name, family=family, role=role, **kwargs)
    db.add(row)
    db.flush()
    return row


def item(db, name, **kwargs):
    values = dict(name=name, normalized_name=name.replace(" ", ""), base_amount=100,
                  base_unit="g", calories=100, carbs=10, protein=10, fat=2,
                  source="public", is_representative=False)
    values.update(kwargs)
    row = NutritionItem(**values)
    db.add(row)
    db.flush()
    return row


def test_alias_manual_override_works_on_first_build_and_survives_rerun(db_factory, tmp_path):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        brown = group(db, "현미밥", role="companion")
        groups = {g.name: g for g in (rice, brown)}
        path = tmp_path / "aliases.json"
        path.write_text(json.dumps({"공기밥": "현미밥"}), encoding="utf-8")
        build.upsert_aliases(db, groups, build.Taxonomy(), path)
        assert db.get(FoodGroupAlias, "공기밥").group_id == brown.id
        build.upsert_aliases(db, groups, build.Taxonomy(), None)
        assert db.get(FoodGroupAlias, "공기밥").group_id == brown.id
        assert db.get(FoodGroupAlias, "공기밥").kind == "manual"


def test_removed_automatic_alias_is_deleted_but_manual_is_preserved(db_factory):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        db.add_all([FoodGroupAlias(alias="오류별칭", group_id=rice.id, kind="synonym"),
                    FoodGroupAlias(alias="내별칭", group_id=rice.id, kind="manual")])
        db.flush()
        build.upsert_aliases(db, {rice.name: rice}, build.Taxonomy(), None)
        assert db.get(FoodGroupAlias, "오류별칭") is None
        assert db.get(FoodGroupAlias, "내별칭") is not None


def test_names_require_majority_and_do_not_guess_suffix(db_factory):
    tax = build.Taxonomy()
    tax.name_group["동명이음식"] = Counter({"달걀": 2, "과일": 2})
    assert tax.group_by_name("동명이음식") is None
    with db_factory() as db:
        salad = group(db, "닭가슴살 샐러드", family="샐러드·채소·나물")
        known = item(db, "닭가슴살 샐러드")
        unknown = item(db, "다른닭가슴살샐러드")
        build.assign_items(db, {salad.name: salad}, tax)
        db.expire_all()
        assert db.get(NutritionItem, known.id).food_group_id == salad.id
        assert db.get(NutritionItem, unknown.id).food_group_id is None


def test_broad_source_groups_are_refined_only_by_unambiguous_dish_name(tmp_path):
    dish = tmp_path / "dish.jsonl"
    processed = tmp_path / "processed.jsonl"
    dish.write_text("\n".join(json.dumps(r) for r in [
        {"foodCd": "D1", "foodLv3Nm": "밥류", "foodLv4Nm": "볶음밥", "foodNm": "볶음밥_새우볶음밥"},
        {"foodCd": "D2", "foodLv3Nm": "빵 및 과자류", "foodLv4Nm": "피자", "foodNm": "피자_불고기"},
        {"foodCd": "D3", "foodLv3Nm": "밥류", "foodLv4Nm": "덮밥", "foodNm": "덮밥_불고기"},
        {"foodCd": "D4", "foodLv3Nm": "찌개 및 전골류", "foodLv4Nm": "김치찌개", "foodNm": "김치찌개_삼겹살"},
    ]), encoding="utf-8")
    processed.write_text("\n".join(json.dumps(r) for r in [
        {"foodCd": "P1", "foodLv3Nm": "즉석식품류", "foodLv4Nm": "밥류", "foodNm": "새우볶음밥"},
        {"foodCd": "P2", "foodLv3Nm": "즉석식품류", "foodLv4Nm": "밥류", "foodNm": "특제밥"},
        {"foodCd": "P3", "foodLv3Nm": "식육가공품 및 포장육", "foodLv4Nm": "양념육", "foodNm": "불고기"},
        {"foodCd": "P4", "foodLv3Nm": "식육가공품 및 포장육", "foodLv4Nm": "양념육", "foodNm": "삼겹살"},
    ]), encoding="utf-8")
    tax = build.Taxonomy()
    tax.scan(dish, "D")
    tax.scan(processed, "P")
    assert tax.code_group["P1"] == "볶음밥"
    assert tax.code_group["P2"] == "밥류"
    assert tax.code_group["P3"] == "양념육"
    assert tax.code_group["P4"] == "양념육"
    assert tax.refined_count == 1


def test_seed_bulgogi_is_not_misclassified_as_pizza_flavor(db_factory):
    with db_factory() as db:
        tax = build.Taxonomy()
        tax.name_group["불고기"] = Counter({"피자": 3, "덮밥": 1, "양념육": 2})
        groups = build.upsert_groups(db, tax)
        build.upsert_aliases(db, groups, tax, None)
        build.assign_items(db, groups, tax)
        food = db.scalar(select(NutritionItem).where(NutritionItem.name == "불고기"))
        assert food.food_group_id == groups["불고기"].id


def test_rebuild_does_not_reactivate_stale_generated_item(db_factory):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        stale = item(db, "쌀밥", external_id="gen:stale", serving_basis="per_serving")
        build.assign_items(db, {rice.name: rice}, build.Taxonomy())
        db.expire_all()
        assert db.get(NutritionItem, stale.id).food_group_id is None


def test_merge_preserves_item_reference_and_is_idempotent(db_factory):
    with db_factory() as db:
        old = group(db, "즉석 피자", family="버거·피자·샌드위치")
        target = group(db, "피자", family="버거·피자·샌드위치")
        food = item(db, "치즈피자", food_group_id=old.id)
        db.add(FoodGroupAlias(alias="피자별칭", group_id=old.id, kind="manual"))
        db.flush()
        tax = build.Taxonomy()
        tax.group_count["피자"] = 1
        tax.group_families["피자"][target.family] = 1
        tax.group_sources["피자"].add("즉석 피자")
        for _ in range(2):
            build.upsert_groups(db, tax)
            db.expire_all()
            assert db.get(NutritionItem, food.id).food_group_id == target.id
            assert db.get(FoodGroupAlias, "피자별칭").group_id == target.id
            assert db.scalar(select(FoodGroup).where(FoodGroup.name == "즉석 피자")) is None


def test_changed_role_clears_companion_before_flush(db_factory):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        egg = group(db, "달걀말이", family="구이·볶음·조림·찜·전류", companion_group_id=rice.id)
        tax = build.Taxonomy()
        for g in (rice, egg):
            tax.group_count[g.name] = 1
            tax.group_families[g.name][g.family] = 1
        build.upsert_groups(db, tax)
        assert egg.role == "exclude"
        assert egg.companion_group_id is None


def test_rebuild_rejects_unhandled_normalized_group_collision(db_factory):
    tax = build.Taxonomy()
    tax.group_count.update({"동일 음식": 1, "동일음식": 1})
    with db_factory() as db, pytest.raises(ValueError, match="정규화 충돌"):
        build.upsert_groups(db, tax)


def test_serving_basis_preserves_audit_and_recognizes_converted_brand(db_factory, tmp_path):
    with db_factory() as db:
        audited = item(db, "감사강등", is_representative=True, serving_basis="per_100g")
        brand = item(db, "개별상품", base_amount=150, external_id="P1", serving_basis="per_100g")
        drink = item(db, "음료상품", base_amount=200, base_unit="ml", category="음료",
                     external_id="P2", serving_basis="per_100g")
        unknown = item(db, "미판정", base_amount=250)
        tax = build.Taxonomy()
        tax.code_serving["P1"] = (150, "g")
        tax.code_serving["P2"] = (200, "g")
        build.fill_serving_basis(db, tmp_path / "audit.csv", tax)
        db.expire_all()
        assert db.get(NutritionItem, audited.id).serving_basis == "per_100g"
        assert db.get(NutritionItem, brand.id).serving_basis == "per_serving"
        assert db.get(NutritionItem, drink.id).serving_basis == "per_serving"
        assert db.get(NutritionItem, unknown.id).serving_basis is None


def test_obsolete_automatic_group_cannot_keep_stale_recommendation(db_factory):
    with db_factory() as db:
        obsolete = group(db, "쿠키", family="빵·과자·디저트", role="snack", calories=450, member_count=10)
        build.upsert_groups(db, build.Taxonomy())
        assert db.get(FoodGroup, obsolete.id) is obsolete
        assert obsolete.role == "exclude"
        assert obsolete.calories is None
        assert obsolete.member_count == 0


def test_macros_use_matching_unit_and_clear_stale_count_and_companion(db_factory):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        soup = group(db, "테스트국", family="국·탕·찌개류", companion_group_id=rice.id)
        empty = group(db, "빈군", member_count=999, calories=123)
        item(db, "국1", food_group_id=soup.id, serving_basis="per_serving", base_amount=100, calories=10)
        item(db, "국2", food_group_id=soup.id, serving_basis="per_serving", base_amount=300, calories=30)
        item(db, "국3", food_group_id=soup.id, serving_basis="per_serving", base_unit="ml", calories=900)
        item(db, "국100g", food_group_id=soup.id, serving_basis="per_100g", calories=999)
        build.fill_group_macros(db, {g.name: g for g in (rice, soup, empty)})
        assert soup.calories == 20
        assert soup.base_amount == 200
        assert soup.base_unit == "g"
        assert soup.member_count == 4
        assert soup.role == "exclude"
        assert soup.companion_group_id is None
        assert empty.member_count == 0
        assert empty.calories is None


@pytest.mark.parametrize("rice_first", [False, True])
def test_analyzed_sundubu_keeps_meal_role_with_rice_after_rebuild(db_factory, rice_first):
    """MFDS D106-291030000-0001: 35kcal/100g × 데모 200g + 쌀밥 210g.

    순두부찌개 단독 70kcal만 보고 반찬으로 강등하지 않고, 모든 군의 환산을 마친
    후 동반 포함 418.6kcal를 사용한다. 기존 자동 강등도 재구축 시 복구한다.
    """
    fixture = json.loads((Path(__file__).parent / "fixtures" / "food_group_companion.json").read_text())
    specs = {spec["group_name"]: spec for spec in fixture["items"]}
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        soup = group(db, "순두부찌개", family="국·탕·찌개류", role="exclude",
                     note="auto: 대표값 70kcal < 80 (반찬)")
        tax = build.Taxonomy()
        for row in (soup, rice):
            tax.group_count[row.name] = 1
            tax.group_families[row.name][row.family] = 1
            spec = specs[row.name]
            raw = spec["original"]
            scale = spec["demo_serving_g"] / 100
            item(db, raw["foodNm"], food_group_id=row.id, serving_basis="per_serving",
                 base_amount=spec["demo_serving_g"], external_id=raw["foodCd"],
                 **{field: float(raw[source]) * scale for field, source in
                    (("calories", "enerc"), ("carbs", "chocdf"), ("protein", "prot"), ("fat", "fatce"))})
        for _ in range(2):
            build.upsert_groups(db, tax)
            ordered = (rice, soup) if rice_first else (soup, rice)
            build.fill_group_macros(db, {row.name: row for row in ordered})
            assert soup.calories == 70
            assert rice.calories == pytest.approx(348.6)
            assert soup.role == "meal"
            assert soup.note is None
            assert soup.companion_group_id == rice.id


def test_rice_does_not_rescue_explicit_side_or_standalone_small_dish(db_factory):
    with db_factory() as db:
        rice = group(db, "쌀밥", role="companion")
        side = group(db, "멸치볶음", family="구이·볶음·조림·찜·전류", role="exclude")
        salad = group(db, "미니 샐러드", family="샐러드·채소·나물")
        for row, calories in ((rice, 300), (side, 20), (salad, 20)):
            item(db, row.name, food_group_id=row.id, serving_basis="per_serving", calories=calories)
        build.fill_group_macros(db, {row.name: row for row in (rice, side, salad)})
        assert side.role == salad.role == "exclude"
        assert side.companion_group_id is salad.companion_group_id is None
