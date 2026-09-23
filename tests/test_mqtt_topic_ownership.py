"""
The MQTT subscriber must bind an inbound topic to the owner it names.

Finding N-4 of the 2026-08-02 audit. The subscriber took the vehicle from the third
topic segment and ignored the owner segment entirely — the same pattern that was
closed in the main server's two subscribers as C-1.

It is defence in depth here rather than a live hole, because we subscribe to explicit
ovms/{owner}/{vehicle}/… topics instead of wildcards. But the ACL the main server
generates for this account grants wildcard reads (topic read ovms/+/+/metric/…), so
the only thing keeping foreign traffic out is the subscription list. These tests exist
so that widening it later — an easy thing to do while simplifying reconnect handling —
fails loudly instead of silently allowing another user's GPS trace to be attributed to
one of our vehicles.
"""

import app.mqtt_subscriber as mqtt_subscriber_module
from app.mqtt_subscriber import MqttSubscriber


class _Msg:
    def __init__(self, topic: str, payload: bytes = b"1"):
        self.topic = topic
        self.payload = payload


def _subscriber_with(pairs):
    sub = MqttSubscriber()
    sub._subscribed_vehicles = set(pairs)
    return sub


def _captured_dispatches(monkeypatch, subscriber):
    """Record every (vehicle_id, …) the subscriber tries to hand to the trip tracker."""
    seen = []

    class FakeTracker:
        def process_message(self, vehicle_id, metric_name, payload):
            seen.append(("metric", vehicle_id, metric_name))
            return None

        def process_data_notification(self, vehicle_id, payload, age_seconds, received_at=None):
            seen.append(("data", vehicle_id, age_seconds))
            return None

    monkeypatch.setattr(mqtt_subscriber_module, "trip_tracker_service", FakeTracker())
    return seen


def test_metric_for_the_correct_owner_is_processed(monkeypatch):
    sub = _subscriber_with([("alice", "ALICECAR")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/alice/ALICECAR/metric/v/p/latitude"))

    assert seen == [("metric", "ALICECAR", "v.p.latitude")]


def test_metric_naming_a_foreign_owner_is_dropped(monkeypatch):
    """The cross-tenant case: mallory's prefix, alice's vehicle id."""
    sub = _subscriber_with([("alice", "ALICECAR"), ("mallory", "MALCAR")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/mallory/ALICECAR/metric/v/p/latitude"))

    assert seen == [], "a foreign owner segment must not be attributed to ALICECAR"


def test_data_notification_naming_a_foreign_owner_is_dropped(monkeypatch):
    """GPS log records arrive on notify/data and carry position history too."""
    sub = _subscriber_with([("alice", "ALICECAR"), ("mallory", "MALCAR")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/mallory/ALICECAR/notify/data/XNE-GPS-Log/1/-30"))

    assert seen == []


def test_data_notification_for_the_correct_owner_is_processed(monkeypatch):
    sub = _subscriber_with([("alice", "ALICECAR")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/alice/ALICECAR/notify/data/XNE-GPS-Log/1/-30"))

    assert seen == [("data", "ALICECAR", 30)]


def test_unsubscribed_vehicle_is_dropped(monkeypatch):
    """
    Covers the withdrawal case: once a vehicle is unsubscribed because tracking was
    switched off, in-flight or retained messages for it must not still be stored.
    """
    sub = _subscriber_with([])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/alice/ALICECAR/metric/v/p/latitude"))

    assert seen == []


def test_vehicle_id_case_does_not_defeat_the_check(monkeypatch):
    """Vehicle ids are normalised upper-case; the pair lookup must match regardless."""
    sub = _subscriber_with([("alice", "alicecar")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/alice/AliceCar/metric/v/b/soc"))

    assert seen == [("metric", "ALICECAR", "v.b.soc")]


def test_owner_comparison_is_exact(monkeypatch):
    """Usernames are stored verbatim; a near-miss must not pass."""
    sub = _subscriber_with([("alice", "ALICECAR")])
    seen = _captured_dispatches(monkeypatch, sub)

    sub._on_message(None, None, _Msg("ovms/Alice/ALICECAR/metric/v/b/soc"))

    assert seen == []
