"""
The life of a trip across a restart of the service, and its end by the reaper.

Until these existed, a restart in the middle of a ride lost the ride: the live path never
looked for the trip already open in the database, so the next point started a second
one, the first was orphaned, and the reaper later *deleted* it. A restart inside the
end grace period did the same to a ride that was already over. What is pinned here:

- a running trip is continued after a restart, its energy baseline intact, and the
  retained burst the restart receives does not add its last point a second time;
- an open trip whose last point is too far back is finalized and a new one started;
- a trip left open by a run that ended while the vehicle was off is ended by the
  retained `v.e.on=0`, not left to the reaper — with the readings of now only while its
  last point is recent, without the retained position the restart receives, and not
  continued by a new ride that starts too long after it;
- an adopted trip without a stored energy baseline reports no energy rather than a
  wrong one;
- the grace period runs beside the MQTT workers, not in them;
- the reaper finalizes instead of deleting, through the vehicle's state;
- a point for a trip that was finalized meanwhile does not extend it;
- buffered log records keep to the resume gap: a later ride's records finalize an old
  open trip instead of extending it;
- any switch to off ends an open trip this run does not track, not only the first report;
- one trip end per vehicle, which an ignored v.e.on=1 does not cancel;
- one open trip per vehicle: finalization is a no-op on a trip no longer open, a refused
  insert continues the existing trip, and the partial unique index exists on both dialects;
- the SQL behind that, the ordered track aggregate and the bulk delete (which keeps the
  running trip), compiled for PostgreSQL; a new point is flushed; a failed migration
  stops the start.

No database: the crud functions the tracker calls are replaced by a small in-memory
store, as in test_gps_log_filter.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app import trip_tracker
from app.config import settings
from app.trip_tracker import TripTrackerService


VEHICLE = "TESTCAR"
T0 = datetime(2026, 10, 2, 8, 0, 0, tzinfo=timezone.utc)

# Roughly 1 m in latitude
M = 1 / 111_320


class Store:
    """The rows the tracker would read and write."""

    def __init__(self):
        self.trips = {}
        self.points = {}  # trip id -> [(ts, lat, lon, speed)]
        self.completed = []  # (trip id, end_soc, energy)
        self.start_energy_writes = []

    def open_trip(self, start, lat, lon, start_energy=None, points=None):
        trip = SimpleNamespace(id=uuid4(), vehicle_id=VEHICLE, status="in_progress",
                               start_time=start, start_energy_kwh=start_energy,
                               start_soc=80.0, start_location=None)
        self.trips[trip.id] = trip
        self.points[trip.id] = points if points is not None else [(start, lat, lon, 30.0)]
        return trip

    def open_trips(self):
        return [t for t in self.trips.values() if t.status == "in_progress"]


def _wkt_latlon(wkt):
    lon, lat = wkt[len("POINT("):-1].split()
    return float(lat), float(lon)


@pytest.fixture
def store(monkeypatch):
    s = Store()
    crud = trip_tracker.crud

    def get_in_progress(db, vid):
        trips = [t for t in s.open_trips() if t.vehicle_id == vid]
        return max(trips, key=lambda t: t.start_time) if trips else None

    def last_position(db, trip_id):
        pts = sorted(s.points.get(trip_id, []))
        if not pts:
            return None
        ts, lat, lon, speed = pts[-1]
        return ts, lat, lon, speed, None

    def last_gps_point(db, trip_id):
        pts = sorted(s.points.get(trip_id, []))
        return SimpleNamespace(timestamp=pts[-1][0], location="end") if pts else None

    def create_trip(db, vid, start, soc, wkt, start_energy_kwh=None):
        # uq_trips_vehicle_in_progress: one open trip per vehicle.
        if any(t.vehicle_id == vid for t in s.open_trips()):
            raise IntegrityError("INSERT INTO trips", {}, Exception("uq_trips_vehicle_in_progress"))
        lat, lon = _wkt_latlon(wkt)
        trip = s.open_trip(start, lat, lon, start_energy_kwh, points=[])
        trip.start_soc = soc
        return trip

    def add_gps_point(db, trip_id, ts, wkt, speed, altitude):
        lat, lon = _wkt_latlon(wkt)
        s.points.setdefault(trip_id, []).append((ts, lat, lon, speed))
        return SimpleNamespace(timestamp=ts, location=wkt)

    def complete(db, trip, end_time, end_soc, end_point, energy, battery_capacity_kwh=None):
        trip.status = "completed"
        trip.end_time = end_time
        s.completed.append((trip.id, end_soc, energy))
        return True

    def set_start_energy(db, trip_id, value):
        s.start_energy_writes.append((trip_id, value))
        s.trips[trip_id].start_energy_kwh = value

    async def no_map(trip_id):
        return None

    monkeypatch.setattr(trip_tracker.database, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(trip_tracker.database, "OvmsSessionLocal", lambda: MagicMock())
    monkeypatch.setattr(crud, "is_trip_tracking_enabled", lambda db, vid: True)
    monkeypatch.setattr(crud, "get_in_progress_trip_by_vehicle", get_in_progress)
    monkeypatch.setattr(crud, "get_last_point_position", last_position)
    monkeypatch.setattr(crud, "get_last_gps_point", last_gps_point)
    monkeypatch.setattr(crud, "get_trip_by_id", lambda db, tid: s.trips.get(tid))
    monkeypatch.setattr(crud, "lock_open_trip", lambda db, tid: next(
        (t for t in s.open_trips() if t.id == tid), None))
    monkeypatch.setattr(crud, "create_trip", create_trip)
    monkeypatch.setattr(crud, "add_gps_point", add_gps_point)
    monkeypatch.setattr(crud, "update_trip_on_completion", complete)
    monkeypatch.setattr(crud, "set_trip_start_energy", set_start_energy)
    monkeypatch.setattr(crud, "find_timed_out_trips", lambda db, timeout: s.open_trips())
    monkeypatch.setattr(trip_tracker, "generate_trip_map", no_map)

    def completed_covering(db, vid, ts, slack_seconds):
        slack = timedelta(seconds=slack_seconds)
        return next((t for t in s.trips.values() if t.vehicle_id == vid and t.status == "completed"
                     and t.start_time <= ts + slack and t.end_time >= ts - slack), None)

    def has_point_near(db, trip_id, ts, tolerance):
        return any(abs((p[0] - ts).total_seconds()) <= tolerance for p in s.points.get(trip_id, []))

    monkeypatch.setattr(crud, "get_completed_trip_covering", completed_covering)
    monkeypatch.setattr(crud, "trip_has_point_near", has_point_near)
    monkeypatch.setattr(crud, "refresh_trip_stats", lambda db, trip: None)
    monkeypatch.setattr(crud, "enqueue_map_generation", lambda db, trip_id: None)

    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_MAX_WINDOW_SECONDS", 0.2)
    monkeypatch.setattr(settings, "KARTO_GPS_UPDATE_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0)
    monkeypatch.setattr(settings, "KARTO_TRIP_RESUME_MAX_GAP_SECONDS", 900)
    monkeypatch.setattr(settings, "KARTO_TRIP_TIMEOUT_SECONDS", 7200)
    return s


def transmit_pass(t: datetime, lat: float, lon: float, speed: float):
    return [
        ("m.time.utc", t.strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("v.p.latitude", f"{lat:.6f}"),
        ("v.p.longitude", f"{lon:.6f}"),
        ("v.p.speed", f"{speed:.2f}"),
    ]


def xsq(lat: float, lon: float, speed: float) -> str:
    """A smart EQ GPS log record; it carries no time, the topic gives its age."""
    return (f"XSQ-GPS-Log,123456,86400,{lat:.6f},{lon:.6f},112,87,{speed:.0f},1,"
            f"0,-71,1.5,0.1,0.0,4.2")


async def feed_log(tracker, age_seconds, payload, received_at):
    await tracker.process_data_notification(VEHICLE, payload, age_seconds, received_at=received_at)


async def feed(tracker, metrics):
    for metric, payload in metrics:
        coro = tracker.process_message(VEHICLE, metric, payload)
        if coro is not None:
            await coro


async def settle(tracker):
    for _ in range(200):
        task = tracker._flush_tasks.get(VEHICLE)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    raise AssertionError("flush task never settled")


async def drive_pass(tracker, t, lat, lon, speed=40.0):
    await feed(tracker, transmit_pass(t, lat, lon, speed))
    await settle(tracker)


async def ended(tracker):
    """Waits for the trip ends in their grace period, which run as tasks of their own."""
    while tracker._trip_end_tasks:
        await asyncio.wait_for(asyncio.gather(*tracker._trip_end_tasks.values(), return_exceptions=True), timeout=5)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_mid_ride_continues_the_open_trip(store):
    lat, lon = 49.30, 8.40
    trip = store.open_trip(T0, lat, lon, start_energy=10.0,
                           points=[(T0, lat, lon, 40.0), (T0 + timedelta(seconds=30), lat + 300 * M, lon, 40.0)])

    # A fresh run: the retained burst first — v.e.on, the energy counter, and the last
    # pass the module sent, which is the point the trip already ends with.
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1"), ("v.b.energy.used", "11.0")])
    await drive_pass(tracker, T0 + timedelta(seconds=30), lat + 300 * M, lon)

    state = tracker._vehicle_states[VEHICLE]
    assert state.current_trip_id == str(trip.id)
    assert len(store.points[trip.id]) == 2, "the replayed last point was stored again"
    assert state.trip_start_energy_kwh == 10.0, "the energy baseline was not taken from the row"

    # The ride goes on: the next pass extends the same trip, no second one appears.
    await drive_pass(tracker, T0 + timedelta(seconds=60), lat + 600 * M, lon)
    assert len(store.trips) == 1
    assert len(store.points[trip.id]) == 3

    # And it ends normally, with the energy measured from the stored baseline.
    await feed(tracker, [("v.b.energy.used", "12.5"), ("v.b.soc", "71")])
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)
    assert store.completed == [(trip.id, 71.0, pytest.approx(2.5))]
    assert state.current_trip_id is None


@pytest.mark.asyncio
async def test_an_open_trip_too_far_back_is_finalized_and_a_new_one_started(store):
    lat, lon = 49.30, 8.40
    old = store.open_trip(T0, lat, lon, points=[(T0, lat, lon, 40.0),
                                                 (T0 + timedelta(minutes=5), lat + 3000 * M, lon, 40.0)])

    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1"), ("v.b.soc", "60")])
    later = T0 + timedelta(minutes=5) + timedelta(seconds=settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS + 60)
    await drive_pass(tracker, later, lat + 5000 * M, lon)

    # The old ride ends at its last point — without the SoC of now, which belongs to
    # a different moment.
    assert store.completed == [(old.id, None, None)]
    state = tracker._vehicle_states[VEHICLE]
    assert state.current_trip_id is not None and state.current_trip_id != str(old.id)
    assert len(store.trips) == 2


@pytest.mark.asyncio
async def test_a_trip_left_open_while_the_vehicle_is_off_is_ended_by_the_retained_state(store):
    """The car was switched off while Karto was down, or inside the grace period."""
    stopped = datetime.now(timezone.utc) - timedelta(seconds=60)
    trip = store.open_trip(stopped - timedelta(minutes=10), 49.30, 8.40, start_energy=10.0,
                           points=[(stopped - timedelta(minutes=10), 49.30, 8.40, 30.0),
                                   (stopped, 49.30 + 3000 * M, 8.40, 0.0)])

    tracker = TripTrackerService()
    await feed(tracker, [("v.b.soc", "64"), ("v.b.energy.used", "13.0")])
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)

    # A minute after it stopped, the readings of now are the readings of its end.
    assert store.completed == [(trip.id, 64.0, pytest.approx(3.0))]
    assert tracker._vehicle_states[VEHICLE].current_trip_id is None


@pytest.mark.asyncio
async def test_only_the_first_v_e_on_after_a_start_adopts_a_trip_for_ending(store):
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "0")])
    # A trip appearing later (another process, a test) is not picked up by a repeat.
    store.open_trip(datetime.now(timezone.utc), 49.30, 8.40)
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)
    assert store.completed == []


def _left_open(store, stopped, start_energy=10.0):
    """A ride the previous run recorded up to `stopped`, at 40 km/h until the end."""
    lat, lon = 49.30, 8.40
    start = stopped - timedelta(minutes=10)
    return store.open_trip(start, lat, lon, start_energy=start_energy,
                           points=[(start, lat, lon, 40.0), (stopped, lat + 3000 * M, lon, 40.0)])


@pytest.mark.asyncio
async def test_a_trip_left_open_long_ago_ends_without_the_readings_of_now(store):
    """Karto was down for an hour; the car was charged meanwhile."""
    now = datetime.now(timezone.utc)
    trip = _left_open(store, now - timedelta(seconds=settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS + 600))

    tracker = TripTrackerService()
    await feed(tracker, [("v.b.soc", "95"), ("v.b.energy.used", "0.4")])
    await feed(tracker, [("v.e.on", "0")])

    # Ended at once and at its last point: 95 % would make the ride a charge, and the
    # counter of now says nothing about it.
    assert store.completed == [(trip.id, None, None)]
    assert tracker._trip_end_tasks == {}
    assert tracker._vehicle_states[VEHICLE].current_trip_id is None


@pytest.mark.asyncio
async def test_the_retained_position_is_not_added_to_a_trip_ended_after_a_restart(store, monkeypatch):
    """
    The retained burst a restart receives: the module's last m.time.utc (about now) with
    the position it last sent. The trip ended moving, so the filter alone would take it.
    """
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.2)
    now = datetime.now(timezone.utc)
    stopped = now - timedelta(seconds=120)
    trip = _left_open(store, stopped)
    before = list(store.points[trip.id])

    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "0")])
    # Parked 500 m on, out of reach of the stored track.
    await drive_pass(tracker, now, 49.30 + 3500 * M, 8.40, speed=0.0)
    await ended(tracker)

    assert store.points[trip.id] == before, "a point from the restart was added to the finished ride"
    assert [c[0] for c in store.completed] == [trip.id]


@pytest.mark.asyncio
async def test_a_trip_left_open_continues_when_the_vehicle_is_on_again_soon(store, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.3)
    now = datetime.now(timezone.utc)
    stopped = now - timedelta(seconds=60)
    trip = _left_open(store, stopped)

    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "0")])
    await feed(tracker, [("v.e.on", "1")])
    await ended(tracker)
    assert store.completed == []

    # The same ride, a minute on: its points go on the same trip again.
    await drive_pass(tracker, now, 49.30 + 4000 * M, 8.40)
    assert len(store.trips) == 1
    assert len(store.points[trip.id]) == 3


@pytest.mark.asyncio
async def test_a_trip_left_open_is_not_continued_by_a_ride_starting_too_long_after_it(store, monkeypatch):
    """v.e.on=1 inside the grace period, but the trip's last point is past the resume gap."""
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.3)
    now = datetime.now(timezone.utc)
    trip = _left_open(store, now - timedelta(seconds=600))

    tracker = TripTrackerService()
    await feed(tracker, [("v.b.soc", "70"), ("v.e.on", "0")])
    assert tracker._vehicle_states[VEHICLE].current_trip_id == str(trip.id)
    # Ten minutes is inside the gap that let it be adopted; tighten the gap so the ride
    # starting now lies beyond it, as one starting a quarter of an hour later would.
    monkeypatch.setattr(settings, "KARTO_TRIP_RESUME_MAX_GAP_SECONDS", 300)
    await feed(tracker, [("v.e.on", "1")])
    await ended(tracker)

    assert store.completed == [(trip.id, None, None)]
    await drive_pass(tracker, now, 49.40, 8.50)
    new_id = tracker._vehicle_states[VEHICLE].current_trip_id
    assert new_id is not None and new_id != str(trip.id)
    assert len(store.points[trip.id]) == 2, "the new ride was folded into the old trip"


