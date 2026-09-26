"""add users tutorial_completed_at

Revision ID: f4b8c2e91a06
Revises: e7a3c95d18b4
Create Date: 2026-09-20 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4b8c2e91a06"
down_revision: Union[str, None] = "e7a3c95d18b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tutorial_completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "tutorial_completed_at")
