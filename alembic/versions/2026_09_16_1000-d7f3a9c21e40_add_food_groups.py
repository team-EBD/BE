"""add food_groups, food_group_aliases

음식군 3층 구조의 2층 (docs/음식군-DB-계약.md §2.1·§2.2). 식약처 대표식품명 기준으로
군을 만들고 계열(family)·역할(role)·기본 동반·대표 영양값을 군에 1회 저장한다.
aliases 는 사용자 기록 이름·시드·동의어 → 군.

Revision ID: d7f3a9c21e40
Revises: d5a9f37c1e84  (dev 의 users.role 뒤에 선형으로 붙인다 — CI 의 downgrade -1 검증은 병합 리비전을 허용하지 않는다)
Create Date: 2026-09-16 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7f3a9c21e40"
down_revision: Union[str, None] = "d5a9f37c1e84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "food_groups",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("family", sa.String(length=30), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("companion_group_id", sa.BigInteger(), nullable=True),
        sa.Column("calories", sa.Numeric(8, 2), nullable=True),
        sa.Column("carbs", sa.Numeric(8, 2), nullable=True),
        sa.Column("protein", sa.Numeric(8, 2), nullable=True),
        sa.Column("fat", sa.Numeric(8, 2), nullable=True),
        sa.Column("base_amount", sa.Numeric(8, 2), nullable=True),
        sa.Column("base_unit", sa.String(length=20), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_names", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name", name="uq_food_groups_name"),
        sa.ForeignKeyConstraint(
            ["companion_group_id"], ["food_groups.id"], ondelete="SET NULL",
            name="fk_food_groups_companion",
        ),
    )
    op.create_index("ix_food_groups_family", "food_groups", ["family"])
    op.create_index("ix_food_groups_role", "food_groups", ["role"])

    op.create_table(
        "food_group_aliases",
        sa.Column("alias", sa.String(length=100), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id"], ["food_groups.id"], ondelete="CASCADE", name="fk_food_group_aliases_group"
        ),
    )
    op.create_index("ix_food_group_aliases_group_id", "food_group_aliases", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_food_group_aliases_group_id", table_name="food_group_aliases")
    op.drop_table("food_group_aliases")
    op.drop_index("ix_food_groups_role", table_name="food_groups")
    op.drop_index("ix_food_groups_family", table_name="food_groups")
    op.drop_table("food_groups")
