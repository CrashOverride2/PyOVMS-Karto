"""add energy_used_kwh to trips

Revision ID: 20260507
Revises: 7c8c122629f0
Create Date: 2026-05-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260507'
down_revision: Union[str, None] = '7c8c122629f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trips', sa.Column('energy_used_kwh', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('trips', 'energy_used_kwh')