@pytest.mark.asyncio
async def test_a_resumed_trip_without_a_stored_baseline_reports_no_energy(store):
    """Open when the start energy column was added: the row has none."""
    lat, lon = 49.30, 8.40
    trip = store.open_trip(T0, lat, lon, start_energy=None,
                           points=[(T0, lat, lon, 40.0), (T0 + timedelta(seconds=30), lat + 300 * M, lon, 40.0)])

    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1"), ("v.b.energy.used", "11.0")])
    await drive_pass(tracker, T0 + timedelta(seconds=60), lat + 600 * M, lon)
    assert tracker._vehicle_states[VEHICLE].current_trip_id == str(trip.id)

    # Neither the first reading after the restart becomes its start ...
    await feed(tracker, [("v.b.energy.used", "12.5"), ("v.b.soc", "71")])
    assert store.start_energy_writes == []
    assert tracker.live_view(VEHICLE, str(trip.id))["energy_used_kwh"] is None
    # ... nor is the whole counter read as its consumption.
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)
    assert store.completed == [(trip.id, 71.0, None)]


@pytest.mark.asyncio
async def test_a_trip_left_open_without_a_stored_baseline_ends_without_energy(store):
    trip = _left_open(store, datetime.now(timezone.utc) - timedelta(seconds=60), start_energy=None)

    tracker = TripTrackerService()
    await feed(tracker, [("v.b.soc", "64"), ("v.b.energy.used", "13.0")])
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)
    assert store.completed == [(trip.id, 64.0, None)]


