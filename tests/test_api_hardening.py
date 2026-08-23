"""
Regression tests for the remaining Karto findings.

H-8   SERVER_HOST defaulted to 0.0.0.0 and /docs was unauthenticated, so a default
      install published the whole trip API — and its schema — straight to the network.
H-10  Every inbound MQTT message spawned its own task and its own DB session, with no
      queue bound and no pool limits.
M-15  Subscriptions were only ever added, so switching trip tracking off kept Karto
      consuming and storing that vehicle's location until the next restart.
M-16  /maps authorization parsed the vehicle id out of the filename instead of looking
      the file up, making it depend on a naming rule enforced in another repository.
L1    `Query(..., enum=[...])` is not a thing in FastAPI; the relation parameter was
      never validated, and an unknown value silently dropped the spatial filter.
"""

import asyncio
import inspect

import pytest

from app import api
from app.config import Settings, settings


# --- H-8: safe defaults --------------------------------------------------------------

def test_bind_address_defaults_to_loopback():
    assert Settings.model_fields["SERVER_HOST"].default == "127.0.0.1"


def test_api_docs_are_disabled_by_default():
    assert Settings.model_fields["ENABLE_API_DOCS"].default is False


def test_docs_urls_are_gated_on_the_setting():
    import main

    if not settings.ENABLE_API_DOCS:
        assert main.app.docs_url is None
        assert main.app.redoc_url is None
        assert main.app.openapi_url is None


# --- M-16: map authorization ---------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "ABC123_3f2504e0-4f89-11d3-9a0c-0305e82c3301.png",
    "EV-1_00000000-0000-0000-0000-000000000000.png",
])
def test_valid_map_filenames_are_accepted(filename):
    assert api._MAP_FILENAME_RE.fullmatch(filename)


@pytest.mark.parametrize("filename", [
    "../karto.env",
    "../../karto.env",
    "ALICE_CAR_3f2504e0-4f89-11d3-9a0c-0305e82c3301.png",  # underscore in the vehicle id
    "ABC123_not-a-uuid.png",
    "ABC123_3f2504e0-4f89-11d3-9a0c-0305e82c3301.png.txt",
    "ABC123_3f2504e0-4f89-11d3-9a0c-0305e82c3301.svg",     # would render in our origin
    "abc123_3f2504e0-4f89-11d3-9a0c-0305e82c3301.png",     # ids are stored upper-case
])
def test_invalid_map_filenames_are_rejected(filename):
    assert api._MAP_FILENAME_RE.fullmatch(filename) is None


def test_map_endpoint_resolves_the_vehicle_from_the_database():
    """Authorization must not be derived by splitting the filename."""
    source = inspect.getsource(api.get_map_image)
    assert "get_trip_by_map_filename" in source
    assert "stem.split" not in source, "vehicle id must not be parsed out of the filename"


def test_map_endpoint_pins_the_content_type():
    source = inspect.getsource(api.get_map_image)
    assert 'media_type="image/png"' in source
    assert "nosniff" in source


# --- L1: the relation parameter is actually validated --------------------------------

def test_relation_parameter_is_a_literal():
    from typing import Literal, get_args, get_origin

    annotation = inspect.signature(api.search_trips_by_location).parameters["relation"].annotation
    assert get_origin(annotation) is Literal
    assert set(get_args(annotation)) == {
        "starts_within_radius", "ends_within_radius", "starts_within_bbox", "ends_within_bbox",
    }


# --- H-10: bounded queue and pool ----------------------------------------------------

def test_database_pool_is_bounded():
    from app import database

    source = inspect.getsource(database.init_db_engines)
    for kwarg in ("pool_size", "max_overflow", "pool_timeout"):
        assert kwarg in source, f"{kwarg} missing: an unbounded pool can exhaust the shared server"


