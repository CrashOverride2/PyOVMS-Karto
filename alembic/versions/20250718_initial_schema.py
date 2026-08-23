"""Initial Schema

Revision ID: 20250718
Revises: 
Create Date: 2025-07-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geography

# revision identifiers, used by Alembic.
revision: str = '20250718'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Instruct Alembic not to run this migration inside a transaction.
# This is necessary for commands like CREATE EXTENSION.
disable_ddl_transaction = True


def upgrade() -> None:
    # Enable PostGIS extension
    op.execute('CREATE EXTENSION IF NOT EXISTS postgis;')
    
    # Create trips table
    op.create_table('trips',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('vehicle_id', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('start_soc', sa.Float(), nullable=True),
        sa.Column('end_soc', sa.Float(), nullable=True),
        sa.Column('soc_used', sa.Float(), nullable=True),
        sa.Column('distance_km', sa.Float(), nullable=True),
        sa.Column('average_speed_kph', sa.Float(), nullable=True),
        sa.Column('map_preview_path', sa.String(length=255), nullable=True),
        sa.Column('start_location', Geography(geometry_type='POINT', srid=4326, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
        sa.Column('end_location', Geography(geometry_type='POINT', srid=4326, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trips_vehicle_id'), 'trips', ['vehicle_id'], unique=False)

    # Create gps_points table
    op.create_table('gps_points',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('trip_id', sa.Uuid(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('location', Geography(geometry_type='POINT', srid=4326, spatial_index=True, from_text='ST_GeogFromText', name='geography'), nullable=False),
        sa.Column('speed_kph', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gps_points_timestamp'), 'gps_points', ['timestamp'], unique=False)
    op.create_index(op.f('ix_gps_points_trip_id'), 'gps_points', ['trip_id'], unique=False)


def downgrade() -> None:
    op.drop_table('gps_points')
    op.drop_table('trips')
    op.execute('DROP EXTENSION IF EXISTS postgis;')