@pytest.mark.asyncio
async def test_the_grace_period_does_not_hold_the_mqtt_worker(store, monkeypatch):
    """
    The handler returns at once; the wait is a task of its own. After a restart every
    vehicle that is off starts its grace period together, and workers asleep in it let
    the queue in front of them overflow.
    """
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 60)
    lat, lon = 49.30, 8.40
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, lat, lon)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id

    await asyncio.wait_for(feed(tracker, [("v.e.on", "0")]), timeout=1)
    assert len(tracker._trip_end_tasks) == 1
    assert store.completed == []
    assert tracker.live_view(VEHICLE, trip_id)["phase"] == "ending"

    # Shutdown cancels the wait; the trip stays open for the next start to end.
    await tracker.cancel_pending_trip_ends()
    assert tracker._trip_end_tasks == {}
    assert store.completed == []
    assert store.trips[next(iter(store.trips))].status == "in_progress"


@pytest.mark.asyncio
async def test_a_newer_v_e_on_still_cancels_a_trip_end_waiting_in_its_task(store, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.2)
    lat, lon = 49.30, 8.40
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, lat, lon)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id

    await feed(tracker, [("v.e.on", "0")])
    await feed(tracker, [("v.e.on", "1")])
    await ended(tracker)
    assert store.completed == []
    assert tracker._vehicle_states[VEHICLE].current_trip_id == trip_id


