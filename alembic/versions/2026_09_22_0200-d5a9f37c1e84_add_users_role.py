"""add users role

Revision ID: d5a9f37c1e84
Revises: b3e7d1a4c962
Create Date: 2026-09-22 02:00:00.000000

계정 역할 user/tester/admin (app/models/user.py UserRole). 기존 행은 전부 'user'.
tester·admin 은 AI 사용 한도를 받지 않고, 분석 대시보드 지표에서 제외된다.
지정은 서버 쪽 도구로만 한다 (pjt_eatlog/deploy_aws/set_user_roles.sh).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5a9f37c1e84"
down_revision: Union[str, None] = "b3e7d1a4c962"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(length=10), nullable=False, server_default="user"),
    )
    op.create_check_constraint("ck_users_role", "users", "role IN ('user', 'tester', 'admin')")


def downgrade() -> None:
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_column("users", "role")
