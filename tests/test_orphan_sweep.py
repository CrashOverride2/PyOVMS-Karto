"""
The hourly sweep of trips whose vehicle id the OVMS server no longer registers.

Such trips are unreachable — reads are owner-only, and the registration cutoff hides
them from whoever registers the id next — but they are still the GPS history of a
deleted vehicle or account. They appear when a point races the deletion, when the main
server runs with ENABLE_KARTO_TRIP_TRACKING off (it then never calls Karto), or from
before deletions went through Karto at all.

Deleting by absence is dangerous with the wrong database behind OVMS_DATABASE_URL, so
the guards are pinned as carefully as the deletion.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, database, models_ovms, reaper
from app.config import settings


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def world(monkeypatch):
    """Karto's trips per vehicle id, and the ids the OVMS server registers."""
    state = {"trips": {}, "registered": set(), "deleted": [], "purged": []}

    monkeypatch.setattr(settings, "KARTO_ORPHAN_SWEEP_ENABLED", True)
    monkeypatch.setattr(database, "SessionLocal", FakeSession)
    monkeypatch.setattr(database, "OvmsSessionLocal", FakeSession)
    monkeypatch.setattr(crud, "get_trip_vehicle_ids", lambda db: sorted(state["trips"]))
    monkeypatch.setattr(crud, "get_registered_vehicle_ids",
                        lambda db, ids: {i for i in ids if i in state["registered"]})
    monkeypatch.setattr(crud, "count_registered_vehicles", lambda db: len(state["registered"]))
    monkeypatch.setattr(crud, "get_vehicle_registration",
                        lambda db, vid: object() if vid in state["registered"] else None)

    def delete_all(db, vehicle_id, include_in_progress):
        assert include_in_progress, "a trip still running would outlive the sweep"
        state["deleted"].append(vehicle_id)
        return state["trips"].pop(vehicle_id)

    monkeypatch.setattr(crud, "delete_all_trips_for_vehicle", delete_all)

    async def purge(vehicle_id, delete):
        state["purged"].append(vehicle_id)
        return delete()

    monkeypatch.setattr(reaper.trip_tracker_service, "purge_vehicle", purge)
    return state


def _sweep():
    return asyncio.run(reaper.sweep_unregistered_vehicles())


def test_the_trips_of_an_unregistered_vehicle_are_deleted(world):
    world["trips"] = {"GONE": 4, "CAR1": 2, "CAR2": 7}
    world["registered"] = {"CAR1", "CAR2", "CAR3"}

    assert _sweep() == 4
    assert world["deleted"] == ["GONE"]
    assert world["purged"] == ["GONE"], "not through the tracker lock"


def test_a_vehicle_registered_again_meanwhile_is_left_alone(world, monkeypatch):
    world["trips"] = {"BACK": 3, "CAR1": 1, "CAR2": 1}
    world["registered"] = {"CAR1", "CAR2"}
    found = reaper._find_unregistered_vehicle_ids
    monkeypatch.setattr(reaper, "_find_unregistered_vehicle_ids",
                        lambda: (found(), world["registered"].add("BACK"))[0])

    assert _sweep() == 0
    assert world["deleted"] == []


def test_nothing_is_deleted_against_an_empty_ovms_database(world):
    world["trips"] = {"CAR1": 1}

    assert _sweep() == 0
    assert world["deleted"] == []


def test_nothing_is_deleted_when_most_vehicles_look_unregistered(world):
    """The wrong database, not a wave of deletions."""
    world["trips"] = {f"CAR{i}": 1 for i in range(10)}
    world["registered"] = {"CAR0", "CAR1", "OTHER"}

    assert _sweep() == 0
    assert world["deleted"] == []


def test_a_few_deleted_vehicles_on_a_small_server_are_swept(world):
    """The floor: on a server with three vehicles, three can be gone."""
    world["trips"] = {"A": 1, "B": 1, "C": 1, "KEPT": 1}
    world["registered"] = {"KEPT"}

    assert _sweep() == 3


def test_the_switch_turns_it_off(world, monkeypatch):
    monkeypatch.setattr(settings, "KARTO_ORPHAN_SWEEP_ENABLED", False)
    world["trips"] = {"GONE": 1, "CAR1": 1, "CAR2": 1}
    world["registered"] = {"CAR1", "CAR2"}

    assert _sweep() == 0


def test_one_failed_vehicle_does_not_stop_the_others(world, monkeypatch):
    world["trips"] = {"BAD": 1, "GONE": 2, "CAR1": 1, "CAR2": 1, "CAR3": 1}
    world["registered"] = {"CAR1", "CAR2", "CAR3"}
    real = crud.delete_all_trips_for_vehicle

    def flaky(db, vehicle_id, include_in_progress):
        if vehicle_id == "BAD":
            raise RuntimeError("locked")
        return real(db, vehicle_id, include_in_progress)

    monkeypatch.setattr(crud, "delete_all_trips_for_vehicle", flaky)
    assert _sweep() == 2


def test_the_registration_lookup_is_case_blind_and_chunked(monkeypatch):
    engine = create_engine("sqlite://")
    models_ovms.OvmsBase.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models_ovms.User(id=1, username="u", hashed_password="x", is_admin=False, is_active=True))
    db.add_all([models_ovms.Vehicle(vehicle_id=f"CAR{i}", owner_id=1) for i in range(7)])
    db.commit()
    monkeypatch.setattr(crud, "_REGISTRATION_LOOKUP_CHUNK", 3)

    wanted = [f"car{i}" for i in range(10)]
    assert crud.get_registered_vehicle_ids(db, wanted) == {f"CAR{i}" for i in range(7)}
    assert crud.count_registered_vehicles(db) == 7
