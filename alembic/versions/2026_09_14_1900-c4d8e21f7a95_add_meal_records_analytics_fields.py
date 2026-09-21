"""add meal_records analytics fields

분석 로그(정답지)용 — 기록의 입력 방식(photo/text/search)과 초안을 만든
AI 호출 ID. 문장·직접 검색 기록은 이 컬럼으로만 구분되고, ai_call_log_id 로
food_candidates·client_events 와 조인한다.

Revision ID: c4d8e21f7a95
Revises: c4a1f7b28d95
Create Date: 2026-09-14 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4d8e21f7a95"
down_revision: Union[str, None] = "c4a1f7b28d95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "meal_records", sa.Column("entry_method", sa.String(length=10), nullable=True)
    )
    op.add_column(
        "meal_records", sa.Column("ai_call_log_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_meal_records_ai_call_log_id",
        "meal_records",
        "ai_call_logs",
        ["ai_call_log_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_meal_records_ai_call_log_id", "meal_records", ["ai_call_log_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_meal_records_ai_call_log_id", table_name="meal_records")
    op.drop_constraint("fk_meal_records_ai_call_log_id", "meal_records", type_="foreignkey")
    op.drop_column("meal_records", "ai_call_log_id")
    op.drop_column("meal_records", "entry_method")
