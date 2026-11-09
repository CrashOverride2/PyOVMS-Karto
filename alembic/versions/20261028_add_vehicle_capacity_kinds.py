"""add vehicle_capacity_kinds

Revision ID: 20261028
Revises: 20261002
Create Date: 2026-10-28 12:00:00.000000

Keeps what was learned about a vehicle's v.b.capacity (app/trip_energy.py) across restarts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261028'
down_revision: Union[str, None] = '20261002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'vehicle_capacity_kinds',
        sa.Column('vehicle_id', sa.String(length=32), primary_key=True),
        sa.Column('capacity_kind', sa.String(length=16), nullable=True),
        sa.Column('reference_soc', sa.Float(), nullable=True),
        sa.Column('reference_kwh', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('vehicle_capacity_kinds')
