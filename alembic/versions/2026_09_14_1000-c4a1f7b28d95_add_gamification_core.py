"""add gamification core

게이미피케이션 v3(함께 크는 펫) 코어 9개 테이블.
기존 테이블은 전혀 건드리지 않는 추가 전용 마이그레이션이다.

Revision ID: c4a1f7b28d95
Revises: b9f2d47ac103
Create Date: 2026-09-14 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a1f7b28d95"
down_revision: Union[str, None] = "b9f2d47ac103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "game_profiles",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("best_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_recorded_logical_date", sa.Date(), nullable=True),
        sa.Column("total_record_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_pet_code", sa.String(length=40), nullable=True),
        sa.Column("equipped_skill_code", sa.String(length=40), nullable=True),
        sa.Column("first_friend_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage_revision", sa.Integer(), nullable=False, server_default="0"),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_game_profiles_user"),
    )

    op.create_table(
        "catalog_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("asset_key", sa.String(length=60), nullable=False),
        sa.Column("preview_key", sa.String(length=60), nullable=False),
        sa.Column("unlock", sa.JSON(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_ts_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_catalog_items_code"),
    )
    op.create_index("ix_catalog_items_category", "catalog_items", ["category"])

    op.create_table(
        "user_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["catalog_item_id"], ["catalog_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "catalog_item_id", name="uq_user_items_user_item"),
    )
    op.create_index("ix_user_items_user_id", "user_items", ["user_id"])

    op.create_table(
        "stage_placements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_type", sa.String(length=20), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("transform", sa.JSON(), nullable=True),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["catalog_item_id"], ["catalog_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slot_type", "slot_index", name="uq_stage_placements_slot"),
    )
    op.create_index("ix_stage_placements_user_id", "stage_placements", ["user_id"])

    op.create_table(
        "unlock_progress",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("current_value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_value", sa.Integer(), nullable=False),
        sa.Column("last_counted_logical_date", sa.Date(), nullable=True),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["catalog_item_id"], ["catalog_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "catalog_item_id", name="uq_unlock_progress_user_item"),
    )
    op.create_index("ix_unlock_progress_user_id", "unlock_progress", ["user_id"])

    op.create_table(
        "reward_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("xp_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("ref_type", sa.String(length=20), nullable=True),
        sa.Column("ref_id", sa.String(length=64), nullable=True),
        sa.Column("logical_date", sa.Date(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_reward_ledger_user_key"),
    )
    op.create_index("ix_reward_ledger_user_id", "reward_ledger", ["user_id"])
    # 일일 지급 상한(하루 4건) 조회용
    op.create_index(
        "ix_reward_ledger_user_reason_date",
        "reward_ledger",
        ["user_id", "reason", "logical_date"],
    )

    op.create_table(
        "pet_bonds",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("pet_code", sa.String(length=40), nullable=False),
        sa.Column("nickname", sa.String(length=20), nullable=True),
        sa.Column("bond_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bond_level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_counted_logical_date", sa.Date(), nullable=True),
        sa.Column("selected_growth_style", sa.String(length=40), nullable=True),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "pet_code", name="uq_pet_bonds_user_pet"),
    )
    op.create_index("ix_pet_bonds_user_id", "pet_bonds", ["user_id"])

    op.create_table(
        "user_skills",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("skill_code", sa.String(length=40), nullable=False),
        sa.Column("source_pet_code", sa.String(length=40), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("charge_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recharge_at_record_days", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "skill_code", name="uq_user_skills_user_skill"),
    )
    op.create_index("ix_user_skills_user_id", "user_skills", ["user_id"])

    op.create_table(
        "skill_usage_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("skill_code", sa.String(length=40), nullable=False),
        sa.Column("logical_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("ref_type", sa.String(length=20), nullable=True),
        sa.Column("ref_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_skill_usage_user_key"),
    )
    op.create_index("ix_skill_usage_ledger_user_id", "skill_usage_ledger", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_usage_ledger_user_id", table_name="skill_usage_ledger")
    op.drop_table("skill_usage_ledger")
    op.drop_index("ix_user_skills_user_id", table_name="user_skills")
    op.drop_table("user_skills")
    op.drop_index("ix_pet_bonds_user_id", table_name="pet_bonds")
    op.drop_table("pet_bonds")
    op.drop_index("ix_reward_ledger_user_reason_date", table_name="reward_ledger")
    op.drop_index("ix_reward_ledger_user_id", table_name="reward_ledger")
    op.drop_table("reward_ledger")
    op.drop_index("ix_unlock_progress_user_id", table_name="unlock_progress")
    op.drop_table("unlock_progress")
    op.drop_index("ix_stage_placements_user_id", table_name="stage_placements")
    op.drop_table("stage_placements")
    op.drop_index("ix_user_items_user_id", table_name="user_items")
    op.drop_table("user_items")
    op.drop_index("ix_catalog_items_category", table_name="catalog_items")
    op.drop_table("catalog_items")
    op.drop_table("game_profiles")
