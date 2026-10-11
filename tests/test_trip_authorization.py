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
import asyncio
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


@pytest.mark.parametrize("raised", ["TripNotFound", "TripNotAuthorized"])
def test_a_trip_not_there_is_answered_with_a_code(monkeypatch, raised):
    """
    The same machine-readable detail for both, so the app can tell this handler's 404
    from an unrouted path or a proxy's and count a delete answered with it as done.
    """
    from fastapi import HTTPException

    def resolve(*args, **kwargs):
        raise getattr(crud, raised)()

    monkeypatch.setattr(crud, "resolve_owned_trip", resolve)
    with pytest.raises(HTTPException) as refused:
        api._authorize_trip(None, None, "trip", 1)
    assert (refused.value.status_code, refused.value.detail) == (404, "trip_not_found")


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
    trips = source.index("db.query(Trip).filter(*selected).delete")
    assert queue < trips, "the queue is cleared after the trips it references are gone"


def test_deletion_removes_the_rendered_map_previews():
    """
    The PNG *is* the route. Deleting the trip while leaving a picture of it on disk
    is not a deletion.
    """
    source = _code_only(crud.delete_all_trips_for_vehicle)

    assert "_remove_map_preview(" in source
    # Both variants: a light twin left behind is just as much a picture of the route.
    assert "map_preview_path_light" in source
    assert source.count("_remove_map_preview(") == 2


def test_single_trip_deletion_removes_both_preview_variants():
    """
    The same rule as the bulk delete, on the route that deletes one trip. This one kept
    unlinking map_preview_path only after the light twin was added, so the second image
    of the route survived the deletion.
    """
    source = _code_only(api.delete_trip)

    assert "map_preview_path_light" in source
    assert source.count("_remove_map_preview(") >= 1, (
        "the route deletes by hand instead of using the helper that stays inside the "
        "maps directory"
    )


def test_the_orphan_sweep_accounts_for_both_variants():
    """
    crud.get_referenced_map_filenames drives a deletion, so a variant it forgets is a
    file the sweep would remove while a trip is still showing it.
    """
    source = _code_only(crud.get_referenced_map_filenames)

    assert "map_preview_path" in source
    assert "map_preview_path_light" in source


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
    ["get_trips_for_vehicle", "get_trip_statistics", "get_vehicle_heatmap", "get_current_trip"],
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
    "fn", [crud.get_trips_for_vehicle, crud.get_heatmap_for_vehicle, crud.get_trip_statistics,
           crud.get_current_trip_for_vehicle]
)
def test_crud_accepts_a_cutoff(fn):
    assert "cutoff" in inspect.signature(fn).parameters


@pytest.mark.parametrize(
    "fn", [crud.get_trips_for_vehicle, crud.get_heatmap_for_vehicle, crud.get_current_trip_for_vehicle]
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


# ---------------------------------------------------------------------------
# 2026-10-09 — admins have no access to other users' trips
# ---------------------------------------------------------------------------
#
# check_vehicle_ownership() let any admin through, so every admin could read every
# vehicle's movement profile: list, running trip, detail, exports, statistics, heatmap,
# map images. The main server's support mode (an owner grants an admin temporary access
# to a vehicle) deliberately excludes trips. The one administrative path left is the bulk
# delete, which the main server calls with the acting user's internal key when it deletes
# a vehicle or an account — the actor is often an admin.
#
# These run against a real (SQLite) OVMS database, so the rule itself is exercised, not
# a patched stand-in for it. Karto's own data layer is replaced by a sentinel: reaching
# it means authorization let the caller through.

from unittest.mock import MagicMock
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models_ovms

OWNER, ADMIN, INACTIVE_ADMIN, STRANGER = 1, 2, 3, 4
TRIP_ID = UUID("12345678-1234-1234-1234-123456789abc")
REGISTERED = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class _Reached(Exception):
    """Karto's data layer was asked for the vehicle's trips."""


def _reached(*args, **kwargs):
    raise _Reached()


@pytest.fixture
def ovms_db():
    # One connection for every thread: the bulk delete authorizes in the threadpool, and
    # an in-memory database is otherwise a new, empty one per thread.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models_ovms.OvmsBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for uid, name, is_admin, is_active in (
        (OWNER, "owner", False, True),
        (ADMIN, "admin", True, True),
        (INACTIVE_ADMIN, "former-admin", True, False),
        (STRANGER, "stranger", False, True),
    ):
        session.add(models_ovms.User(id=uid, username=name, hashed_password="x",
                                     is_admin=is_admin, is_active=is_active))
    session.add(models_ovms.Vehicle(vehicle_id="CAR1", owner_id=OWNER, created_at=REGISTERED))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _user(uid):
    return SimpleNamespace(id=uid)


