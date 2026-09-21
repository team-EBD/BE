"""add users is_test_device

Revision ID: b3e7d1a4c962
Revises: a61d8c9f3b27
Create Date: 2026-09-21 17:00:00.000000

앱이 로그인·가입 요청에 실어 보내는 '테스트 기기'(구글 플레이 사전 점검 로봇 등) 여부.
NULL = 보고한 적 없음(이 기능 이전 빌드) — 기존 행은 전부 NULL 로 남는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3e7d1a4c962"
down_revision: Union[str, None] = "a61d8c9f3b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_test_device", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "is_test_device")
