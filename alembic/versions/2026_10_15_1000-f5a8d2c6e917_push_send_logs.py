"""서버 푸시 발송 이력 (재기동·다중 인스턴스 중복 발송 방지)

Revision ID: f5a8d2c6e917
Revises: e3c7a9b2d804
Create Date: 2026-10-15 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "f5a8d2c6e917"
down_revision = "e3c7a9b2d804"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_send_logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("template_id", sa.String(40), nullable=False),
        sa.Column("logical_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "channel", "logical_date", name="uq_push_send_logs_user_channel_date"),
    )
    op.create_index("ix_push_send_logs_user_id", "push_send_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_push_send_logs_user_id", table_name="push_send_logs")
    op.drop_table("push_send_logs")