@pytest.mark.parametrize("uid, expected", [
    (OWNER, True), (ADMIN, False), (INACTIVE_ADMIN, False), (STRANGER, False),
])
def test_ownership_is_the_owner_alone(ovms_db, uid, expected):
    assert crud.check_vehicle_ownership(ovms_db, user_id=uid, vehicle_id="CAR1") is expected


def _deletion_key(uid, vehicle_id="CAR1", lifetime=datetime.timedelta(minutes=1), name=None):
    """The key the main server mints in trigger_karto_vehicle_deletion()."""
    created = datetime.datetime.now(datetime.timezone.utc)
    return models_ovms.ApiKey(
        user_id=uid, hashed_key="h", is_active=True,
        name=name if name is not None else f"temp-karto-delete-{vehicle_id}-{created.timestamp()}",
        created_at=created, expires_at=created + lifetime if lifetime is not None else None,
    )


def _personal_key(uid):
    return models_ovms.ApiKey(user_id=uid, hashed_key="h", is_active=True, name="my laptop",
                              created_at=datetime.datetime.now(datetime.timezone.utc))


@pytest.mark.parametrize("uid, key, expected", [
    (OWNER, None, True),                      # the app's "delete all trips", by cookie
    (OWNER, "personal", True),
    (ADMIN, "deletion", True),                # the main server deleting vehicle/account
    (ADMIN, None, False),                     # an admin's session cookie
    (ADMIN, "personal", False),               # an admin's own API key
    (INACTIVE_ADMIN, "deletion", False),
    (STRANGER, "deletion", False),
    (STRANGER, None, False),
])
def test_administration_is_the_owner_or_an_admin_with_the_deletion_key(ovms_db, uid, key, expected):
    api_key = {"deletion": _deletion_key(uid), "personal": _personal_key(uid), None: None}[key]
    assert crud.check_vehicle_administration(
        ovms_db, user_id=uid, vehicle_id="CAR1", api_key=api_key) is expected


@pytest.mark.parametrize("key", [
    _deletion_key(ADMIN, vehicle_id="CAR2"),                           # another vehicle
    _deletion_key(ADMIN, vehicle_id="CAR1-X"),                         # prefix of another id
    _deletion_key(ADMIN, lifetime=datetime.timedelta(days=30)),        # not short-lived
    _deletion_key(ADMIN, lifetime=None),                               # never expires
    _deletion_key(ADMIN, name="temp-karto-delete-CAR1"),               # no timestamp
    _deletion_key(OWNER),                                              # someone else's key
], ids=["other-vehicle", "id-prefix", "long-lived", "no-expiry", "no-timestamp", "other-user"])
def test_admin_needs_this_vehicles_short_lived_deletion_key(ovms_db, key):
    assert crud.check_vehicle_administration(
        ovms_db, user_id=ADMIN, vehicle_id="CAR1", api_key=key) is False


def test_deletion_key_matches_the_vehicle_id_case_insensitively(ovms_db):
    assert crud.check_vehicle_administration(
        ovms_db, user_id=ADMIN, vehicle_id="CAR1", api_key=_deletion_key(ADMIN, vehicle_id="car1")) is True


def test_no_one_holds_an_unknown_vehicle(ovms_db):
    assert crud.check_vehicle_ownership(ovms_db, user_id=ADMIN, vehicle_id="NOPE") is False
    assert crud.check_vehicle_administration(
        ovms_db, user_id=ADMIN, vehicle_id="NOPE", api_key=_deletion_key(ADMIN, vehicle_id="NOPE")) is False


_VEHICLE_ROUTES = {
    "list": ("get_trips_for_vehicle", "get_trips_for_vehicle", dict(page=1, limit=5)),
    "current": ("get_current_trip", "get_current_trip_for_vehicle", {}),
    "stats": ("get_trip_statistics", "get_trip_statistics",
              dict(start_date=None, end_date=None, daily_limit=14)),
    "heatmap": ("get_vehicle_heatmap", "get_heatmap_for_vehicle", dict(grid_size_m=100.0)),
}


