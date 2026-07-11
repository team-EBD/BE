"""add users password_hash

이메일 가입 사용자용 비밀번호 해시 컬럼 (소셜 전용 계정은 NULL).

Revision ID: d41f7b2a9c31
Revises: 8c7c0c95b0ce
Create Date: 2026-07-11 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd41f7b2a9c31'
down_revision: Union[str, None] = '8c7c0c95b0ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'password_hash')
