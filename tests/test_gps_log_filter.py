"""
Speed/distance filter for GPS log records (notify/data/...).

The smart EQ sends a XSQ-GPS-Log record whenever any of its values changed - battery
power and current included - so a car waiting at a traffic light keeps producing records
at the same position. The live flow drops such points (KARTO_GPS_MIN_SPEED_KPH /
KARTO_GPS_MIN_DISTANCE_METERS); log records get the same rule, decided in memory before
any database session is opened.

No database: SessionLocal hands out mocks and the crud calls are replaced by fakes that
record what would have been written. `db.stored` holds the timestamps of the inserted
points, `db.sessions` the sessions that were opened at all.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app import trip_tracker
from app.config import settings
from app.trip_tracker import TripTrackerService


VEHICLE = "TESTSMART"

# Roughly 1 m in latitude
M = 1 / 111_320


def xsq(lat: float, lon: float, speed: float, gpslock: int = 1, fix_age: int = 0) -> str:
    return (f"XSQ-GPS-Log,123456,86400,{lat:.6f},{lon:.6f},112,87,{speed:.0f},{gpslock},"
            f"{fix_age},-71,1.5,0.1,0.0,4.2")


@pytest.fixture
def db(monkeypatch):
    """An in-progress trip that takes every record; flip `trip` to None for orphans."""
    fake = SimpleNamespace(
        sessions=[],
        stored=[],
        trip=SimpleNamespace(id=uuid4(), start_time=datetime(2020, 1, 1, tzinfo=timezone.utc)),
        fail=False,
    )

    def session_local():
        session = MagicMock()
        fake.sessions.append(session)
        return session

    def add_gps_point(session, trip_id, ts, wkt, speed, altitude):
        if fake.fail:
            raise RuntimeError("database gone")
        fake.stored.append(ts)

    monkeypatch.setattr(trip_tracker.database, "SessionLocal", session_local)
    monkeypatch.setattr(trip_tracker.crud, "get_in_progress_trip_by_vehicle", lambda session, vid: fake.trip)
    monkeypatch.setattr(trip_tracker.crud, "get_completed_trip_covering", lambda *a, **kw: None)
    monkeypatch.setattr(trip_tracker.crud, "trip_has_point_near", lambda *a, **kw: False)
    monkeypatch.setattr(trip_tracker.crud, "add_gps_point", add_gps_point)
    monkeypatch.setattr(settings, "KARTO_GPS_MIN_SPEED_KPH", 5.0)
    monkeypatch.setattr(settings, "KARTO_GPS_MIN_DISTANCE_METERS", 20.0)
    monkeypatch.setattr(settings, "KARTO_GPSLOG_FILTER_RESET_SECONDS", 120.0)
    monkeypatch.setattr(settings, "KARTO_GPSLOG_MAX_FIX_AGE_SECONDS", 10.0)
    return fake


async def feed(tracker, records, received_at=None):
    """records: (age_seconds, payload) - the age is what the module puts in the topic."""
    for age, payload in records:
        coro = tracker.process_data_notification(VEHICLE, payload, age, received_at=received_at)
        if coro is not None:
            await coro


@pytest.mark.asyncio
async def test_standstill_records_are_dropped_before_opening_a_session(db):
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    # Driving up, then 60 s at a traffic light with the battery values still changing
    await feed(tracker, [
        (100, xsq(lat - 50 * M, lon, 30)),
        (90, xsq(lat, lon, 0)),
        (80, xsq(lat + 1 * M, lon, 0)),
        (70, xsq(lat, lon, 0)),
        (60, xsq(lat + 2 * M, lon, 0)),
        (50, xsq(lat, lon, 0)),
    ])
    assert len(db.stored) == 2  # the approach and the first point at the light
    assert len(db.sessions) == 2
    state = tracker._vehicle_states[VEHICLE]
    assert state.last_gpslog_point.lat == pytest.approx(lat)


@pytest.mark.asyncio
async def test_driving_records_pass(db):
    tracker = TripTrackerService()
    await feed(tracker, [(100 - 10 * i, xsq(49.32 + i * 80 * M, 8.40, 30)) for i in range(5)])
    assert len(db.stored) == 5


@pytest.mark.asyncio
async def test_the_point_where_the_vehicle_stops_is_kept(db):
    """Arriving at 10 m from the last moving record: the stop is the destination."""
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    await feed(tracker, [
        (100, xsq(lat, lon, 25)),
        (90, xsq(lat + 10 * M, lon, 0)),   # came to a halt - kept despite the 10 m
        (80, xsq(lat + 10 * M, lon, 0)),   # still standing - dropped
        (70, xsq(lat + 11 * M, lon, 0)),   # still standing - dropped
    ])
    assert len(db.stored) == 2
    assert tracker._vehicle_states[VEHICLE].last_gpslog_point.lat == pytest.approx(lat + 10 * M)


@pytest.mark.asyncio
async def test_slow_rolling_accumulates_distance_against_the_last_accepted_point(db):
    tracker = TripTrackerService()
    # 3 km/h: about 8 m per 10 s record, below both thresholds on its own
    await feed(tracker, [(100 - 10 * i, xsq(49.32 + i * 8 * M, 8.40, 3)) for i in range(7)])
    # Accepted at 0 m, then at 24 m and 48 m from the start - every third record
    assert len(db.stored) == 3


@pytest.mark.asyncio
async def test_pause_in_the_record_stream_resets_the_filter(db):
    """A new ride from the same parking spot must not lose its first point."""
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    await feed(tracker, [
        (1000, xsq(lat, lon, 0)),
        (990, xsq(lat, lon, 0)),   # standstill, dropped
        (500, xsq(lat, lon, 0)),   # after a pause: vehicle was off, starts afresh
        (490, xsq(lat, lon, 0)),   # standstill again, dropped
    ])
    assert len(db.stored) == 2


@pytest.mark.asyncio
async def test_rejected_records_do_not_move_the_reference(db):
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    await feed(tracker, [
        (100, xsq(lat, lon, 0)),
        (90, xsq(lat + 15 * M, lon, 0)),  # 15 m: dropped
        (80, xsq(lat + 30 * M, lon, 0)),  # 30 m from the accepted point, 15 m from the dropped one
    ])
    assert len(db.stored) == 2


@pytest.mark.asyncio
async def test_an_orphan_record_does_not_become_the_reference(db):
    """
    The first record can beat v.e.on=1 and find no trip. It is dropped - and must not
    then filter out the standstill record at the same spot once the trip exists.
    """
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    trip = db.trip
    db.trip = None
    await feed(tracker, [(100, xsq(lat, lon, 0))])
    assert db.stored == []

    db.trip = trip
    await feed(tracker, [(90, xsq(lat, lon, 0))])
    assert len(db.stored) == 1


@pytest.mark.asyncio
async def test_a_record_lost_to_a_database_error_does_not_become_the_reference(db):
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    db.fail = True
    await feed(tracker, [(100, xsq(lat, lon, 0))])
    db.fail = False
    await feed(tracker, [(90, xsq(lat, lon, 0))])
    assert len(db.stored) == 1


@pytest.mark.asyncio
async def test_a_stale_position_while_moving_is_dropped(db):
    """GPS lost in a tunnel: speed from the car, position frozen for 30 s."""
    tracker = TripTrackerService()
    lat, lon = 49.32, 8.40
    await feed(tracker, [
        (100, xsq(lat, lon, 50, fix_age=1)),
        (90, xsq(lat, lon, 50, fix_age=11)),
        (80, xsq(lat, lon, 50, fix_age=21)),
    ])
    assert len(db.stored) == 1


@pytest.mark.asyncio
async def test_an_old_position_at_a_standstill_is_not_stale(db):
    """Parked with a fix, the position simply does not change - its age grows anyway."""
    tracker = TripTrackerService()
    await feed(tracker, [(100, xsq(49.32, 8.40, 0, fix_age=600))])
    assert len(db.stored) == 1


@pytest.mark.asyncio
async def test_record_age_counts_from_the_receive_time(db):
    """A record that waited in the MQTT queue keeps the time it was received with."""
    tracker = TripTrackerService()
    received = datetime.now(timezone.utc) - timedelta(seconds=30)
    await feed(tracker, [(20, xsq(49.32, 8.40, 30))], received_at=received)
    assert db.stored == [received - timedelta(seconds=20)]


@pytest.mark.asyncio
async def test_an_implausible_record_age_is_dropped(db):
    tracker = TripTrackerService()
    await feed(tracker, [(10**12, xsq(49.32, 8.40, 30))])
    assert db.stored == []
    assert db.sessions == []
