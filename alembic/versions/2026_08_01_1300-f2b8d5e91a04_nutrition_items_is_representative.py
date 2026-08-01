"""nutrition_items.is_representative 추가

대표 음식(1인분 기준) 플래그. 검색 최상위 노출과 AI 분석 매칭 대상을 표시한다.
시드 40건 + 공공 음식편(급식·가정식)에서 큐레이션한 대표 항목이 true.

Revision ID: f2b8d5e91a04
Revises: e1a9c4d27f83
Create Date: 2026-08-01 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2b8d5e91a04'
down_revision: Union[str, None] = 'e1a9c4d27f83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nutrition_items',
        sa.Column(
            'is_representative',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('nutrition_items', 'is_representative')
