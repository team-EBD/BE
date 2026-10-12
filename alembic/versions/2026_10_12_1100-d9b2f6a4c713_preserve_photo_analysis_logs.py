"""사진 삭제 후 후보 보존 및 호출 입력 종류 스냅샷

Revision ID: d9b2f6a4c713
Revises: c8a1e5f3b602
Create Date: 2026-10-12 11:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "d9b2f6a4c713"
down_revision = "c8a1e5f3b602"
branch_labels = None
depends_on = None

# 초기 마이그레이션의 무명 FK 에 Postgres 가 부여하는 이름.
# SQLite batch 모드에서도 같은 이름으로 무명 제약을 찾아 교체한다.
_FK_NAME = "food_candidates_meal_image_id_fkey"
_NAMING = {"fk": "%(table_name)s_%(column_0_name)s_fkey"}


def _replace_image_fk(ondelete: str) -> None:
    with op.batch_alter_table("food_candidates", naming_convention=_NAMING) as batch:
        batch.drop_constraint(_FK_NAME, type_="foreignkey")
        batch.create_foreign_key(
            _FK_NAME, "meal_images", ["meal_image_id"], ["id"], ondelete=ondelete,
        )


def upgrade() -> None:
    op.add_column("ai_call_logs", sa.Column("input_type", sa.String(10), nullable=True))
    # FK 가 남은 호출만 사진으로 확정할 수 있다. NULL 은 문장/삭제된 사진을 구분 못한다.
    op.execute(sa.text("UPDATE ai_call_logs SET input_type = 'photo' WHERE meal_image_id IS NOT NULL"))
    _replace_image_fk("SET NULL")


def downgrade() -> None:
    _replace_image_fk("CASCADE")
    op.drop_column("ai_call_logs", "input_type")
