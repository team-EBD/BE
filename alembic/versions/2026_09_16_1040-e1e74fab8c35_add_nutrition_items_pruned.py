"""add nutrition_items_pruned

삭제한 nutrition_items 행의 아카이브 (docs/음식군-DB-계약.md §2.6·§6). 순수 중복
8,581행 등을 지우기 전에 행 전체를 JSON 으로 보존한다 — 되돌리기용.
survivor_id 는 참조(meal_items·food_candidates·favorite_foods)를 넘긴 대표 행.

Revision ID: e1e74fab8c35
Revises: e0d63e9a7b24
Create Date: 2026-09-16 10:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1e74fab8c35"
down_revision: Union[str, None] = "e0d63e9a7b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nutrition_items_pruned",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("original_id", sa.BigInteger(), nullable=False),
        sa.Column("survivor_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "pruned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_nutrition_items_pruned_original_id", "nutrition_items_pruned", ["original_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_nutrition_items_pruned_original_id", table_name="nutrition_items_pruned")
    op.drop_table("nutrition_items_pruned")
