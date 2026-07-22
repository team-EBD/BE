"""add user_profiles goal_source

목표 칼로리 출처 구분: auto(BMR/TDEE 자동 산정) / manual(사용자 직접 설정).
manual 로 설정된 목표는 신체정보(키/몸무게)나 식습관 목표가 바뀌어도
자동 재계산으로 덮어쓰지 않는다.

Revision ID: 9f3b2c1d0e8a
Revises: c3a9d1e7f842
Create Date: 2026-07-22 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f3b2c1d0e8a'
down_revision: Union[str, None] = 'c3a9d1e7f842'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_profiles',
        sa.Column('goal_source', sa.String(length=10), nullable=False, server_default='auto'),
    )


def downgrade() -> None:
    op.drop_column('user_profiles', 'goal_source')