def test_mqtt_queue_drops_instead_of_growing_without_bound():
    from app.mqtt_subscriber import MqttSubscriber

    subscriber = MqttSubscriber()

    async def scenario():
        subscriber._queue = asyncio.Queue(maxsize=2)

        async def noop():
            return None

        coros = [noop() for _ in range(5)]
        for coro in coros:
            subscriber._enqueue(coro)

        assert subscriber._queue.qsize() == 2, "queue grew past its bound"
        assert subscriber._dropped_messages == 3, "excess messages were not shed"

        # Drain so the accepted coroutines are awaited rather than left dangling.
        while not subscriber._queue.empty():
            await subscriber._queue.get_nowait()

    asyncio.run(scenario())


def test_mqtt_worker_survives_a_failing_message():
    """One bad message must not silently kill a worker and reduce throughput to zero."""
    from app.mqtt_subscriber import MqttSubscriber

    subscriber = MqttSubscriber()

    async def scenario():
        subscriber._queue = asyncio.Queue(maxsize=10)
        worker = asyncio.create_task(subscriber._worker(0))

        async def boom():
            raise RuntimeError("bad message")

        processed = asyncio.Event()

        async def ok():
            processed.set()

        subscriber._enqueue(boom())
        subscriber._enqueue(ok())

        await asyncio.wait_for(processed.wait(), timeout=5)
        assert not worker.done(), "worker died on a failing message"

        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


# --- M-15: withdrawal of consent takes effect ----------------------------------------

def test_subscriber_unsubscribes_from_disabled_vehicles():
    from app.mqtt_subscriber import MqttSubscriber

    source = inspect.getsource(MqttSubscriber._subscribe)
    assert "unsubscribe" in source, (
        "vehicles whose trip tracking was switched off must be unsubscribed, "
        "otherwise their location is still consumed until the next restart"
    )


# --- M-14: cookie failures are rate limited too --------------------------------------

def test_jwt_path_records_authentication_failures():
    from app import security

    source = inspect.getsource(security.get_current_user)
    jwt_branch = source[source.index("__Host-access_token"):]
    assert "record_failure_and_get_count" in jwt_branch, (
        "only the API-key branch counted failures; forged session tokens were unlimited"
    )


# --- LOW (2026-08-08): an authorization error must not be reported as an outage -------

def test_no_broad_handler_rewrites_an_http_error():
    """
    The finding, as a sweep over every route rather than the three that had it.

    A route that wraps its body in `except Exception` and answers 503 swallows the
    403 and 404 raised by the authorization helpers: the caller is told the service
    is down, and the IDOR attempt disappears from logs and monitoring — it is
    indistinguishable from a database hiccup. Any broad handler in this module must
    therefore re-raise HTTPException before it maps anything.
    """
    import ast

    source = inspect.getsource(api)
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            catches_everything = handler.type is None or (
                isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
            )
            if not catches_everything:
                continue

            body = ast.unparse(handler)
            reraises = (
                "raise e" in body
                or "raise exc" in body
                or "isinstance(e, HTTPException)" in body
            )
            # A sibling handler that catches HTTPException first is equally fine.
            guarded_earlier = any(
                isinstance(other.type, ast.Name) and other.type.id == "HTTPException"
                for other in node.handlers
                if other is not handler and isinstance(other.type, ast.Name)
            )
            raises_http = "HTTPException" in body

            if raises_http and not (reraises or guarded_earlier):
                offenders.append(f"line {handler.lineno}")

    assert not offenders, (
        "these turn an authorization failure into a generic error; re-raise "
        f"HTTPException first: {offenders}"
    )


def test_the_map_route_reraises_its_own_http_errors():
    """The concrete instance: /maps/{filename} answers 404 for a foreign file."""
    source = inspect.getsource(api.get_map_image)
    assert "isinstance(e, HTTPException)" in source or "except HTTPException" in source


def test_the_jwt_validator_reraises_http_errors():
    from app import security

    source = inspect.getsource(security)
    assert "except HTTPException as http_exc:" in source, (
        "a 401 for missing MFA would be rewritten into the generic credentials error"
    )
