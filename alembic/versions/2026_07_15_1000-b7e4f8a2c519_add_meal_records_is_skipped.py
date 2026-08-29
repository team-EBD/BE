"""add meal_records is_skipped

식사 생략(안 먹음) 기록 지원 — items 없이 저장되는 레코드를 구분한다.

Revision ID: b7e4f8a2c519
Revises: d41f7b2a9c31
Create Date: 2026-07-15 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4f8a2c519'
down_revision: Union[str, None] = 'd41f7b2a9c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'meal_records',
        sa.Column('is_skipped', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('meal_records', 'is_skipped')