@pytest.mark.asyncio
async def test_the_reaper_finalizes_instead_of_deleting(store):
    now = datetime.now(timezone.utc)
    stale = store.open_trip(now - timedelta(hours=3), 49.30, 8.40)

    tracker = TripTrackerService()
    assert await tracker.reap_timed_out_trips() == 1
    assert store.completed == [(stale.id, None, None)]
    assert stale.id in store.trips, "the trip was deleted"


@pytest.mark.asyncio
async def test_the_reaper_clears_the_state_that_tracked_the_trip(store):
    """A lost v.e.on=0: the state still believes the trip runs."""
    now = datetime.now(timezone.utc)
    lat, lon = 49.30, 8.40
    trip = store.open_trip(now - timedelta(hours=3), lat, lon)

    tracker = TripTrackerService()
    state = await tracker._get_or_create_state(VEHICLE)
    state.is_driving = True
    state.current_trip_id = str(trip.id)
    state.latest_soc = 55.0

    assert await tracker.reap_timed_out_trips() == 1
    # The SoC the state holds is from now, hours after the trip stopped — the car may
    # have been charged since. The trip ends without it.
    assert store.completed == [(trip.id, None, None)]
    assert state.current_trip_id is None


@pytest.mark.asyncio
async def test_the_reaper_leaves_a_trip_that_moved_meanwhile(store):
    now = datetime.now(timezone.utc)
    trip = store.open_trip(now - timedelta(hours=3), 49.30, 8.40,
                           points=[(now - timedelta(hours=3), 49.30, 8.40, 30.0),
                                   (now - timedelta(seconds=20), 49.31, 8.40, 30.0)])

    tracker = TripTrackerService()
    assert await tracker.reap_timed_out_trips() == 0
    assert trip.status == "in_progress"


