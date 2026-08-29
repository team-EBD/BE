"""add telemetry tables

- ai_call_logs.total_ms: BE 요청 처리 전체 시간 (AI 내부 latency_ms 와 분해용)
- request_logs: 전 API 요청 처리 시간 (타이밍 미들웨어)
- client_events: FE 측정 구간 시간·행동 이벤트 (ai_call_log_id 로 서버 기록과 조인)

Revision ID: b7d4e9f2a1c3
Revises: b6d31f9a4c72
Create Date: 2026-07-28 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d4e9f2a1c3'
down_revision: Union[str, None] = 'b6d31f9a4c72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_call_logs', sa.Column('total_ms', sa.Integer(), nullable=True))

    op.create_table(
        'request_logs',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('method', sa.String(length=8), nullable=False),
        sa.Column('path', sa.String(length=200), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_request_logs_path', 'request_logs', ['path'])
    op.create_index('ix_request_logs_created_at', 'request_logs', ['created_at'])

    op.create_table(
        'client_events',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('ai_call_log_id', sa.BigInteger(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['ai_call_log_id'], ['ai_call_logs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_client_events_user_id', 'client_events', ['user_id'])
    op.create_index('ix_client_events_event_type', 'client_events', ['event_type'])
    op.create_index('ix_client_events_created_at', 'client_events', ['created_at'])


def downgrade() -> None:
    op.drop_table('client_events')
    op.drop_table('request_logs')
    op.drop_column('ai_call_logs', 'total_ms')
