"""add meal_items.bbox

사진 확대 보기의 음식 이름 오버레이용 위치 스냅샷(정규화 좌표 JSON).
기존 기록은 NULL — 오버레이만 생략된다.

Revision ID: b6d31f9a4c72
Revises: a5c8e2d47b91
Create Date: 2026-07-27 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6d31f9a4c72'
down_revision: Union[str, None] = 'a5c8e2d47b91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('meal_items', sa.Column('bbox', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('meal_items', 'bbox')