@pytest.mark.asyncio
async def test_a_point_for_a_trip_finalized_meanwhile_does_not_extend_it(store):
    lat, lon = 49.30, 8.40
    trip = store.open_trip(T0, lat, lon)
    trip.status = "completed"  # the reaper got there first

    tracker = TripTrackerService()
    state = await tracker._get_or_create_state(VEHICLE)
    state.is_driving = True
    state.driving_reported = True
    state.current_trip_id = str(trip.id)

    await drive_pass(tracker, T0 + timedelta(seconds=30), lat + 300 * M, lon)
    assert len(store.points[trip.id]) == 1
    assert state.current_trip_id is None


@pytest.mark.asyncio
async def test_an_energy_baseline_seen_mid_trip_is_stored_on_the_row(store):
    lat, lon = 49.30, 8.40
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, lat, lon)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id
    assert trip_id is not None

    await feed(tracker, [("v.b.energy.used", "7.25")])
    assert store.start_energy_writes == [(store.trips[next(iter(store.trips))].id, 7.25)]


@pytest.mark.asyncio
async def test_live_view_reports_the_phase_of_the_tracked_trip(store):
    lat, lon = 49.30, 8.40
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1"), ("v.b.energy.used", "5.0"), ("v.b.soc", "90")])
    await drive_pass(tracker, T0, lat, lon)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id

    await feed(tracker, [("v.b.energy.used", "5.8"), ("v.b.soc", "88")])
    view = tracker.live_view(VEHICLE, trip_id)
    assert view["phase"] == "driving"
    assert view["current_soc"] == 88.0
    assert view["energy_used_kwh"] == pytest.approx(0.8)

    # A trip this run does not track has nothing live to report.
    assert tracker.live_view(VEHICLE, str(uuid4())) == {
        "phase": None, "current_soc": None, "energy_used_kwh": None}


# ---------------------------------------------------------------------------
# Buffered GPS log records and an open trip this run does not track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_log_record_close_to_an_untracked_open_trip_goes_into_it(store):
    now = datetime.now(timezone.utc)
    trip = _left_open(store, now - timedelta(seconds=120))

    tracker = TripTrackerService()
    await feed_log(tracker, 60, xsq(49.30 + 3500 * M, 8.40, 30), received_at=now)

    assert len(store.points[trip.id]) == 3
    assert store.completed == []


@pytest.mark.asyncio
async def test_a_log_record_of_a_later_ride_does_not_extend_an_old_open_trip(store):
    """
    After a restart the QoS 2 queue delivers the records of a ride that began long after
    the open trip last moved. Folded in, two rides were one — and stayed one, since the
    reaper finalizes rather than deletes.
    """
    now = datetime.now(timezone.utc)
    trip = _left_open(store, now - timedelta(hours=1))
    before = list(store.points[trip.id])

    tracker = TripTrackerService()
    await feed_log(tracker, 60, xsq(49.40, 8.50, 30), received_at=now)

    # The old ride ends at its last point, without the readings of now ...
    assert store.completed == [(trip.id, None, None)]
    assert store.points[trip.id] == before
    # ... and the record is what it would have been with no trip open: off, an orphan.
    assert len(store.trips) == 1


@pytest.mark.asyncio
async def test_a_log_record_of_a_later_ride_starts_its_own_trip_while_driving(store):
    now = datetime.now(timezone.utc)
    old = _left_open(store, now - timedelta(hours=1))

    tracker = TripTrackerService()
    state = await tracker._get_or_create_state(VEHICLE)
    state.is_driving = True
    state.driving_reported = True
    await feed_log(tracker, 60, xsq(49.40, 8.50, 30), received_at=now)

    assert store.completed == [(old.id, None, None)]
    new_id = state.current_trip_id
    assert new_id is not None and new_id != str(old.id)
    assert len(store.points[next(t.id for t in store.trips.values() if str(t.id) == new_id)]) == 1


# ---------------------------------------------------------------------------
# Ending a trip this run did not track, and the end task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_mid_ride_parked_before_any_point_is_still_ended(store):
    """
    The first report after the restart was the retained v.e.on=1, and the car was
    parked before a live point resumed the trip. Its switch to off ends the trip, with
    the readings of now — not two hours later by the reaper, without them.
    """
    trip = _left_open(store, datetime.now(timezone.utc) - timedelta(seconds=60))

    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1"), ("v.b.soc", "64"), ("v.b.energy.used", "13.0")])
    assert tracker._vehicle_states[VEHICLE].current_trip_id is None
    await feed(tracker, [("v.e.on", "0")])
    await ended(tracker)

    assert store.completed == [(trip.id, 64.0, pytest.approx(3.0))]


