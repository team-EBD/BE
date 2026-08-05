"""nutrition_items.macros_estimated — 탄단지 추정 행 추적 플래그

탄수+지방 결측을 적재 시 추정으로 채우는데(2026-08-05 실측 앵커식으로 교체),
어떤 행이 실측이고 어떤 행이 추정인지 DB 안에서 구분할 수 없던 문제(PM 지적)를
해결한다. 값은 재적재 파이프라인이 채운다.

Revision ID: a3c7e19f42b8
Revises: f2b8d5e91a04
Create Date: 2026-08-05 18:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c7e19f42b8"
down_revision = "f2b8d5e91a04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nutrition_items",
        sa.Column(
            "macros_estimated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("nutrition_items", "macros_estimated")
