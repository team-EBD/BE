"""add refund guarantee claims

Revision ID: a61d8c9f3b27
Revises: f4b8c2e91a06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a61d8c9f3b27"
down_revision: Union[str, None] = "f4b8c2e91a06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refund_guarantee_claims",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "period_start", name="uq_refund_guarantee_claim_period"),
    )
    op.create_index("ix_refund_guarantee_claims_user_id", "refund_guarantee_claims", ["user_id"])
    op.create_index("ix_meal_records_user_eaten_at", "meal_records", ["user_id", "eaten_at"])


def downgrade() -> None:
    op.drop_index("ix_meal_records_user_eaten_at", table_name="meal_records")
    op.drop_index("ix_refund_guarantee_claims_user_id", table_name="refund_guarantee_claims")
    op.drop_table("refund_guarantee_claims")