@pytest.mark.asyncio
async def test_a_trip_fed_only_by_log_records_is_ended_when_the_vehicle_switches_off(store):
    now = datetime.now(timezone.utc)
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    # The live path never got a fix; the module's buffered records opened the trip, and
    # a restart in between left this run with no state for it.
    await feed_log(tracker, 90, xsq(49.30, 8.40, 30), received_at=now)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id
    tracker._clear_trip_state(tracker._vehicle_states[VEHICLE])
    await feed_log(tracker, 60, xsq(49.30 + 3000 * M, 8.40, 30), received_at=now)

    await feed(tracker, [("v.b.soc", "70"), ("v.e.on", "0")])
    await ended(tracker)
    assert [(str(c[0]), c[1]) for c in store.completed] == [(trip_id, 70.0)]


@pytest.mark.asyncio
async def test_a_toggling_module_keeps_one_trip_end_per_vehicle(store, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 60)
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, 49.30, 8.40)

    first = None
    for _ in range(5):
        await feed(tracker, [("v.e.on", "0")])
        first = first or tracker._trip_end_tasks[VEHICLE]
        await feed(tracker, [("v.e.on", "1")])
    await feed(tracker, [("v.e.on", "0")])
    await asyncio.sleep(0)

    assert len(tracker._trip_end_tasks) == 1
    assert first.cancelled(), "a replaced trip end kept sleeping"

    await tracker.cancel_pending_trip_ends()
    assert tracker._trip_end_tasks == {}
    assert store.completed == []


@pytest.mark.asyncio
async def test_the_trip_end_that_replaced_the_others_still_ends_the_trip(store, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.2)
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, 49.30, 8.40)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id

    await feed(tracker, [("v.e.on", "0"), ("v.e.on", "1"), ("v.e.on", "0")])
    await ended(tracker)
    assert [str(c[0]) for c in store.completed] == [trip_id]


@pytest.mark.asyncio
async def test_an_ignored_v_e_on_does_not_cancel_a_pending_trip_end(store, monkeypatch):
    """Trip tracking switched off mid-ride: the v.e.on=1 is ignored, the end is not."""
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.2)
    tracker = TripTrackerService()
    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, T0, 49.30, 8.40)
    trip_id = tracker._vehicle_states[VEHICLE].current_trip_id

    await feed(tracker, [("v.b.soc", "66"), ("v.e.on", "0")])
    monkeypatch.setattr(trip_tracker.crud, "is_trip_tracking_enabled", lambda db, vid: False)
    await feed(tracker, [("v.e.on", "1")])
    await ended(tracker)

    assert [(str(c[0]), c[1]) for c in store.completed] == [(trip_id, 66.0)]


