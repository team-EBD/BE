from datetime import UTC, datetime

from app.models import FoodGroup, MealItem, MealRecord, User
from scripts import backfill_meal_item_groups as backfill


def test_force_clears_stale_group_but_preserves_meal_snapshot(db_factory, monkeypatch):
    with db_factory() as db:
        user = User(social_provider="google", social_id="backfill-test", nickname="tester")
        wrong = FoodGroup(name="피자", family="버거·피자·샌드위치", role="meal")
        db.add_all([user, wrong])
        db.flush()
        record = MealRecord(user_id=user.id, meal_type="dinner", eaten_at=datetime.now(UTC), total_calories=321)
        db.add(record)
        db.flush()
        meal = MealItem(meal_record_id=record.id, food_name="알수없는음식", food_group_id=wrong.id,
                        serving_amount=1, calories=321, carbs=20, protein=30, fat=10)
        db.add(meal)
        db.commit()
        meal_id, record_id = meal.id, record.id
    monkeypatch.setattr(backfill, "SessionLocal", db_factory)
    monkeypatch.setattr("sys.argv", ["backfill", "--force", "--apply"])
    backfill.main()
    with db_factory() as db:
        saved = db.get(MealItem, meal_id)
        assert saved.food_group_id is None
        assert (saved.food_name, float(saved.calories), float(saved.serving_amount)) == ("알수없는음식", 321, 1)
        assert db.get(MealRecord, record_id).total_calories == 321
