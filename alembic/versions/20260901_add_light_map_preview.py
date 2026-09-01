"""add map_preview_path_light to trips

Revision ID: 20260901
Revises: 20260508
Create Date: 2026-09-01 12:00:00.000000

The map worker renders a second, light-themed preview alongside the dark one so the
app can show whichever matches its current brightness. A dedicated column rather than
a filename convention: trips rendered before this change have no light image, and the
client has to be able to tell that apart from "not rendered yet" to fall back cleanly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260901'
down_revision: Union[str, None] = '20260508'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trips', sa.Column('map_preview_path_light', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('trips', 'map_preview_path_light')