@pytest.mark.asyncio
async def test_an_ignored_v_e_on_does_not_cancel_the_end_of_an_inherited_trip(store, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_TRIP_END_GRACE_PERIOD_SECONDS", 0.2)
    trip = _left_open(store, datetime.now(timezone.utc) - timedelta(seconds=60))

    tracker = TripTrackerService()
    await feed(tracker, [("v.b.soc", "64"), ("v.e.on", "0")])
    monkeypatch.setattr(trip_tracker.crud, "is_trip_tracking_enabled", lambda db, vid: False)
    await feed(tracker, [("v.e.on", "1")])
    await ended(tracker)

    assert [(c[0], c[1]) for c in store.completed] == [(trip.id, 64.0)]


# ---------------------------------------------------------------------------
# One open trip per vehicle, held by the database as well
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trip_finalized_by_someone_else_is_not_finalized_again(store):
    trip = _left_open(store, datetime.now(timezone.utc) - timedelta(hours=3))
    tracker = TripTrackerService()
    state = await tracker._get_or_create_state(VEHICLE)
    state.latest_soc = 50.0

    # Loaded while open, completed by another finalizer before the lock was taken.
    trip.status = "completed"
    assert tracker._finalize_trip(MagicMock(), VEHICLE, trip, state) is False
    assert store.completed == []


@pytest.mark.asyncio
async def test_a_trip_opened_meanwhile_is_continued_rather_than_duplicated(store, monkeypatch):
    """
    The open trip was not there when this run looked (another process opened it), so the
    insert of a second one hits uq_trips_vehicle_in_progress. The point goes on the
    existing trip instead.
    """
    now = datetime.now(timezone.utc)
    trip = _left_open(store, now - timedelta(seconds=30))
    tracker = TripTrackerService()
    monkeypatch.setattr(tracker, "_resume_open_trip", lambda vid, state, point: False)

    await feed(tracker, [("v.e.on", "1")])
    await drive_pass(tracker, now, 49.30 + 3500 * M, 8.40)

    assert len(store.trips) == 1
    assert tracker._vehicle_states[VEHICLE].current_trip_id == str(trip.id)
    assert len(store.points[trip.id]) == 3


def test_the_open_trip_index_is_partial_and_unique_on_both_dialects():
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex
    from app.models import Trip

    index = next(i for i in Trip.__table__.indexes if i.name == "uq_trips_vehicle_in_progress")
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        ddl = str(CreateIndex(index).compile(dialect=dialect))
        assert "CREATE UNIQUE INDEX" in ddl
        assert "WHERE status = 'in_progress'" in ddl


# ---------------------------------------------------------------------------
# The SQL the tracker relies on, compiled as PostgreSQL would receive it
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_queries(monkeypatch):
    """An unbound session whose queries are recorded instead of run."""
    from sqlalchemy.orm import Query, Session

    seen = SimpleNamespace(first=[], deleted=[], results=[])

    def first(self):
        seen.first.append(self.statement)
        return seen.results.pop(0) if seen.results else None

    def delete(self, synchronize_session="auto"):
        seen.deleted.append(self.statement)
        return 0

    monkeypatch.setattr(Query, "first", first)
    monkeypatch.setattr(Query, "all", lambda self: [(uuid4(), None, None)])
    monkeypatch.setattr(Query, "count", lambda self: 5)
    monkeypatch.setattr(Query, "delete", delete)
    session = Session()
    monkeypatch.setattr(session, "commit", lambda: None)
    seen.session = session
    return seen


def _pg(statement) -> str:
    from sqlalchemy.dialects import postgresql
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_finalization_locks_the_row_only_while_it_is_open(captured_queries):
    from app import crud

    crud.lock_open_trip(captured_queries.session, uuid4())
    sql = _pg(captured_queries.first[0])
    assert "FOR UPDATE" in sql
    assert "trips.status = 'in_progress'" in sql


def test_the_trip_geometry_is_ordered_inside_the_aggregate(captured_queries):
    from app import crud

    captured_queries.results = [SimpleNamespace(id=uuid4()), ('{"type": "LineString"}',)]
    crud.get_trip_with_points(captured_queries.session, uuid4())
    sql = _pg(captured_queries.first[-1])
    assert "ST_MakeLine(CAST(gps_points.location AS geometry(GEOMETRY,-1)) ORDER BY gps_points.timestamp)" in sql
    assert "(SELECT" not in sql, "the order is back in a subquery"


@pytest.mark.parametrize("include_in_progress, keeps_running", [(False, True), (True, False)])
def test_the_bulk_delete_keeps_the_running_trip_unless_asked(captured_queries, monkeypatch,
                                                             include_in_progress, keeps_running):
    from app import crud

    monkeypatch.setattr(crud, "_remove_map_preview", lambda path: None)
    crud.delete_all_trips_for_vehicle(captured_queries.session, VEHICLE,
                                      include_in_progress=include_in_progress)
    trips_delete = _pg(captured_queries.deleted[-1])
    assert "FROM trips" in trips_delete
    assert ("trips.status != 'in_progress'" in trips_delete) is keeps_running


def test_the_bulk_delete_route_keeps_the_running_trip_by_default(monkeypatch):
    from fastapi import FastAPI
    from app import api

    calls = []
    monkeypatch.setattr(api.crud, "check_vehicle_ownership", lambda ovms_db, user_id, vehicle_id: True)
    monkeypatch.setattr(api.crud, "get_map_paths_for_vehicle", lambda db, vehicle_id: [])
    monkeypatch.setattr(api.crud, "delete_all_trips_for_vehicle",
                        lambda db, vehicle_id, include_in_progress: calls.append(include_in_progress) or 0)

    api.delete_all_trips_for_vehicle("testcar", include_in_progress=True, db=None, ovms_db=None,
                                     current_user=SimpleNamespace(id=1))
    assert calls == [True]

    # The default is what the app's "delete all trips" sends; the main server's /docs
    # is built from this schema.
    app = FastAPI()
    app.include_router(api.router)
    params = app.openapi()["paths"]["/api/karto/v1/vehicles/{vehicle_id}/trips"]["delete"]["parameters"]
    flag = next(p for p in params if p["name"] == "include_in_progress")
    assert flag["in"] == "query" and flag["schema"]["default"] is False


def test_a_new_point_is_flushed_before_the_statistics_read_the_track():
    """The sessions run with autoflush off; unflushed, the point is not in the track."""
    from app import crud

    db = MagicMock()
    crud.add_gps_point(db, uuid4(), T0, "POINT(8.4 49.3)", 30.0, None)
    assert [c[0] for c in db.method_calls] == ["add", "flush"]


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_migration_aborts_startup(monkeypatch):
    """Running on against an old schema, every query naming the new column fails."""
    from app import lifespan

    def broken(cfg, revision):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(lifespan.alembic_command, "upgrade", broken)
    with pytest.raises(RuntimeError, match="relation does not exist"):
        await lifespan.run_migrations()


# ---------------------------------------------------------------------------
# The API's view of a running trip
# ---------------------------------------------------------------------------


@pytest.fixture
def api_store(monkeypatch):
    from app import api

    s = SimpleNamespace(trip=None, last=None, length=None)
    monkeypatch.setattr(api, "_authorize_vehicle", lambda db, vid, uid, action: None)
    monkeypatch.setattr(api.crud, "get_current_trip_for_vehicle", lambda db, vehicle_id, cutoff=None: s.trip)
    monkeypatch.setattr(api.crud, "get_last_point_position", lambda db, tid: s.last)
    monkeypatch.setattr(api.crud, "track_length_km", lambda db, tid: s.length)
    monkeypatch.setattr(api.crud, "get_start_position", lambda trip: (49.30, 8.40))
    return s


def _call_current(api):
    return api.get_current_trip("testcar", db=None, ovms_db=None, current_user=SimpleNamespace(id=1))


def test_no_open_trip_is_204(api_store):
    from app import api

    assert _call_current(api).status_code == 204


def test_an_open_trip_past_the_timeout_is_not_running(api_store):
    from app import api

    now = datetime.now(timezone.utc)
    api_store.trip = SimpleNamespace(id=uuid4(), vehicle_id=VEHICLE, status="in_progress",
                                     start_time=now - timedelta(hours=4), start_soc=80.0)
    api_store.last = (now - timedelta(seconds=settings.KARTO_TRIP_TIMEOUT_SECONDS + 5), 49.31, 8.40, 0.0, None)
    assert _call_current(api).status_code == 204


def test_a_running_trip_is_reported_with_what_is_known_so_far(api_store, monkeypatch):
    from app import api

    now = datetime.now(timezone.utc)
    trip_id = uuid4()
    api_store.trip = SimpleNamespace(id=trip_id, vehicle_id=VEHICLE, status="in_progress",
                                     start_time=now - timedelta(minutes=20), start_soc=80.0)
    api_store.last = (now - timedelta(seconds=10), 49.35, 8.42, 52.0, None)
    api_store.length = 14.2
    monkeypatch.setattr(api.trip_tracker_service, "live_view", lambda vid, tid: {
        "phase": "driving", "current_soc": 76.5, "energy_used_kwh": 2.1234})

    result = _call_current(api)
    assert result.id == trip_id
    assert result.phase == "driving"
    assert result.distance_km == 14.2
    assert result.duration_seconds == 20 * 60 - 10
    assert result.average_speed_kph == pytest.approx(14.2 / (20 * 60 - 10) * 3600, abs=0.1)
    assert result.soc_used == 3.5
    assert result.energy_used_kwh == 2.123
    assert (result.last_lat, result.last_lon, result.last_speed_kph) == (49.35, 8.42, 52.0)
    assert (result.start_lat, result.start_lon) == (49.30, 8.40)


def test_a_running_trip_cannot_be_deleted(monkeypatch):
    """The tracker would start a new trip with the next point: the ride split in two."""
    from fastapi import HTTPException
    from app import api

    trip = SimpleNamespace(id=uuid4(), status="in_progress",
                           map_preview_path=None, map_preview_path_light=None)
    monkeypatch.setattr(api, "_authorize_trip", lambda db, ovms_db, tid, uid: trip)
    deleted = []
    monkeypatch.setattr(api.crud, "delete_trip", lambda db, trip_id: deleted.append(trip_id))

    with pytest.raises(HTTPException) as excinfo:
        api.delete_trip(trip.id, db=None, ovms_db=None, current_user=SimpleNamespace(id=1))
    assert excinfo.value.status_code == 409
    assert deleted == []


@pytest.mark.parametrize("route", ["get_trip_gpx", "get_trip_kml"])
def test_a_running_trip_cannot_be_exported(monkeypatch, route):
    """A file named after the trip would hold a fragment of it."""
    from fastapi import HTTPException
    from app import api

    trip = SimpleNamespace(id=uuid4(), status="in_progress")
    monkeypatch.setattr(api, "_authorize_trip", lambda db, ovms_db, tid, uid: trip)
    monkeypatch.setattr(api.crud, "get_trip_points_for_export",
                        lambda db, trip_id: pytest.fail("the points of a running trip were read for export"))

    with pytest.raises(HTTPException) as excinfo:
        getattr(api, route)(trip.id, db=None, ovms_db=None, current_user=SimpleNamespace(id=1))
    assert excinfo.value.status_code == 409


def test_the_409_is_declared_in_the_api_schema():
    """The main server's /docs is built from this schema; an undeclared 409 is invisible there."""
    from fastapi import FastAPI
    from app import api

    app = FastAPI()
    app.include_router(api.router)
    paths = app.openapi()["paths"]
    assert "409" in paths["/api/karto/v1/trips/{trip_id}"]["delete"]["responses"]
    assert "409" in paths["/api/karto/v1/trips/{trip_id}/gpx"]["get"]["responses"]
    assert "409" in paths["/api/karto/v1/trips/{trip_id}/kml"]["get"]["responses"]


def test_the_start_position_is_read_from_the_loaded_row():
    """Polled every 30 s per client: no second query for a value the row already holds."""
    import shapely
    from geoalchemy2.elements import WKBElement
    from app import crud

    point = shapely.set_srid(shapely.Point(8.40, 49.30), 4326)
    trip = SimpleNamespace(start_location=WKBElement(shapely.to_wkb(point, include_srid=True),
                                                     srid=4326, extended=True))
    assert crud.get_start_position(trip) == pytest.approx((49.30, 8.40))
    assert crud.get_start_position(SimpleNamespace(start_location=None)) is None
