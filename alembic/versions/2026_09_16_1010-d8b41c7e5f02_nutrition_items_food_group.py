"""nutrition_items: food_group_id, serving_basis

상품(3층) → 군(2층) 연결과 영양값 기준량 명시 (docs/음식군-DB-계약.md §2.3).
둘 다 NULL 허용 — 채워지기 전·못 채운 행도 유효하다. id·is_representative 는 건드리지 않는다.

Revision ID: d8b41c7e5f02
Revises: d7f3a9c21e40
Create Date: 2026-09-16 10:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8b41c7e5f02"
down_revision: Union[str, None] = "d7f3a9c21e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nutrition_items", sa.Column("food_group_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "nutrition_items", sa.Column("serving_basis", sa.String(length=12), nullable=True)
    )
    op.create_foreign_key(
        "fk_nutrition_items_food_group",
        "nutrition_items",
        "food_groups",
        ["food_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_nutrition_items_food_group_id", "nutrition_items", ["food_group_id"])


def downgrade() -> None:
    op.drop_index("ix_nutrition_items_food_group_id", table_name="nutrition_items")
    op.drop_constraint("fk_nutrition_items_food_group", "nutrition_items", type_="foreignkey")
    op.drop_column("nutrition_items", "serving_basis")
    op.drop_column("nutrition_items", "food_group_id")
