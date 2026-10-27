"""
The energy figure of a completed trip, against metrics whose meaning differs between the
OVMS vehicle integrations (app/trip_energy.py).

Karto used to check the counter difference against v.b.capacity and fall back to SoC used
times v.b.capacity. Renault, smart EQ, NIU and others publish the energy *left* there, so
a trip that used more than remained at its end lost its measured figure, and the stand-in
came out far too low (a Zoe Ph2: 4.1 kWh measured, 0.279 kWh stored). What is pinned here:

- a counter difference is kept whenever the trip's distance and duration allow for it,
  whatever v.b.capacity says; a counter that jumped, or one that did not move over a long
  ride (stored as 0 kWh before), gives way to the estimate;
- what v.b.capacity is gets decided from two readings far enough apart in SoC, and a
  pair that does not tell decides nothing.

The tracker side — learning the meaning from the metrics and handing the estimate on —
is in test_trip_lifecycle.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app import crud, trip_energy


START = datetime(2026, 10, 20, 6, 38, tzinfo=timezone.utc)


def _complete(monkeypatch, distance_km, minutes, energy, estimate):
    monkeypatch.setattr(crud, "track_length_km", lambda db, trip_id: distance_km)
    trip = SimpleNamespace(id=uuid4(), vehicle_id="TESTCAR", start_time=START, start_soc=13.0)
    kept = crud.update_trip_on_completion(MagicMock(), trip, START + timedelta(minutes=minutes), 4.0,
                                          SimpleNamespace(location="end"), energy, energy_estimate_kwh=estimate)
    assert kept
    return trip


def test_a_counter_difference_above_the_energy_left_is_kept(monkeypatch):
    # A Zoe Ph2: 4.1 kWh by its counter, 3.1 kWh left at the end.
    trip = _complete(monkeypatch, distance_km=25.2, minutes=44, energy=4.1, estimate=0.279)
    assert trip.energy_used_kwh == pytest.approx(4.1)


def test_a_jumped_counter_gives_way_to_the_estimate(monkeypatch):
    # The counter changed its source mid-trip: 9026 kWh over 13 km.
    trip = _complete(monkeypatch, distance_km=13.2, minutes=39, energy=9026.3, estimate=2.9)
    assert trip.energy_used_kwh == pytest.approx(2.9)


def test_a_counter_that_did_not_move_over_a_long_ride_gives_way_to_the_estimate(monkeypatch):
    # Integrations that publish v.b.energy.used = 0 without counting: 0.0 kWh for 19 km.
    trip = _complete(monkeypatch, distance_km=18.9, minutes=25, energy=0.0, estimate=2.2)
    assert trip.energy_used_kwh == pytest.approx(2.2)


def test_a_short_ride_below_the_counter_resolution_keeps_its_zero(monkeypatch):
    trip = _complete(monkeypatch, distance_km=1.2, minutes=3, energy=0.0, estimate=None)
    assert trip.energy_used_kwh == 0.0


def test_an_implausible_estimate_is_not_stored_either(monkeypatch):
    trip = _complete(monkeypatch, distance_km=2.0, minutes=5, energy=None, estimate=40.0)
    assert trip.energy_used_kwh is None


def test_a_long_standstill_with_heating_is_still_plausible(monkeypatch):
    # 1.5 km in an hour of traffic jam with the heater on full.
    trip = _complete(monkeypatch, distance_km=1.5, minutes=60, energy=6.5, estimate=None)
    assert trip.energy_used_kwh == pytest.approx(6.5)


def test_capacity_that_falls_with_the_soc_is_the_energy_left():
    # smart EQ "HV energy": 9.1 kWh at 59 %, 2.0 kWh at 12 %.
    assert trip_energy.classify_capacity(59, 9.1, 12, 2.0) == trip_energy.CAPACITY_REMAINING


def test_capacity_that_stays_put_while_the_soc_moves_is_the_usable_capacity():
    assert trip_energy.classify_capacity(80, 52.0, 60, 51.9) == trip_energy.CAPACITY_TOTAL


def test_readings_too_close_in_soc_decide_nothing():
    assert trip_energy.classify_capacity(80, 52.0, 75, 52.0) is None
    assert trip_energy.classify_capacity(80, 40.0, 75, 37.5) is None
