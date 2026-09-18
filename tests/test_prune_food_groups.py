from app.models import FoodGroup, NutritionItem
from scripts.prune_nutrition_items import find_targets


def test_same_name_in_different_groups_is_not_a_duplicate(db_factory):
    with db_factory() as db:
        drink = FoodGroup(name="코코아", family="음료", role="snack")
        powder = FoodGroup(name="코코아가공품", family="빵·과자·디저트", role="exclude")
        db.add_all([drink, powder])
        db.flush()
        rows = []
        for group_id, representative in ((drink.id, True), (drink.id, False), (powder.id, False), (None, False)):
            row = NutritionItem(name="코코아", normalized_name="코코아", base_amount=100,
                                base_unit="g", calories=100, carbs=10, protein=5, fat=3,
                                source="public", food_group_id=group_id, is_representative=representative)
            rows.append(row)
            db.add(row)
        db.flush()
        duplicate, excluded = find_targets(db)
        assert duplicate == {rows[1].id: rows[0].id}
        assert rows[2].id not in excluded
        assert rows[3].id not in duplicate


def test_recommendation_exclusion_does_not_delete_recordable_ingredients(db_factory):
    with db_factory() as db:
        sauce = FoodGroup(name="올리브유", family="소스·양념", role="exclude")
        db.add(sauce)
        db.flush()
        oil = NutritionItem(name="올리브유", normalized_name="올리브유", base_amount=100,
                            base_unit="g", calories=900, carbs=0, protein=0, fat=100,
                            source="public", food_group_id=sauce.id, is_representative=False)
        db.add(oil)
        db.flush()
        duplicate, excluded = find_targets(db)
        assert oil.id not in duplicate
        assert excluded == []
