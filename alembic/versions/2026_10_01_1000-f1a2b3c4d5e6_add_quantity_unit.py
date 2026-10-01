"""낱개 개수·단위 — food_candidates.quantity/quantity_unit/grams_per_unit, meal_items.quantity/quantity_unit

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


def downgrade() -> None:
    op.drop_column("meal_items", "quantity_unit")
    op.drop_column("meal_items", "quantity")
    op.drop_column("food_candidates", "grams_per_unit")
    op.drop_column("food_candidates", "quantity_unit")
    op.drop_column("food_candidates", "quantity")