def _call_vehicle_route(monkeypatch, ovms_db, name, uid):
    route, data_fn, kwargs = _VEHICLE_ROUTES[name]
    monkeypatch.setattr(crud, data_fn, _reached)
    return getattr(api, route)("car1", db=None, ovms_db=ovms_db, current_user=_user(uid), **kwargs)


@pytest.mark.parametrize("name", sorted(_VEHICLE_ROUTES))
def test_owner_reaches_the_vehicles_trips(monkeypatch, ovms_db, name):
    with pytest.raises(_Reached):
        _call_vehicle_route(monkeypatch, ovms_db, name, OWNER)


@pytest.mark.parametrize("uid", [ADMIN, STRANGER])
@pytest.mark.parametrize("name", sorted(_VEHICLE_ROUTES))
def test_admin_and_stranger_are_refused_the_vehicles_trips(monkeypatch, ovms_db, name, uid):
    with pytest.raises(HTTPException) as refused:
        _call_vehicle_route(monkeypatch, ovms_db, name, uid)
    assert refused.value.status_code == 403


def _karto_db_with_trip(**fields):
    trip = SimpleNamespace(
        id=TRIP_ID, vehicle_id="CAR1", status="completed",
        start_time=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
        map_preview_path=None, map_preview_path_light=None, **fields,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = trip
    return db, trip


_TRIP_ROUTES = {
    "detail": ("get_trip_details", "get_trip_with_points", dict(simplify_tolerance=None)),
    "delete": ("delete_trip", "delete_trip", {}),
    "gpx": ("get_trip_gpx", "get_trip_points_for_export", {}),
    "kml": ("get_trip_kml", "get_trip_points_for_export", {}),
}


def _call_trip_route(monkeypatch, ovms_db, name, uid):
    route, data_fn, kwargs = _TRIP_ROUTES[name]
    monkeypatch.setattr(crud, data_fn, _reached)
    db, _ = _karto_db_with_trip()
    return getattr(api, route)(TRIP_ID, db=db, ovms_db=ovms_db, current_user=_user(uid), **kwargs)


@pytest.mark.parametrize("name", sorted(_TRIP_ROUTES))
def test_owner_reaches_the_trip(monkeypatch, ovms_db, name):
    with pytest.raises(_Reached):
        _call_trip_route(monkeypatch, ovms_db, name, OWNER)


@pytest.mark.parametrize("uid", [ADMIN, STRANGER])
@pytest.mark.parametrize("name", sorted(_TRIP_ROUTES))
def test_admin_and_stranger_do_not_see_the_trip(monkeypatch, ovms_db, name, uid):
    """The same 404 as for a trip that does not exist — no admin exception, no oracle."""
    with pytest.raises(HTTPException) as refused:
        _call_trip_route(monkeypatch, ovms_db, name, uid)
    assert (refused.value.status_code, refused.value.detail) == (404, "trip_not_found")


def _serve_map(monkeypatch, tmp_path, ovms_db, uid):
    from app.config import settings

    filename = f"CAR1_{TRIP_ID}.png"
    (tmp_path / filename).write_bytes(b"png")
    monkeypatch.setattr(settings, "MAPS_STORAGE_PATH", tmp_path)
    _, trip = _karto_db_with_trip()
    monkeypatch.setattr(crud, "get_trip_by_map_filename", lambda db, name: trip)
    return api.get_map_image(filename, db=None, ovms_db=ovms_db, current_user=_user(uid))


def test_owner_gets_the_map_image(monkeypatch, tmp_path, ovms_db):
    from fastapi.responses import FileResponse

    assert isinstance(_serve_map(monkeypatch, tmp_path, ovms_db, OWNER), FileResponse)


@pytest.mark.parametrize("uid", [ADMIN, STRANGER])
def test_admin_and_stranger_do_not_get_the_map_image(monkeypatch, tmp_path, ovms_db, uid):
    with pytest.raises(HTTPException) as refused:
        _serve_map(monkeypatch, tmp_path, ovms_db, uid)
    assert refused.value.status_code == 404


def test_search_finds_nothing_of_another_users_vehicle_for_an_admin(ovms_db):
    """The search scopes by owner_id in its own query; it never had the bypass. Pinned."""
    assert crud.search_trips(None, ovms_db, user_id=ADMIN, vehicle_id="CAR1",
                             lat=49.0, lon=8.0, radius_m=100.0) == ([], 0)


def _bulk_delete(monkeypatch, ovms_db, uid, api_key=None):
    deleted = []
    monkeypatch.setattr(
        crud, "delete_all_trips_for_vehicle",
        lambda db, vehicle_id, include_in_progress: deleted.append(vehicle_id) or 0,
    )
    request = SimpleNamespace(state=SimpleNamespace(api_key=api_key))
    response = asyncio.run(api.delete_all_trips_for_vehicle(
        request, "car1", include_in_progress=True, db=None, ovms_db=ovms_db, current_user=_user(uid)
    ))
    return response, deleted


@pytest.mark.parametrize("uid, api_key", [(OWNER, None), (ADMIN, _deletion_key(ADMIN))],
                         ids=["owner-cookie", "admin-deletion-key"])
def test_owner_and_admin_can_bulk_delete_a_vehicles_trips(monkeypatch, ovms_db, uid, api_key):
    """
    The main server deletes a vehicle's trips — vehicle deletion, account deletion,
    lifecycle housekeeping — with an internal key of the acting user, often an admin.
    """
    response, deleted = _bulk_delete(monkeypatch, ovms_db, uid, api_key)
    assert response.status_code == 204
    assert deleted == ["CAR1"]


@pytest.mark.parametrize("uid, api_key", [
    (ADMIN, None), (ADMIN, _personal_key(ADMIN)),
    (INACTIVE_ADMIN, _deletion_key(INACTIVE_ADMIN)), (STRANGER, _deletion_key(STRANGER)),
], ids=["admin-cookie", "admin-personal-key", "inactive-admin", "stranger"])
def test_bulk_delete_is_refused_to_anyone_else(monkeypatch, ovms_db, uid, api_key):
    with pytest.raises(HTTPException) as refused:
        _bulk_delete(monkeypatch, ovms_db, uid, api_key)
    assert refused.value.status_code == 403


def test_only_the_bulk_delete_uses_the_administrative_check():
    """
    check_vehicle_administration() admits admins. Any other route calling it would bring
    back the read access this change took away.
    """
    callers = [
        name for name, fn in inspect.getmembers(api, inspect.isfunction)
        if fn.__module__ == api.__name__ and "check_vehicle_administration(" in _code_only(fn)
    ]
    assert callers == ["delete_all_trips_for_vehicle"]
    assert "check_vehicle_administration(" not in _code_only(crud.resolve_owned_trip)


@pytest.mark.parametrize("key_name, expected_status", [
    (None, 204),            # the main server's deletion key for CAR1
    ("my laptop", 403),     # the same admin's personal key
])
def test_bulk_delete_sees_the_key_the_request_authenticated_with(
        monkeypatch, ovms_db, key_name, expected_status):
    """
    Through the real get_current_user: the key row has to reach the route, or every
    admin deletion by the main server is refused (and vehicle deletion stalls).
    """
    import asyncio
    import hashlib

    from app import security

    key = _deletion_key(ADMIN)
    if key_name is not None:
        key.name = key_name
    key.hashed_key = hashlib.sha256(b"secret-key").hexdigest()
    ovms_db.add(key)
    ovms_db.commit()

    monkeypatch.setattr(security.ban_manager, "is_banned", lambda db, ip: False)
    monkeypatch.setattr(security.failure_tracker, "clear_failures", lambda db, ip: None)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), state=SimpleNamespace(), cookies={})

    user = asyncio.run(security.get_current_user(request, api_key="secret-key", ovms_db=ovms_db, karto_db=None))
    assert user.id == ADMIN

    monkeypatch.setattr(crud, "delete_all_trips_for_vehicle", lambda db, vehicle_id, include_in_progress: 0)
    try:
        response = asyncio.run(api.delete_all_trips_for_vehicle(
            request, "car1", include_in_progress=True, db=None, ovms_db=ovms_db, current_user=user))
        status_code = response.status_code
    except HTTPException as refused:
        status_code = refused.status_code
    assert status_code == expected_status
