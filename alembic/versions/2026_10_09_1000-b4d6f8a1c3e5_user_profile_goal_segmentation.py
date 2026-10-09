"""user_profiles 에 목표 세분화 컬럼 (목표·활동량·목표 체중·속도·챙기고 싶은 것)

Revision ID: b4d6f8a1c3e5
Revises: a2b3c4d5e6f7
Create Date: 2026-10-09 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d6f8a1c3e5"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 전부 NULL 허용 — 기존 사용자는 예전 방식(meal_goal + 가벼운 활동 가정)으로 계속 계산된다
    op.add_column("user_profiles", sa.Column("primary_goal", sa.String(20), nullable=True))
    op.add_column("user_profiles", sa.Column("activity_level", sa.String(20), nullable=True))
    op.add_column("user_profiles", sa.Column("target_weight", sa.Numeric(5, 2), nullable=True))
    op.add_column("user_profiles", sa.Column("goal_pace", sa.String(10), nullable=True))
    op.add_column("user_profiles", sa.Column("focus_areas", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("user_profiles", "focus_areas")
    op.drop_column("user_profiles", "goal_pace")
    op.drop_column("user_profiles", "target_weight")
    op.drop_column("user_profiles", "activity_level")
    op.drop_column("user_profiles", "primary_goal")
