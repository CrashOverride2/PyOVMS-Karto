"""
M-12 / M-13 — trip authorization is structural, and a freed vehicle id does not
inherit the previous owner's history.

M-12: authorization existed only as an API-layer pre-check, and several routes loaded
the trip — including ST_MakeLine/ST_AsGeoJSON over every point — *before* asking who
was calling. Nothing was exploitable, because every route did call the check; the
problem was that one new route forgetting to would leak silently.

M-13: deleting a vehicle in the main server frees its id, and the Karto deletion that
should follow is best-effort. If Karto was unreachable the GPS history stayed behind,
and whoever registered the same id next passed check_vehicle_ownership and inherited
the previous owner's complete movement profile.
"""

import datetime
import inspect
from types import SimpleNamespace

import pytest

from app import api, crud


def _code_only(obj):
    lines = inspect.getsource(obj).splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


# ---------------------------------------------------------------------------
# M-12 — authorization lives in the CRUD layer
# ---------------------------------------------------------------------------


def test_resolver_exists_and_takes_the_user():
    signature = inspect.signature(crud.resolve_owned_trip)

    for parameter in ("db", "ovms_db", "trip_id", "user_id"):
        assert parameter in signature.parameters, f"resolver is missing {parameter}"


@pytest.mark.parametrize(
    "route",
    ["get_trip_details", "delete_trip", "get_trip_gpx", "get_trip_kml"],
)
def test_trip_routes_go_through_the_resolver(route):
    source = _code_only(getattr(api, route))

    assert "_authorize_trip(" in source, f"{route} authorizes by hand"
    assert "crud.get_trip_by_id(" not in source, (
        f"{route} still fetches a trip without stating on whose behalf"
    )


def test_trip_details_authorizes_before_building_geometry():
    """
    The expensive part is ST_MakeLine/ST_AsGeoJSON over every point of the trip. It
    used to run for anyone who knew a trip id.
    """
    source = _code_only(api.get_trip_details)

    gate = source.index("_authorize_trip(")
    geometry = source.index("get_trip_with_points(")
    assert gate < geometry


def test_unknown_and_foreign_trips_are_indistinguishable():
    """
    404 for both. The old 404/403 split confirmed that a trip id existed for someone
    else — a membership oracle over UUIDs that may appear in shared links or logs.
    """
    source = _code_only(api._authorize_trip)

    assert "TripNotFound" in source and "TripNotAuthorized" in source
    assert "HTTP_403_FORBIDDEN" not in source, (
        "the resolver still distinguishes 'not found' from 'not yours'"
    )


# ---------------------------------------------------------------------------
# M-13 — a re-registered vehicle id does not inherit old trips
# ---------------------------------------------------------------------------


def _vehicle(created_at):
    return SimpleNamespace(vehicle_id="CAR1", owner_id=1, created_at=created_at)


def _trip(start_time):
    return SimpleNamespace(vehicle_id="CAR1", start_time=start_time)


def test_trip_older_than_the_registration_is_refused():
    registered = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    older = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    assert crud.trip_predates_registration(_vehicle(registered), _trip(older)) is True


def test_trip_from_the_current_registration_is_served():
    registered = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    later = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)

    assert crud.trip_predates_registration(_vehicle(registered), _trip(later)) is False


def test_naive_timestamps_are_compared_as_utc():
    """SQLite and MySQL hand back naive datetimes; they are stored as UTC."""
    registered = datetime.datetime(2026, 6, 1)
    older = datetime.datetime(2026, 1, 1)

    assert crud.trip_predates_registration(_vehicle(registered), _trip(older)) is True


def test_missing_created_at_keeps_the_old_behaviour():
    """
    Rows predating the column must not have their history hidden — that would be a
    silent data-loss bug dressed as a security fix.
    """
    trip = _trip(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))

    assert crud.trip_predates_registration(_vehicle(None), trip) is False


def test_the_resolver_applies_the_registration_check():
    source = _code_only(crud.resolve_owned_trip)

    assert "trip_predates_registration(" in source


