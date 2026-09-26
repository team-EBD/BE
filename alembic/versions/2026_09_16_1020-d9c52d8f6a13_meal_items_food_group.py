"""meal_items: food_group_id

사용자 기록의 군 스냅샷 (docs/음식군-DB-계약.md §2.4). 저장 시 채우고 과거 기록은
backfill 한다 (정확일치·alias 만, 어미 추정은 하지 않는다). NULL = 미분류 → 개인 빈도
집계는 이름 키로 폴백.

Revision ID: d9c52d8f6a13
Revises: d8b41c7e5f02
Create Date: 2026-09-16 10:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d9c52d8f6a13"
down_revision: Union[str, None] = "d8b41c7e5f02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meal_items", sa.Column("food_group_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_meal_items_food_group",
        "meal_items",
        "food_groups",
        ["food_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_meal_items_food_group_id", "meal_items", ["food_group_id"])


def downgrade() -> None:
    op.drop_index("ix_meal_items_food_group_id", table_name="meal_items")
    op.drop_constraint("fk_meal_items_food_group", "meal_items", type_="foreignkey")
    op.drop_column("meal_items", "food_group_id")
