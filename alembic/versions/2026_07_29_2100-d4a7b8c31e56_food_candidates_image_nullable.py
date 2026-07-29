"""food_candidates.meal_image_id nullable — 자연어 파싱 후보는 이미지가 없다

Revision ID: d4a7b8c31e56
Revises: c8e5f1a29b47
Create Date: 2026-07-29 21:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4a7b8c31e56"
down_revision = "c8e5f1a29b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "food_candidates",
        "meal_image_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    # NULL 행(텍스트 파싱 후보)이 있으면 NOT NULL 복원이 실패하므로 먼저 제거
    op.execute("DELETE FROM food_candidates WHERE meal_image_id IS NULL")
    op.alter_column(
        "food_candidates",
        "meal_image_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
