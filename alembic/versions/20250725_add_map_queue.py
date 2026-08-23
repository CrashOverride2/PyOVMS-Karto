"""Add map_regeneration_queue table

Revision ID: 20250725
Revises: 20250722
Create Date: 2025-07-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20250725'
down_revision: Union[str, None] = '20250722'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('map_regeneration_queue',
        sa.Column('trip_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('trip_id')
    )
    op.create_index(op.f('ix_map_regeneration_queue_status'), 'map_regeneration_queue', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('map_regeneration_queue')