# ---------------------------------------------------------------------------
# Unbounded aggregation
# ---------------------------------------------------------------------------


def test_heatmap_is_capped():
    assert 0 < crud.MAX_HEATMAP_CELLS <= 100_000

    source = _code_only(crud.get_heatmap_for_vehicle)
    assert "limit(MAX_HEATMAP_CELLS)" in source
    assert ".all()" in source


def test_heatmap_truncation_is_logged():
    """A silent cap reads as 'this is all the data', which for a heatmap is wrong."""
    source = _code_only(crud.get_heatmap_for_vehicle)

    assert "logger.warning" in source


# ---------------------------------------------------------------------------
# Deleting a vehicle's trips must leave nothing behind
# ---------------------------------------------------------------------------


def test_deletion_clears_the_map_regeneration_queue():
    """
    map_regeneration_queue keys on trip_id but has no foreign key, so the bulk
    delete on trips could not cascade into it — the rows stayed queued forever.
    """
    source = _code_only(crud.delete_all_trips_for_vehicle)

    assert "MapRegenerationQueue" in source

    queue = source.index("MapRegenerationQueue")
    trips = source.index("db.query(Trip).filter(Trip.vehicle_id == vehicle_id).delete")
    assert queue < trips, "the queue is cleared after the trips it references are gone"


def test_deletion_removes_the_rendered_map_previews():
    """
    The PNG *is* the route. Deleting the trip while leaving a picture of it on disk
    is not a deletion.
    """
    source = _code_only(crud.delete_all_trips_for_vehicle)

    assert "_remove_map_preview(" in source


