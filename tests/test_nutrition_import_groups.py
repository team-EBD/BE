"""영양 DB 재적재·대표 생성 후에도 음식군과 제공량 기준이 유지되는지 검증한다."""
from __future__ import annotations

import csv
import json

import pytest
from sqlalchemy import select

from app.models import FoodGroup, FoodGroupAlias, NutritionItem
from app.services.recommend.groups import load_group_index
from scripts import build_generic_foods as generic
from scripts import curate_representative_foods as curate
from scripts import import_mfds_api as mfds
from scripts import import_public_nutrition as public
from scripts.seed_nutrition_items import seed


def _group(db, name, family="국·탕·찌개류", role="meal"):
    group = FoodGroup(name=name, family=family, role=role)
    db.add(group)
    db.flush()
    return group


def _raw(**changes):
    row = {
        "식품코드": "D-IMPORT-1", "식품명": "특제국", "데이터구분코드": "D",
        "대표식품명": "국", "식품대분류명": "국 및 탕류", "식품기원명": "급식",
        "영양성분함량기준량": "100g", "식품중량": "400g",
        "에너지(kcal)": "50", "탄수화물(g)": "5", "단백질(g)": "3", "지방(g)": "2",
    }
    return {**row, **changes}


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("amount,basis", [("100g", "per_100g"), ("200g", None)])
def test_raw_transform_sets_basis_without_assuming_non_100_is_a_serving(amount, basis):
    values = public.transform(_raw(**{"영양성분함량기준량": amount}))
    assert values["serving_basis"] == basis
    assert values["is_representative"] is False


def test_raw_transform_resolves_canonical_source_group(db_factory):
    with db_factory() as db:
        burger = _group(db, "햄버거", "버거·피자·샌드위치")
        values = public.transform(_raw(**{"대표식품명": "버거"}), load_group_index(db))
        assert values["food_group_id"] == burger.id
        assert public.transform(_raw(**{"대표식품명": "미등록군"}), load_group_index(db))["food_group_id"] is None


def test_cocoa_drink_and_processed_ingredient_keep_distinct_groups(db_factory):
    with db_factory() as db:
        drink = _group(db, "코코아", "음료", "snack")
        processed = _group(db, "코코아가공품", "소스·양념", "exclude")
        index = load_group_index(db)
        assert public.source_group_id(_raw(**{"대표식품명": "코코아"}), index) == drink.id
        assert public.source_group_id(_raw(**{"대표식품명": "코코아", "데이터구분코드": "P"}), index) == processed.id


@pytest.mark.parametrize("ids,expected", [
    ([1, 1, 2], 1), ([1, 2], None), ([1, 2, None], None),
    ([1, 1, None], 1), ([1, None, None], None), ([], None),
])
def test_representative_requires_majority_of_all_members(ids, expected):
    assert public.majority_group_id(ids) == expected


def test_csv_reimport_and_recuration_preserve_basis_and_identity(db_factory, tmp_path, monkeypatch):
    path = tmp_path / "foods.csv"
    _write_csv(path, [_raw()])
    monkeypatch.setattr(public.MacroEstimator, "save", lambda *_: None)
    with db_factory() as db:
        group_id = _group(db, "국").id
        db.commit()
    public.run([path], session_factory=db_factory)
    curate.run(path, session_factory=db_factory)
    with db_factory() as db:
        item = db.scalar(select(NutritionItem).where(NutritionItem.external_id == "D-IMPORT-1"))
        item_id = item.id
        assert (float(item.calories), float(item.base_amount)) == (200, 400)
        assert item.is_representative and item.serving_basis == "per_serving"
        assert item.food_group_id == group_id

    public.run([path], session_factory=db_factory)
    with db_factory() as db:
        item = db.get(NutritionItem, item_id)
        assert (float(item.calories), float(item.base_amount)) == (50, 100)
        assert item.is_representative is False and item.serving_basis == "per_100g"
        assert item.food_group_id == group_id
    curate.run(path, session_factory=db_factory)
    curate.run(path, session_factory=db_factory)
    with db_factory() as db:
        item = db.get(NutritionItem, item_id)
        assert (float(item.calories), float(item.base_amount)) == (200, 400)
        assert item.is_representative and item.serving_basis == "per_serving"


def test_curated_group_tie_stays_unclassified(db_factory, tmp_path, monkeypatch):
    path = tmp_path / "ambiguous.csv"
    _write_csv(path, [_raw(), _raw(**{"식품코드": "D-IMPORT-2", "대표식품명": "탕"})])
    monkeypatch.setattr(public.MacroEstimator, "save", lambda *_: None)
    with db_factory() as db:
        _group(db, "국")
        _group(db, "탕")
        db.commit()
    public.run([path], session_factory=db_factory)
    curate.run(path, session_factory=db_factory)
    with db_factory() as db:
        representative = db.scalar(select(NutritionItem).where(
            NutritionItem.external_id == "D-IMPORT-1",
        ))
        assert representative.is_representative
        assert representative.food_group_id is None


