"""add game missions and events

게이미피케이션 미션·이벤트 도메인 4개 테이블.
기존 테이블은 전혀 건드리지 않는 추가 전용 마이그레이션이다.
보상 지급은 기존 reward_ledger 를 그대로 쓰므로 새 원장 테이블은 만들지 않는다.

Revision ID: e7a3c95d18b4
Revises: c4d8e21f7a95
Create Date: 2026-09-18 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7a3c95d18b4"
down_revision: Union[str, None] = "c4d8e21f7a95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "game_missions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rule", sa.String(length=30), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("target_value", sa.Integer(), nullable=False),
        sa.Column("reward_xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reward_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_ts_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_game_missions_code"),
    )
    op.create_index("ix_game_missions_scope", "game_missions", ["scope"])

    op.create_table(
        "user_missions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("mission_code", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_key", sa.String(length=10), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_value", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "mission_code", "period_key", name="uq_user_missions_user_code_period"
        ),
    )
    op.create_index("ix_user_missions_user_id", "user_missions", ["user_id"])
    op.create_index("ix_user_missions_period_key", "user_missions", ["period_key"])

    op.create_table(
        "game_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("rule", sa.String(length=20), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("rewards", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_ts_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_game_events_code"),
    )

    op.create_table(
        "user_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_code", sa.String(length=40), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_counted_logical_date", sa.Date(), nullable=True),
        sa.Column("claimed_thresholds", sa.JSON(), nullable=False),
        *_ts_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "event_code", name="uq_user_events_user_event"),
    )
    op.create_index("ix_user_events_user_id", "user_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_events_user_id", table_name="user_events")
    op.drop_table("user_events")
    op.drop_table("game_events")
    op.drop_index("ix_user_missions_period_key", table_name="user_missions")
    op.drop_index("ix_user_missions_user_id", table_name="user_missions")
    op.drop_table("user_missions")
    op.drop_index("ix_game_missions_scope", table_name="game_missions")
    op.drop_table("game_missions")
