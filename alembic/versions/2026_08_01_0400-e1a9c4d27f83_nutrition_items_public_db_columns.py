"""nutrition_items 공공 영양DB 확장 컬럼

전국통합식품영양성분정보(식약처) 적재를 위한 컬럼 추가.
- 상세 영양성분: sugar/fiber/sodium/cholesterol/saturated_fat/trans_fat (시드 40건은 NULL)
- brand: 제조사/프랜차이즈명 (검색 대상)
- external_id: 공공DB 식품코드 — 적재 멱등키(UNIQUE)
- total_weight: 총 내용량 (per-100g 항목의 전체량 환산용)

Revision ID: e1a9c4d27f83
Revises: d4a7b8c31e56
Create Date: 2026-08-01 04:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1a9c4d27f83'
down_revision: Union[str, None] = 'd4a7b8c31e56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('nutrition_items', sa.Column('sugar', sa.Numeric(8, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('fiber', sa.Numeric(8, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('sodium', sa.Numeric(10, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('cholesterol', sa.Numeric(10, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('saturated_fat', sa.Numeric(8, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('trans_fat', sa.Numeric(8, 2), nullable=True))
    op.add_column('nutrition_items', sa.Column('brand', sa.String(100), nullable=True))
    op.add_column('nutrition_items', sa.Column('external_id', sa.String(40), nullable=True))
    op.add_column('nutrition_items', sa.Column('total_weight', sa.Numeric(10, 2), nullable=True))
    op.create_unique_constraint(
        'uq_nutrition_items_external_id', 'nutrition_items', ['external_id']
    )


def downgrade() -> None:
    op.drop_constraint('uq_nutrition_items_external_id', 'nutrition_items', type_='unique')
    for col in (
        'total_weight', 'external_id', 'brand', 'trans_fat', 'saturated_fat',
        'cholesterol', 'sodium', 'fiber', 'sugar',
    ):
        op.drop_column('nutrition_items', col)