def test_map_preview_removal_stays_inside_the_maps_directory():
    """map_preview_path comes from the database; treat it as a path, not as trusted."""
    source = _code_only(crud._remove_map_preview)

    assert ".name" in source, "the stored path is used without taking its basename"
    assert "resolve()" in source


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "/etc/passwd", "maps/../../x"])
def test_map_preview_removal_refuses_traversal(hostile, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAPS_STORAGE_PATH", tmp_path)
    outside = tmp_path.parent / "victim.txt"
    outside.write_text("keep me")

    crud._remove_map_preview(hostile)

    assert outside.exists(), "traversal in a stored path escaped the maps directory"


def test_map_preview_removal_deletes_a_real_preview(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAPS_STORAGE_PATH", tmp_path)
    preview = tmp_path / "CAR1_abc.png"
    preview.write_bytes(b"png")

    crud._remove_map_preview("maps/CAR1_abc.png")

    assert not preview.exists()


def test_missing_preview_file_is_not_an_error(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAPS_STORAGE_PATH", tmp_path)
    crud._remove_map_preview("maps/never_existed.png")  # must not raise


def test_gps_points_rely_on_a_database_level_cascade():
    """
    The bulk delete bypasses the ORM cascade, so the FK must carry ON DELETE CASCADE
    or every GPS point would be orphaned.
    """
    from app import models

    fk = list(models.GPSPoint.__table__.c.trip_id.foreign_keys)[0]
    assert fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# Follow-up 2026-08-07 — the registration guard covers every vehicle-scoped route
# ---------------------------------------------------------------------------
#
# trip_predates_registration() was only ever consulted by resolve_owned_trip(), so it
# protected the trip detail, the exports and the trip deletion. The listing, the
# search, the statistics, the heatmap and the map images answered for the *whole*
# history of a vehicle id — including whatever a previous holder recorded under it.
# The heatmap is the worst of them: it returns the raw positions, only rounded.


def test_registration_cutoff_returns_the_registration_time(monkeypatch):
    registered = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(crud, "get_vehicle_registration", lambda db, vid: _vehicle(registered))

    assert crud.registration_cutoff(None, "CAR1") == registered


def test_registration_cutoff_treats_a_naive_timestamp_as_utc(monkeypatch):
    monkeypatch.setattr(
        crud, "get_vehicle_registration",
        lambda db, vid: _vehicle(datetime.datetime(2026, 6, 1)),
    )

    cutoff = crud.registration_cutoff(None, "CAR1")

    assert cutoff == datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)


def test_registration_cutoff_is_none_without_a_created_at(monkeypatch):
    """Same rule as trip_predates_registration(): no cutoff rather than hidden history."""
    monkeypatch.setattr(crud, "get_vehicle_registration", lambda db, vid: _vehicle(None))

    assert crud.registration_cutoff(None, "CAR1") is None


def test_registration_cutoff_is_none_for_an_unknown_vehicle(monkeypatch):
    monkeypatch.setattr(crud, "get_vehicle_registration", lambda db, vid: None)

    assert crud.registration_cutoff(None, "CAR1") is None


def test_authorize_vehicle_returns_the_cutoff(monkeypatch):
    registered = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(crud, "check_vehicle_ownership", lambda db, user_id, vehicle_id: True)
    monkeypatch.setattr(crud, "registration_cutoff", lambda db, vehicle_id: registered)

    assert api._authorize_vehicle(None, "CAR1", 1, "test") == registered


def test_authorize_vehicle_refuses_a_foreign_vehicle(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(crud, "check_vehicle_ownership", lambda db, user_id, vehicle_id: False)

    with pytest.raises(HTTPException) as excinfo:
        api._authorize_vehicle(None, "CAR1", 1, "test")

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    "route",
    ["get_trips_for_vehicle", "get_trip_statistics", "get_vehicle_heatmap"],
)
def test_vehicle_scoped_routes_obtain_and_pass_the_cutoff(route):
    """
    A blanket check, not three individual ones: what failed here was not a wrong
    implementation but a route nobody connected to an existing rule. Authorization has
    to *hand over* the cutoff so a route cannot take the permission without the limit.
    """
    source = _code_only(getattr(api, route))

    assert "_authorize_vehicle(" in source, f"{route} authorizes by hand"
    assert "cutoff=cutoff" in source, f"{route} drops the registration cutoff"
    assert "check_vehicle_ownership(" not in source, (
        f"{route} still calls the ownership check directly, bypassing the cutoff"
    )


@pytest.mark.parametrize(
    "fn", [crud.get_trips_for_vehicle, crud.get_heatmap_for_vehicle, crud.get_trip_statistics]
)
def test_crud_accepts_a_cutoff(fn):
    assert "cutoff" in inspect.signature(fn).parameters


@pytest.mark.parametrize(
    "fn", [crud.get_trips_for_vehicle, crud.get_heatmap_for_vehicle]
)
def test_crud_applies_the_cutoff_to_the_query(fn):
    source = _code_only(fn)

    assert "Trip.start_time >= cutoff" in source


def test_statistics_fold_the_cutoff_into_the_lower_bound():
    """
    Four aggregations share one start_dt. Filtering each separately would leave the
    fifth one that gets added later unguarded.
    """
    source = _code_only(crud.get_trip_statistics)

    assert "max(start_dt_inclusive, cutoff)" in source
    assert source.index("cutoff") < source.index("_get_total_stats(")


def test_search_applies_a_cutoff_per_vehicle():
    """
    The search spans every vehicle the user owns. A single shared cutoff would let one
    vehicle's registration date decide what the others may show.
    """
    source = _code_only(crud.search_trips)

    assert "and_(Trip.vehicle_id == vid, Trip.start_time >= cutoff)" in source
    assert "owned_cutoffs" in source
    assert "Trip.vehicle_id.in_(owned_vehicle_ids)" not in source, (
        "the search is back to a flat id list and has lost the per-vehicle cutoff"
    )


def test_map_images_apply_the_registration_guard():
    """
    The rendered PNG is the route. Ownership of the id today says nothing about a trip
    recorded before this registration existed.
    """
    source = _code_only(api.get_map_image)

    assert "trip_predates_registration(" in source

    guard = source.index("trip_predates_registration(")
    served = source.index("FileResponse(")
    assert guard < served, "the map is served before the age guard runs"
