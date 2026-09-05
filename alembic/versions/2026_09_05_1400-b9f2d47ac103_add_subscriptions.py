"""add subscriptions

인앱 결제(자동 갱신 구독) 검증 결과 스냅샷.
(platform, purchase_key) 유일 — 같은 영수증이 여러 계정에 붙는 것을 막는다.

Revision ID: b9f2d47ac103
Revises: a3c7e19f42b8
Create Date: 2026-09-05 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9f2d47ac103"
down_revision: Union[str, None] = "a3c7e19f42b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.String(length=10), nullable=False),
        sa.Column("product_id", sa.String(length=100), nullable=False),
        sa.Column("purchase_key", sa.String(length=512), nullable=False),
        sa.Column("latest_order_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_auto_renewing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="production"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "purchase_key", name="uq_subscriptions_platform_key"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_expires_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
