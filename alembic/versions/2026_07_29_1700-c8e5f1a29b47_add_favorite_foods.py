"""add favorite_foods

즐겨찾기 음식 — 영양값 스냅샷을 함께 저장해 영양 DB 에 없는(매칭 실패한)
음식도 즐겨찾기할 수 있게 한다. 사용자당 음식명 유일.

Revision ID: c8e5f1a29b47
Revises: b7d4e9f2a1c3
Create Date: 2026-07-29 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e5f1a29b47'
down_revision: Union[str, None] = 'b7d4e9f2a1c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'favorite_foods',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('nutrition_item_id', sa.BigInteger(), nullable=True),
        sa.Column('food_name', sa.String(length=100), nullable=False),
        sa.Column('base_serving', sa.String(length=50), nullable=False),
        sa.Column('calories', sa.Numeric(8, 2), nullable=False),
        sa.Column('carbs', sa.Numeric(8, 2), nullable=False),
        sa.Column('protein', sa.Numeric(8, 2), nullable=False),
        sa.Column('fat', sa.Numeric(8, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['nutrition_item_id'], ['nutrition_items.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'food_name', name='uq_favorite_foods_user_food'),
    )
    op.create_index('ix_favorite_foods_user_id', 'favorite_foods', ['user_id'])


def downgrade() -> None:
    op.drop_table('favorite_foods')
