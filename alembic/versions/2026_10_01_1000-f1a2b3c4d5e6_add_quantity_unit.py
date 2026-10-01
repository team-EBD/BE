"""낱개 개수·단위 컬럼 + 시드 기준량 보정(컵라면·치킨)

Revision ID: f1a2b3c4d5e6
Revises: e3a96bcd0e57

AI 가 "피자 8조각" 처럼 센 개수를 저장해 화면에 인분 대신 개수로 보여 주기 위한 컬럼. 전부 NULL 허용,
기존 행 영향 없음. 인분 배수(serving_amount·estimated_serving)는 그대로 계산·저장의 기준이다.
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e3a96bcd0e57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_candidates", sa.Column("quantity", sa.Numeric(8, 2), nullable=True))
    op.add_column("food_candidates", sa.Column("quantity_unit", sa.String(10), nullable=True))
    op.add_column("food_candidates", sa.Column("grams_per_unit", sa.Numeric(8, 2), nullable=True))
    op.add_column("meal_items", sa.Column("quantity", sa.Numeric(8, 2), nullable=True))
    op.add_column("meal_items", sa.Column("quantity_unit", sa.String(10), nullable=True))
    # 시드 기준량 데이터 보정 (seed/nutrition_items_seed.json 과 동일 값):
    # - 컵라면 110g(건면 포장) → 320g(조리 후). 사진의 조리량으로 나누면 2.7인분이 되던 것
    # - 후라이드/양념치킨 300g(3~4조각) → 450g(반 마리) = 1인분, 영양값 ×1.5
    op.execute("UPDATE nutrition_items SET base_amount = 320 WHERE source = 'seed' AND name = '컵라면'")
    op.execute(
        "UPDATE nutrition_items SET base_amount = 450, calories = 1170, carbs = 45, protein = 72, fat = 75 "
        "WHERE source = 'seed' AND name = '후라이드치킨' AND base_amount = 300"
    )
    op.execute(
        "UPDATE nutrition_items SET base_amount = 450, calories = 1290, carbs = 82.5, protein = 66, fat = 75 "
        "WHERE source = 'seed' AND name = '양념치킨' AND base_amount = 300"
    )


def downgrade() -> None:
    # 시드 기준량 보정은 되돌리지 않는다 (데이터 교정)
    op.drop_column("meal_items", "quantity_unit")
    op.drop_column("meal_items", "quantity")
    op.drop_column("food_candidates", "grams_per_unit")
    op.drop_column("food_candidates", "quantity_unit")
    op.drop_column("food_candidates", "quantity")
