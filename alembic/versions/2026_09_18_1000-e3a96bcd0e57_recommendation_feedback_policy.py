"""Separate visible impressions and preserve recommendation policy decisions.

Revision ID: e3a96bcd0e57
Revises: e2f85abc9d46
"""
from alembic import op
import sqlalchemy as sa

revision = "e3a96bcd0e57"
down_revision = "e2f85abc9d46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recommendation_logs", sa.Column("decision", sa.JSON(), nullable=True))
    op.add_column("recommendation_items", sa.Column("features", sa.JSON(), nullable=True))
    op.add_column("recommendation_items", sa.Column("selection_probability", sa.Float(), nullable=True))
    op.add_column("recommendation_items", sa.Column("policy_version", sa.String(80), nullable=True))
    op.add_column("recommendation_items", sa.Column("shown_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recommendation_items", sa.Column("reject_reason", sa.String(20), nullable=True))
    op.create_index("ix_recommendation_items_shown_at", "recommendation_items", ["shown_at"])
    op.create_index("ix_recommendation_items_eaten_meal_record_id", "recommendation_items", ["eaten_meal_record_id"])
    op.alter_column("recommendation_items", "source", existing_type=sa.String(12), type_=sa.String(20))
    # 과거 생성 시각을 실제 노출로 소급하지 않는다. 구기록의 정책·노출은 NULL 유지.


def downgrade() -> None:
    # collaborative 는 13자여서 기존 12자 컬럼으로 축소하지 않는다 (데이터 보존).
    op.drop_index("ix_recommendation_items_shown_at", table_name="recommendation_items")
    op.drop_index("ix_recommendation_items_eaten_meal_record_id", table_name="recommendation_items")
    for column in ("reject_reason", "shown_at", "policy_version", "selection_probability", "features"):
        op.drop_column("recommendation_items", column)
    op.drop_column("recommendation_logs", "decision")
