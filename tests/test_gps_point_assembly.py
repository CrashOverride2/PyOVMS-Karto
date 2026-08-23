"""
Assembly of live GPS points from the OVMS metric stream.

These reproduce the failure mode seen in real NIU GT EVO tracks: OVMS keeps its metric
list sorted by name and walks it in that order, so m.time.utc is published *before*
v.p.latitude/v.p.longitude in every transmit pass. Building the point on the time metric
therefore paired a fresh timestamp with the previous pass' position - a constant lag of
one update interval, and a jump back to the pre-outage position after every reconnect.

No broker and no database: with no trip in progress the tracker never opens a session,
so the assembled point can be read straight off the vehicle state.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.trip_tracker import TripTrackerService


VEHICLE = "TESTNEVO"

# One OVMS transmit pass, in the order TransmitModifiedMetrics() emits it (alphabetical).
def transmit_pass(t: str, lat: float, lon: float, speed: float):
    return [
        ("m.time.utc", t),
        ("v.p.latitude", f"{lat:.6f}"),
        ("v.p.longitude", f"{lon:.6f}"),
        ("v.p.speed", f"{speed:.2f}"),
    ]


@pytest.fixture
def tracker(monkeypatch):
    """A tracker whose debounce is short enough to keep the tests fast."""
    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_MAX_WINDOW_SECONDS", 0.5)
    monkeypatch.setattr(settings, "KARTO_GPS_UPDATE_MIN_INTERVAL_SECONDS", 0.0)
    return TripTrackerService()


async def feed(tracker, metrics):
    for metric, payload in metrics:
        coro = tracker.process_message(VEHICLE, metric, payload)
        if coro is not None:
            await coro


async def settle(tracker):
    """Waits for the debounce task of the vehicle to finish."""
    for _ in range(200):
        task = tracker._flush_tasks.get(VEHICLE)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    raise AssertionError("flush task never settled")


@pytest.mark.asyncio
async def test_point_uses_the_position_from_its_own_transmit_pass(tracker):
    """The regression: two passes must not produce a point mixing time and position."""
    await feed(tracker, transmit_pass("2026-08-06 07:41:03 UTC", 49.328033, 8.413617, 11.67))
    await settle(tracker)
    first = tracker._vehicle_states[VEHICLE].last_saved_gps_point
    assert first is not None
    assert (first.lat, first.lon) == (49.328033, 8.413617)
    assert first.timestamp == datetime(2026, 8, 6, 7, 41, 3, tzinfo=timezone.utc)

    await feed(tracker, transmit_pass("2026-08-06 07:41:09 UTC", 49.328220, 8.413817, 11.11))
    await settle(tracker)
    second = tracker._vehicle_states[VEHICLE].last_saved_gps_point
    # Before the fix this carried 49.328033/8.413617 - the previous pass' position.
    assert (second.lat, second.lon) == (49.328220, 8.413817)
    assert second.timestamp == datetime(2026, 8, 6, 7, 41, 9, tzinfo=timezone.utc)
    assert second.speed_kph == 11.11


@pytest.mark.asyncio
async def test_burst_is_flushed_once(tracker):
    """A transmit pass is four messages but must collapse into a single point."""
    flushes = []
    original = tracker._flush_pending_gps_point

    async def counting(vehicle_id, state):
        flushes.append(vehicle_id)
        return await original(vehicle_id, state)

    tracker._flush_pending_gps_point = counting

    await feed(tracker, transmit_pass("2026-08-06 07:41:03 UTC", 49.328033, 8.413617, 11.67))
    await settle(tracker)

    assert flushes == [VEHICLE]
    state = tracker._vehicle_states[VEHICLE]
    assert state.last_saved_gps_point is not None
    assert state.pending_lat is None and state.pending_gpstime is None


@pytest.mark.asyncio
async def test_reconnect_burst_without_a_fresh_position_is_dropped(tracker):
    """
    After an LTE outage the reconnect can deliver m.time.utc without a new position.
    That produced the 3.6 km jump back to where the connection dropped.
    """
    # The position arrives, then the connection drops for four minutes, then the
    # reconnect burst re-sends m.time.utc while v.p.latitude/longitude are unchanged.
    await feed(tracker, [("v.p.latitude", "49.394447"), ("v.p.longitude", "8.371653")])
    state = tracker._vehicle_states[VEHICLE]
    state.pending_pos_received -= 240.0   # age the position by the outage length

    await feed(tracker, [("m.time.utc", "2026-08-05 07:03:04 UTC")])
    await settle(tracker)

    assert state.last_saved_gps_point is None, "stale position must not become a point"


@pytest.mark.asyncio
async def test_split_transmit_pass_still_pairs(tracker):
    """
    TransmitAllMetrics() caps at 100 metrics per call, so a reconnect can split time and
    position across two ticks. Those still belong together and must yield a point.
    """
    await feed(tracker, [("m.time.utc", "2026-08-05 07:03:04 UTC")])
    await settle(tracker)
    assert tracker._vehicle_states[VEHICLE].last_saved_gps_point is None

    await feed(tracker, [("v.p.latitude", "49.426430"), ("v.p.longitude", "8.359582")])
    await settle(tracker)

    point = tracker._vehicle_states[VEHICLE].last_saved_gps_point
    assert point is not None
    assert (point.lat, point.lon) == (49.426430, 8.359582)
    assert point.timestamp == datetime(2026, 8, 5, 7, 3, 4, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_priority_metric_order_also_works(tracker):
    """
    TransmitPriorityMetrics() sends m.time.utc *last*, the opposite of the sorted walk.
    The debounce must not care which order it gets.
    """
    await feed(tracker, [
        ("v.p.latitude", "49.335213"),
        ("v.p.longitude", "8.420012"),
        ("v.p.speed", "18.06"),
        ("m.time.utc", "2026-08-06 07:43:15 UTC"),
    ])
    await settle(tracker)

    point = tracker._vehicle_states[VEHICLE].last_saved_gps_point
    assert (point.lat, point.lon) == (49.335213, 8.420012)
    assert point.timestamp == datetime(2026, 8, 6, 7, 43, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_continuous_publishing_cannot_postpone_the_point(tracker, monkeypatch):
    """The debounce is capped, so a vehicle publishing without pause still gets points."""
    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_DEBOUNCE_SECONDS", 5.0)
    monkeypatch.setattr(settings, "KARTO_GPS_BATCH_MAX_WINDOW_SECONDS", 0.1)

    await feed(tracker, transmit_pass("2026-08-06 07:41:03 UTC", 49.328033, 8.413617, 11.67))
    await asyncio.wait_for(settle(tracker), timeout=2)

    assert tracker._vehicle_states[VEHICLE].last_saved_gps_point is not None
