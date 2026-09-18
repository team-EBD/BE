"""구축 검증은 잘못된 DB를 실패 처리하고 실제 운영 조회를 하지 않는다."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Base, FoodGroup, FoodGroupAlias, MealItem, MealRecord, NutritionItem
from scripts import verify_food_groups as verifier


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        # 손상/구버전 DB 검사: 이 fixture에서만 DB 자체 CHECK/FK를 끈다.
        db.execute(text("PRAGMA ignore_check_constraints=ON"))
        yield db
    engine.dispose()


def ready(db):
    group = FoodGroup(name="김치찌개", family="국·탕·찌개류", role="meal", member_count=1)
    db.add(group)
    db.flush()
    item = NutritionItem(
        name="김치찌개", normalized_name="김치찌개", source="seed",
        base_amount=400, base_unit="g", calories=320, carbs=18, protein=22, fat=16,
        is_representative=True, food_group_id=group.id, serving_basis="per_serving",
    )
    db.add(item)
    db.flush()
    return group, item


def test_empty_database_fails(database, capsys):
    assert not verifier.verify(database)
    assert "V0 구축 군 0" in capsys.readouterr().out


def test_completed_database_passes_and_baseline_mismatch_fails(database):
    _, item = ready(database)
    assert verifier.verify(database, (1, item.id))
    assert not verifier.verify(database, (2, item.id))


@pytest.mark.parametrize("field,value", [
    ("family", "없는 계열"), ("role", "main"), ("member_count", -1),
    ("companion_group_id", 999),
])
def test_invalid_group_values_fail(database, field, value):
    group, _ = ready(database)
    setattr(group, field, value)
    database.flush()
    assert not verifier.verify(database)


@pytest.mark.parametrize("basis", [None, "per_bag"])
def test_incomplete_or_invalid_serving_basis_fails(database, basis):
    _, item = ready(database)
    item.serving_basis = basis
    database.flush()
    assert not verifier.verify(database)


def test_invalid_group_foreign_keys_fail(database):
    _, item = ready(database)
    item.food_group_id = 999
    database.add(FoodGroupAlias(alias="별칭", group_id=998, kind="manual"))
    database.flush()
    assert not verifier.verify(database)


@pytest.mark.parametrize("alias,kind", [(" 김치찌개 ", "manual"), ("김치찌개", "typo")])
def test_invalid_alias_fails(database, alias, kind):
    group, _ = ready(database)
    database.add(FoodGroupAlias(alias=alias, group_id=group.id, kind=kind))
    database.flush()
    assert not verifier.verify(database)


def test_normalized_group_name_collision_fails(database, capsys):
    ready(database)
    database.add(FoodGroup(name="김치 찌개", family="국·탕·찌개류", role="meal"))
    database.flush()
    assert not verifier.verify(database)
    assert "군명 정규화 충돌: 김치찌개 / 김치 찌개" in capsys.readouterr().out


def test_companion_requires_distinct_companion_target_and_meal_source(database):
    group, _ = ready(database)
    group.companion_group_id = group.id
    database.flush()
    assert not verifier.verify(database)
    rice = FoodGroup(name="쌀밥", family="밥류", role="meal")
    database.add(rice)
    database.flush()
    group.companion_group_id = rice.id
    database.flush()
    assert not verifier.verify(database)
    rice.role = "companion"
    database.flush()
    assert verifier.verify(database)
    group.role = "exclude"
    database.flush()
    assert not verifier.verify(database)


def test_calorie_boundary_is_advisory(database, capsys):
    group, _ = ready(database)
    group.calories = 90
    database.flush()
    assert verifier.verify(database)
    assert "V5 영양값 경계 검토(안내) 1건" in capsys.readouterr().out


def test_top_names_fail_even_when_total_coverage_passes_and_skip_deleted(database, capsys):
    group, _ = ready(database)
    now = datetime.now(timezone.utc)
    record = MealRecord(user_id=1, meal_type="dinner", eaten_at=now)
    unresolved_record = MealRecord(user_id=1, meal_type="lunch", eaten_at=now)
    database.add_all([record, unresolved_record])
    database.flush()
    for i in range(21):
        database.add(MealItem(
            meal_record_id=record.id if i < 20 else unresolved_record.id,
            food_name="김치찌개" if i < 20 else "미분류 음식", serving_amount=1,
            calories=320, carbs=18, protein=22, fat=16,
            food_group_id=group.id if i < 20 else None,
        ))
    database.flush()
    assert not verifier.verify(database)
    output = capsys.readouterr().out
    assert "기록 1/21 (4.8%, 기준<10) ✓" in output
    assert "V4 기록 상위 200 미분류 1개" in output
    unresolved_record.deleted_at = now
    database.flush()
    assert verifier.verify(database)
    unresolved_record.deleted_at = None
    unresolved_record.is_skipped = True
    database.flush()
    assert verifier.verify(database)


def test_cli_returns_nonzero_for_failed_verification(database, monkeypatch):
    monkeypatch.setattr(verifier, "SessionLocal", lambda: database)
    assert verifier.main([]) == 1
    ready(database)
    assert verifier.main([]) == 0
