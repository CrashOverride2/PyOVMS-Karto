"""
Every stored instant is normalised the same way, in one place.

Ported from the main server's tests/test_cross_dialect_sql.py and
tests/test_api_timestamps.py, and worth having here for a reason specific to Karto:
this service reads two databases with different timestamp shapes. Its own is
PostgreSQL, so `timestamptz` comes back tz-aware in the session's TimeZone; the OVMS
database it also reads is whatever that deployment chose, and the default is SQLite,
which has no timezone type and returns naive values.

Both shapes reach the same code. The two ways of getting it wrong are symmetrical and
both were live here:

  * `replace(tzinfo=utc)` on an *aware* value moves the instant by the session offset.
    That is an API key honoured hours past expiry (security.py) and a trip attributed
    to the wrong vehicle registration (crud.py).
  * `strftime('...Z')` on an unconverted value writes a local wall clock and labels it
    UTC — a GPX file that states an instant it does not mean.

as_utc() is the answer to both; the static guards at the bottom are what keep it applied.
"""

import ast
import datetime
import pathlib

import pytest

from app.timestamps import UtcDatetime, as_utc

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

NAIVE = datetime.datetime(2026, 3, 4, 5, 6, 7, 123456)  # noqa: DTZ001 - the SQLite shape
AWARE = NAIVE.replace(tzinfo=datetime.timezone.utc)
BERLIN = NAIVE.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=2)))


# --- the normalisation itself -----------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    # Naive: a stored UTC value whose label went missing. Attach it.
    (NAIVE, AWARE),
    (AWARE, AWARE),
    # Aware: convert, never relabel. 07:06+02:00 is 05:06 UTC, and a relabel would
    # have called it 07:06 UTC — two hours of a key's life, in the wrong direction.
    (BERLIN.replace(hour=7), AWARE),
])
def test_as_utc_labels_the_naive_and_converts_the_aware(value, expected):
    assert as_utc(value) == expected
    assert as_utc(value).tzinfo is datetime.timezone.utc


def test_as_utc_passes_none_through():
    """An absent timestamp is not a time, and must not become the epoch."""
    assert as_utc(None) is None


def test_a_trip_response_carries_its_zone():
    from pydantic import BaseModel

    class _Trip(BaseModel):
        start_time: UtcDatetime

    assert '"start_time":"2026-03-04T05:06:07.123456Z"' in _Trip(start_time=NAIVE).model_dump_json()


# --- nothing may relabel an aware value ------------------------------------------

def _tzinfo_replacements():
    """Every `X.replace(tzinfo=...)` in app/, with what it was called on."""
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "replace"
                    and any(kw.arg == "tzinfo" for kw in node.keywords)):
                yield path.relative_to(REPO_ROOT), node.lineno, node.func.value


def test_only_freshly_built_datetimes_get_a_tzinfo_stamped_on():
    """
    `replace(tzinfo=utc)` is correct for a value this code just constructed — a day
    boundary from datetime.combine(), a sentinel literal — and wrong for anything read
    from a database, where an aware value would be moved rather than labelled.

    The receiver tells them apart: a Call is something built here, an attribute or a
    name is a value that came from somewhere else. Those belong in as_utc().
    """
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, receiver in _tzinfo_replacements()
        if not isinstance(receiver, ast.Call)
        and path.name != "timestamps.py"
    ]

    assert offenders == [], (
        "these stamp a tzinfo onto a value they did not construct; if it is already "
        "aware the instant moves by the session's UTC offset. Use as_utc(): "
        + ", ".join(offenders)
    )


# --- a written zone designator is a claim, and has to be true ---------------------

def _zone_asserting_strftime_calls():
    """Every `X.strftime(fmt)` in app/ whose fmt states which zone the value is in."""
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "strftime"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            fmt = node.args[0].value
            if fmt.endswith("Z") or "UTC" in fmt:
                yield path.relative_to(REPO_ROOT), node.lineno, node.func.value


def _is_already_utc(receiver) -> bool:
    if not isinstance(receiver, ast.Call):
        return False
    if isinstance(receiver.func, ast.Name) and receiver.func.id == "as_utc":
        return True
    return isinstance(receiver.func, ast.Attribute) and receiver.func.attr in {"now", "astimezone"}


def test_every_written_zone_designator_was_actually_converted():
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, receiver in _zone_asserting_strftime_calls()
        if not _is_already_utc(receiver)
    ]

    assert offenders == [], (
        "these write a zone designator onto a value they never converted, so on a "
        "PostgreSQL session that is not UTC they label a local wall clock as UTC: "
        + ", ".join(offenders)
    )


# --- the connection the whole thing rests on --------------------------------------

@pytest.mark.parametrize("url", ["postgresql://u@h/d", "postgresql+psycopg2://u@h/d"])
def test_postgresql_sessions_are_pinned_to_utc(url):
    from app.database import connect_args_for

    assert connect_args_for(url) == {"options": "-c timezone=UTC"}


def test_a_sqlite_ovms_database_gets_the_cross_thread_argument_instead():
    """OVMS_DATABASE_URL need not be PostgreSQL — that server defaults to SQLite."""
    from app.database import connect_args_for

    assert connect_args_for("sqlite:///./ovms_py.db") == {"check_same_thread": False}


# --- guards whose subject must not silently become empty --------------------------

def test_the_scans_still_find_what_they_guard():
    assert len(list(_tzinfo_replacements())) >= 3
    assert len(list(_zone_asserting_strftime_calls())) >= 3
