"""add recommendation_items

추천 항목별 행동 로그 (docs/음식군-DB-계약.md §2.5). recommendation_logs 의
recommended_items JSON 안에 담던 노출·채택·섭취를 행으로 옮겨 출처별 집계가 되게 한다.
과거 JSON 로그는 그대로 둔다.

Revision ID: e0d63e9a7b24
Revises: d9c52d8f6a13
Create Date: 2026-09-16 10:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e0d63e9a7b24"
down_revision: Union[str, None] = "d9c52d8f6a13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recommendation_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("food_group_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.Numeric(6, 4), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eaten_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eaten_meal_record_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["log_id"], ["recommendation_logs.id"], ondelete="CASCADE",
            name="fk_recommendation_items_log",
        ),
        sa.ForeignKeyConstraint(
            ["food_group_id"], ["food_groups.id"], ondelete="SET NULL",
            name="fk_recommendation_items_group",
        ),
        sa.ForeignKeyConstraint(
            ["eaten_meal_record_id"], ["meal_records.id"], ondelete="SET NULL",
            name="fk_recommendation_items_meal",
        ),
    )
    op.create_index("ix_recommendation_items_log_id", "recommendation_items", ["log_id"])
    op.create_index(
        "ix_recommendation_items_food_group_id", "recommendation_items", ["food_group_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_items_food_group_id", table_name="recommendation_items")
    op.drop_index("ix_recommendation_items_log_id", table_name="recommendation_items")
    op.drop_table("recommendation_items")