def _api_rows(serving="30g", group_names=("과자", "과자", "빵")):
    return [
        {
            "foodCd": f"P-IMPORT-{i}", "foodNm": "특제과자", "dataCd": "P",
            "foodLv3Nm": "과자류·빵류 또는 떡류", "foodLv4Nm": group,
            "nutConSrtrQua": "100g", "enerc": "400", "chocdf": "70", "prot": "5", "fatce": "10",
            "foodSize": "150g", "servSize": serving,
            "mfrNm": "오리온" if i == 0 else f"독립제조사{i}",
        }
        for i, group in enumerate(group_names)
    ]


@pytest.mark.parametrize("serving,amount,kcal", [("30g", 30, 120), ("100g", 100, 400)])
def test_processed_import_and_refresh_classify_brand_and_representative(db_factory, tmp_path, serving, amount, kcal):
    with db_factory() as db:
        cookie = _group(db, "과자", "빵·과자·디저트", "snack")
        bread = _group(db, "빵", "빵·과자·디저트", "snack")
        cookie_id, bread_id = cookie.id, bread.id
        db.commit()
    path = tmp_path / "processed.jsonl"
    rows = _api_rows(serving=serving)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    mfds.run(path, min_group=3, session_factory=db_factory)
    with db_factory() as db:
        brand = db.scalar(select(NutritionItem).where(NutritionItem.external_id == "P-IMPORT-0"))
        representative = db.scalar(select(NutritionItem).where(NutritionItem.external_id.like("rep:%")))
        representative_id = representative.id
        assert brand.is_representative is False
        assert (brand.serving_basis, float(brand.base_amount), float(brand.calories)) == ("per_serving", amount, kcal)
        assert brand.food_group_id == cookie_id
        assert representative.food_group_id == cookie_id
        assert representative.serving_basis == "per_serving" and representative.is_representative

    rows = _api_rows(serving=serving, group_names=("과자", "빵", "빵"))
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    result = mfds.run(path, min_group=3, session_factory=db_factory)
    assert result["reps"] == 1 and result["updated"] == 2
    with db_factory() as db:
        representative = db.get(NutritionItem, representative_id)
        assert representative.food_group_id == bread_id
        assert float(representative.calories) == kcal


def test_seed_sets_serving_and_resolves_alias_on_rerun(db_factory):
    with db_factory() as db:
        rice = _group(db, "쌀밥", "밥류", "companion")
        db.add(FoodGroupAlias(alias="공기밥", group_id=rice.id, kind="seed"))
        group_id = rice.id
        item = db.scalar(select(NutritionItem).where(NutritionItem.normalized_name == "공기밥"))
        item_id = item.id
        db.commit()
    seed(session_factory=db_factory)
    with db_factory() as db:
        item = db.get(NutritionItem, item_id)
        assert item.food_group_id == group_id
        assert item.serving_basis == "per_serving" and item.is_representative


def test_generic_build_uses_serving_items_and_inherits_majority(db_factory, monkeypatch):
    with db_factory() as db:
        group = _group(db, "국수", "면류")
        group_id = group.id
        for i in range(5):
            db.add(NutritionItem(
                name=f"특제{i}특수국수", normalized_name=f"특제{i}특수국수", source="public",
                external_id=f"D-NOODLE-{i}", is_representative=True, serving_basis="per_serving",
                food_group_id=group_id, base_amount=300, base_unit="g", category="면류",
                calories=300, carbs=50, protein=10, fat=6,
            ))
        db.add(NutritionItem(
            name="특수국수", normalized_name="특수국수", source="public", external_id="D-PER100-NOODLE",
            is_representative=True, serving_basis="per_100g", base_amount=100, base_unit="g",
            category="면류", calories=100, carbs=17, protein=3, fat=2,
        ))
        db.commit()
    monkeypatch.setattr(generic, "GENERIC_TERMS", ["특수국수"])
    result = generic.run(session_factory=db_factory)
    assert result["created"] == 1
    with db_factory() as db:
        item = db.scalar(select(NutritionItem).where(NutritionItem.external_id == generic._external_id("특수국수")))
        assert item.food_group_id == group_id and item.serving_basis == "per_serving"
        assert (float(item.calories), float(item.base_amount)) == (300, 300)


