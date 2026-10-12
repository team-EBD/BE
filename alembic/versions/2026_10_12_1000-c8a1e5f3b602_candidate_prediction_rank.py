"""음식 그룹 내 후보 순위를 별도 저장

Revision ID: c8a1e5f3b602
Revises: b4d6f8a1c3e5
Create Date: 2026-10-12 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "c8a1e5f3b602"
down_revision = "b4d6f8a1c3e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # food_index 는 a2b3c4d5e6f7 에서 이미 추가됐다. 과거 순위는 백필하지 않는다.
    op.add_column("food_candidates", sa.Column("prediction_rank", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("food_candidates", "prediction_rank")
