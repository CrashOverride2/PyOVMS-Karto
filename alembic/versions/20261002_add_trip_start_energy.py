"""add start_energy_kwh to trips

Revision ID: 20261002
Revises: 20260901
Create Date: 2026-10-02 12:00:00.000000

The energy counter reading at the start of a trip used to live in memory only, so a
trip that survived a Karto restart lost it and finished without an energy figure — or
with the whole session counter, read as if it had been reset at the start. Stored on
the row, the trip can be resumed with its baseline intact.

It also gives `trips` a partial unique index: at most one `in_progress` row per vehicle.
The tracker keeps to that with a lock per vehicle, which holds inside one process only.
Before trips were resumed after a restart, a restart mid-ride left the old row open and
started a second one, so an existing database can hold several; all but the newest per
vehicle are finalized first, or the index could not be created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261002'
down_revision: Union[str, None] = '20260901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _finalize_surplus_open_trips() -> None:
    """
    Finalizes every open trip but the newest of its vehicle, the way the tracker ends a
    trip found too late: at its last point, without end SoC or energy — nothing recorded
    says what they were. A trip without points, or shorter than the minimum distance, is
    deleted, as the tracker's completion would do; the others are queued for their map.
    """
    from app.config import settings

    op.execute("""
        CREATE TEMPORARY TABLE surplus_open_trips AS
        SELECT id FROM (
            SELECT id, row_number() OVER (
                PARTITION BY vehicle_id ORDER BY start_time DESC, id DESC) AS newest_first
            FROM trips
            WHERE status = 'in_progress'
        ) ranked
        WHERE newest_first > 1
    """)
    # The line is aggregated in time order inside the aggregate; ST_MakeLine over a
    # single point is no line, so such a track measures 0.
    op.execute("""
        UPDATE trips AS t SET
            status = 'completed',
            end_time = p.last_ts,
            end_location = p.last_location,
            duration_seconds = GREATEST(0, EXTRACT(EPOCH FROM (p.last_ts - t.start_time)))::integer,
            distance_km = p.distance_km,
            average_speed_kph = CASE
                WHEN p.last_ts > t.start_time
                THEN p.distance_km / EXTRACT(EPOCH FROM (p.last_ts - t.start_time)) * 3600
                ELSE 0 END
        FROM (
            SELECT trip_id,
                   max("timestamp") AS last_ts,
                   (array_agg(location ORDER BY "timestamp" DESC))[1] AS last_location,
                   CASE WHEN count(*) >= 2
                        THEN ST_Length(ST_MakeLine(location::geometry ORDER BY "timestamp")::geography) / 1000
                        ELSE 0 END AS distance_km
            FROM gps_points
            WHERE trip_id IN (SELECT id FROM surplus_open_trips)
            GROUP BY trip_id
        ) AS p
        WHERE t.id = p.trip_id
    """)
    # Points go with their trip (ON DELETE CASCADE); a queue entry has no foreign key.
    discard = """
        SELECT id FROM trips
        WHERE id IN (SELECT id FROM surplus_open_trips)
          AND (status = 'in_progress' OR distance_km < :min_distance_km)
    """
    for statement in (f"DELETE FROM map_regeneration_queue WHERE trip_id IN ({discard})",
                      f"DELETE FROM trips WHERE id IN ({discard})"):
        op.execute(sa.text(statement).bindparams(min_distance_km=settings.KARTO_TRIP_MIN_DISTANCE_KM))
    op.execute("""
        INSERT INTO map_regeneration_queue (trip_id, status)
        SELECT id, 'pending' FROM trips WHERE id IN (SELECT id FROM surplus_open_trips)
        ON CONFLICT (trip_id) DO UPDATE SET status = 'pending', updated_at = now()
    """)
    op.execute("DROP TABLE surplus_open_trips")


def upgrade() -> None:
    op.add_column('trips', sa.Column('start_energy_kwh', sa.Float(), nullable=True))
    # The open-trip lookups filter on the vehicle and the status together.
    op.create_index('ix_trips_vehicle_status', 'trips', ['vehicle_id', 'status'])

    _finalize_surplus_open_trips()
    op.create_index('uq_trips_vehicle_in_progress', 'trips', ['vehicle_id'], unique=True,
                    postgresql_where=sa.text("status = 'in_progress'"))


def downgrade() -> None:
    # The trips finalized on the way up stay finalized: which of them were open is not
    # recorded anywhere, and an open trip nobody tracks is only work for the reaper.
    op.drop_index('uq_trips_vehicle_in_progress', table_name='trips')
    op.drop_index('ix_trips_vehicle_status', table_name='trips')
    op.drop_column('trips', 'start_energy_kwh')