@pytest.mark.parametrize("reason", ["disappeared", "failed_build", "preferred_item"])
def test_generic_refresh_retires_stale_values_without_deleting_identity(db_factory, monkeypatch, reason):
    monkeypatch.setattr(generic, "GENERIC_TERMS", ["검증국수"])
    with db_factory() as db:
        group_id = _group(db, "국수", "면류").id
        for i in range(5):
            db.add(NutritionItem(
                name=f"특제{i}검증국수", normalized_name=f"특제{i}검증국수", source="public",
                external_id=f"D-LIFECYCLE-{i}", is_representative=True, serving_basis="per_serving",
                food_group_id=group_id, base_amount=300, base_unit="g", category="면류",
                calories=300, carbs=50, protein=10, fat=6,
            ))
        db.commit()
    assert generic.run(session_factory=db_factory)["created"] == 1
    with db_factory() as db:
        item = db.scalar(select(NutritionItem).where(NutritionItem.external_id == generic._external_id("검증국수")))
        item_id = item.id
        before = (item.name, item.base_amount, item.calories, item.carbs, item.protein, item.fat)
        for member in db.scalars(select(NutritionItem).where(NutritionItem.external_id.like("D-LIFECYCLE-%"))):
            if reason == "disappeared":
                member.is_representative = False
            elif reason == "failed_build":
                member.base_amount = 700
        if reason == "preferred_item":
            db.add(NutritionItem(
                name="검증국수", normalized_name="검증국수", source="public", external_id="D-PREFERRED",
                is_representative=True, serving_basis="per_serving", food_group_id=group_id,
                base_amount=300, base_unit="g", calories=310, carbs=50, protein=10, fat=6,
            ))
        db.commit()
    assert generic.run(session_factory=db_factory)["retired"] == 1
    assert generic.run(session_factory=db_factory)["retired"] == 0
    with db_factory() as db:
        old = db.get(NutritionItem, item_id)
        assert not old.is_representative and old.food_group_id is None
        assert old.serving_basis == "per_serving"
        assert (old.name, old.base_amount, old.calories, old.carbs, old.protein, old.fat) == before
        for member in db.scalars(select(NutritionItem).where(NutritionItem.external_id.like("D-LIFECYCLE-%"))):
            member.is_representative, member.base_amount = True, 300
        preferred = db.scalar(select(NutritionItem).where(NutritionItem.external_id == "D-PREFERRED"))
        if preferred:
            preferred.is_representative = False
        db.commit()
    assert generic.run(session_factory=db_factory)["updated"] == 1
    with db_factory() as db:
        assert db.get(NutritionItem, item_id).is_representative
        assert db.get(NutritionItem, item_id).food_group_id == group_id


@pytest.mark.parametrize("reason", ["disappeared", "insufficient_members", "failed_build", "preferred_item"])
def test_processed_refresh_retires_stale_rep_and_can_restore_same_id(db_factory, tmp_path, reason):
    with db_factory() as db:
        group_id = _group(db, "과자", "빵·과자·디저트", "snack").id
        _group(db, "빵", "빵·과자·디저트", "snack")
        db.commit()
    path = tmp_path / "processed.jsonl"
    rows = _api_rows()

    def write_rows(values):
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in values), encoding="utf-8")

    write_rows(rows)
    assert mfds.run(path, min_group=3, session_factory=db_factory)["reps"] == 1
    with db_factory() as db:
        old = db.scalar(select(NutritionItem).where(NutritionItem.external_id.like("rep:%")))
        item_id = old.id
        before = (old.name, old.base_amount, old.calories, old.carbs, old.protein, old.fat)
        if reason == "preferred_item":
            db.add(NutritionItem(
                name="특제과자", normalized_name="특제과자", source="seed", is_representative=True,
                serving_basis="per_serving", food_group_id=group_id, base_amount=30,
                base_unit="g", calories=130, carbs=20, protein=2, fat=4,
            ))
            db.commit()
    if reason == "disappeared":
        write_rows([])
    elif reason == "insufficient_members":
        write_rows(rows[:2])
    elif reason == "failed_build":
        write_rows([{**r, "servSize": "", "foodSize": "100g"} for r in rows])
    assert mfds.run(path, min_group=3, session_factory=db_factory)["retired"] == 1
    assert mfds.run(path, min_group=3, session_factory=db_factory)["retired"] == 0
    with db_factory() as db:
        old = db.get(NutritionItem, item_id)
        assert not old.is_representative and old.food_group_id is None
        assert old.serving_basis == "per_serving"
        assert (old.name, old.base_amount, old.calories, old.carbs, old.protein, old.fat) == before
        preferred = db.scalar(select(NutritionItem).where(
            NutritionItem.normalized_name == "특제과자", NutritionItem.source == "seed",
        ))
        if preferred:
            preferred.is_representative = False
            db.commit()
    write_rows(rows)
    assert mfds.run(path, min_group=3, session_factory=db_factory)["reps"] == 1
    with db_factory() as db:
        assert db.get(NutritionItem, item_id).is_representative
        assert db.get(NutritionItem, item_id).food_group_id == group_id
