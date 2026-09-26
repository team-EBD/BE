"""Constrain food hierarchy values and normalize legacy family labels.

Revision ID: e2f85abc9d46
Revises: e1e74fab8c35
"""
from alembic import op

revision = "e2f85abc9d46"
down_revision = "e1e74fab8c35"
branch_labels = None
depends_on = None

# Frozen vocabulary: migrations must not change when application rules evolve.
FAMILIES = (
    "밥류", "면류", "분식류", "국·탕·찌개류", "죽·스프류", "구이·볶음·조림·찜·전류", "튀김류",
    "버거·피자·샌드위치", "빵·과자·디저트", "음료", "유제품", "샐러드·채소·나물", "과일",
    "육류·수산물·달걀", "두부·묵·콩·견과류", "김치·절임", "소스·양념", "기타",
)


def upgrade():
    op.execute("UPDATE food_groups SET family = '육류·수산물·달걀' WHERE family = '육·수산 가공'")
    op.execute("UPDATE food_groups SET family = '구이·볶음·조림·찜·전류' WHERE family = '구이·볶음·조림류'")
    op.execute("UPDATE food_groups SET companion_group_id = NULL "
               "WHERE role <> 'meal' OR companion_group_id = id")
    with op.batch_alter_table("food_groups") as batch:
        batch.create_check_constraint("ck_food_groups_family", "family IN (" + ",".join(repr(f) for f in FAMILIES) + ")")
        batch.create_check_constraint("ck_food_groups_role", "role IN ('meal','companion','snack','exclude')")
        batch.create_check_constraint("ck_food_groups_member_count", "member_count >= 0")
        batch.create_check_constraint("ck_food_groups_companion",
                                      "companion_group_id IS NULL OR (companion_group_id <> id AND role = 'meal')")
    with op.batch_alter_table("food_group_aliases") as batch:
        batch.create_check_constraint("ck_food_group_aliases_kind", "kind IN ('synonym','seed','manual','auto')")
    with op.batch_alter_table("nutrition_items") as batch:
        batch.create_check_constraint("ck_nutrition_items_serving_basis",
                                      "serving_basis IS NULL OR serving_basis IN ('per_serving','per_100g')")


def downgrade():
    with op.batch_alter_table("nutrition_items") as batch:
        batch.drop_constraint("ck_nutrition_items_serving_basis", type_="check")
    with op.batch_alter_table("food_group_aliases") as batch:
        batch.drop_constraint("ck_food_group_aliases_kind", type_="check")
    with op.batch_alter_table("food_groups") as batch:
        for name in ("family", "role", "member_count", "companion"):
            batch.drop_constraint(f"ck_food_groups_{name}", type_="check")
    op.execute("UPDATE food_groups SET family = '육·수산 가공' WHERE family IN ('육류·수산물·달걀', '두부·묵·콩·견과류')")
    op.execute("UPDATE food_groups SET family = '구이·볶음·조림류' WHERE family = '구이·볶음·조림·찜·전류'")
    op.execute("UPDATE food_groups SET family = '국·탕·찌개류' WHERE family = '죽·스프류'")
