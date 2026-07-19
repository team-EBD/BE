"""add users nickname_tag

닉네임 중복 허용용 식별 태그(표시형식 "닉네임#0001").
(닉네임, 태그) 쌍을 유일하게 유지해 닉네임 선점(스쿼팅)이 성립하지 않게 한다.

Revision ID: c3a9d1e7f842
Revises: b7e4f8a2c519
Create Date: 2026-07-19 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a9d1e7f842'
down_revision: Union[str, None] = 'b7e4f8a2c519'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('nickname_tag', sa.String(length=4), nullable=False, server_default='0000'),
    )
    # 기존 사용자 백필: id 기반 의사난수(7919는 9999와 서로소 → id 1~9999 구간에서 태그가
    # 겹치지 않음). 같은 닉네임끼리도 서로 다른 태그를 갖게 된다.
    op.execute("UPDATE users SET nickname_tag = lpad(((id * 7919) % 9999 + 1)::text, 4, '0')")
    op.create_unique_constraint('uq_users_nickname_tag', 'users', ['nickname', 'nickname_tag'])


def downgrade() -> None:
    op.drop_constraint('uq_users_nickname_tag', 'users', type_='unique')
    op.drop_column('users', 'nickname_tag')
