"""ensure_timezone_aware_timestamps

Revision ID: 7c8c122629f0
Revises: 20250725
Create Date: 2025-10-04 23:59:14.903468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c8c122629f0'
down_revision: Union[str, None] = '20250725'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fix any existing naive timestamps in trips table by assuming they are UTC
    op.execute("""
        UPDATE trips
        SET start_time = start_time AT TIME ZONE 'UTC'
        WHERE start_time IS NOT NULL
        AND timezone('UTC', start_time) = start_time
    """)

    op.execute("""
        UPDATE trips
        SET end_time = end_time AT TIME ZONE 'UTC'
        WHERE end_time IS NOT NULL
        AND timezone('UTC', end_time) = end_time
    """)

    # Fix any existing naive timestamps in gps_points table
    op.execute("""
        UPDATE gps_points
        SET timestamp = timestamp AT TIME ZONE 'UTC'
        WHERE timestamp IS NOT NULL
        AND timezone('UTC', timestamp) = timestamp
    """)


def downgrade() -> None:
    pass