"""add ai_call_logs is_tutorial

Revision ID: a7c4e9d2b518
Revises: f1a2b3c4d5e6
Create Date: 2026-10-05 10:00:00.000000

튜토리얼(첫 기록 연습) 중에 일어난 AI 분석 호출 표시. 앱이 /meals/analyze·/meals/parse-text
요청에 실어 보낸 값을 그대로 저장한다. 기존 행과 이 값을 보내지 않는 구버전 앱의 호출은
NULL(알 수 없음)로 남긴다 — false 로 채우면 '튜토리얼 아님'으로 잘못 확정된다.
분석 대시보드가 채택률에서 연습 호출을 가려내는 데 쓴다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c4e9d2b518"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_call_logs", sa.Column("is_tutorial", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_call_logs", "is_tutorial")
