"""식사 사진 최대 3장 연결과 분석 입력 스냅샷

Revision ID: e3c7a9b2d804
Revises: d9b2f6a4c713
Create Date: 2026-10-12 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "e3c7a9b2d804"
down_revision = "d9b2f6a4c713"
branch_labels = None
depends_on = None


def backfill() -> None:
    """대표 사진을 멱등하게 연결한다. 삭제된 기록과 사진이 사라진 호출도 포함한다."""
    op.execute(sa.text("""
        INSERT INTO meal_record_images (meal_record_id, meal_image_id, position)
        SELECT id, meal_image_id, 0 FROM meal_records r
        WHERE meal_image_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM meal_record_images i WHERE i.meal_record_id = r.id)
    """))
    photo_calls = "task_type IN ('analyze', 'analyze_clarifier') AND (input_type = 'photo' OR meal_image_id IS NOT NULL)"
    op.execute(sa.text(f"""
        UPDATE ai_call_logs SET input_image_count = 1
        WHERE {photo_calls} AND input_image_count IS NULL
    """))
    op.execute(sa.text(f"""
        INSERT INTO ai_call_log_images (ai_call_log_id, meal_image_id, position, status)
        SELECT id, meal_image_id, 0, 'ok' FROM ai_call_logs l
        WHERE {photo_calls}
          AND NOT EXISTS (SELECT 1 FROM ai_call_log_images i WHERE i.ai_call_log_id = l.id)
    """))


def upgrade() -> None:
    op.create_table(
        "meal_record_images",
        sa.Column("meal_record_id", sa.BigInteger(), sa.ForeignKey("meal_records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("meal_image_id", sa.BigInteger(), sa.ForeignKey("meal_images.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.UniqueConstraint("meal_record_id", "position", name="uq_meal_record_images_position"),
        sa.CheckConstraint("position BETWEEN 0 AND 2", name="ck_meal_record_images_position"),
    )
    op.create_index("ix_meal_record_images_meal_image_id", "meal_record_images", ["meal_image_id"])
    op.create_table(
        "ai_call_log_images",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("ai_call_log_id", sa.BigInteger(), sa.ForeignKey("ai_call_logs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("meal_image_id", sa.BigInteger(), sa.ForeignKey("meal_images.id", ondelete="SET NULL"), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("fail_reason", sa.String(30), nullable=True),
        sa.UniqueConstraint("ai_call_log_id", "position", name="uq_ai_call_log_images_position"),
        sa.CheckConstraint("position BETWEEN 0 AND 2", name="ck_ai_call_log_images_position"),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="ck_ai_call_log_images_status"),
    )
    op.create_index("ix_ai_call_log_images_ai_call_log_id", "ai_call_log_images", ["ai_call_log_id"])
    op.add_column("ai_call_logs", sa.Column("input_image_count", sa.SmallInteger(), nullable=True))
    with op.batch_alter_table("meal_items") as batch:
        batch.add_column(sa.Column("bbox_image_id", sa.BigInteger(), nullable=True))
        batch.create_foreign_key("meal_items_bbox_image_id_fkey", "meal_images", ["bbox_image_id"], ["id"], ondelete="SET NULL")
    backfill()


def downgrade() -> None:
    with op.batch_alter_table("meal_items") as batch:
        batch.drop_constraint("meal_items_bbox_image_id_fkey", type_="foreignkey")
        batch.drop_column("bbox_image_id")
    with op.batch_alter_table("ai_call_logs") as batch:
        batch.drop_column("input_image_count")
    op.drop_table("ai_call_log_images")
    op.drop_table("meal_record_images")
