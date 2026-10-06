"""food_candidates 에 g 양·영양 출처·음식 번호 (g × 100g 당 모델)

Revision ID: a2b3c4d5e6f7
Revises: a7c4e9d2b518
Create Date: 2026-10-06 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "a7c4e9d2b518"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_candidates", sa.Column("estimated_grams", sa.Numeric(8, 2), nullable=True))
    op.add_column("food_candidates", sa.Column("nutrition_source", sa.String(12), nullable=True))
    op.add_column("food_candidates", sa.Column("food_index", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("food_candidates", "food_index")
    op.drop_column("food_candidates", "nutrition_source")
    op.drop_column("food_candidates", "estimated_grams")
