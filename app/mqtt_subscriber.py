import asyncio
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import settings
from . import crud, database
from .trip_tracker import trip_tracker_service

logger = logging.getLogger(__name__)

_METRIC_SUFFIXES = [
    "metric/v/e/on",
    "metric/v/b/soc",
    "metric/v/b/energy/used",
    "metric/v/b/capacity",
    "metric/v/p/latitude",
    "metric/v/p/longitude",
    "metric/v/p/speed",
    "metric/v/p/altitude",
    "metric/v/p/gpslock",
    "metric/m/time/utc",
]

# OVMS data notifications: history records the module buffered during an LTE outage,
# published on notify/data/<subtype...>/<msg_id>/<-age_seconds>. The GPS log records in
# there (XNE-GPS-Log, RT-GPS-Log, XSQ-GPS-Log, ...) are dispatched by their record type in the trip
# tracker; other record types are ignored. Subscribed with QoS 2 so the broker queues
# them for us across Karto restarts (requires the persistent session set up in connect()).
_DATA_NOTIFY_SUFFIX = "notify/data/#"

class MqttSubscriber:
    def __init__(self):
        self._client: mqtt.Client = None
        self._is_connected = False
        self._loop: asyncio.AbstractEventLoop = None
        self._subscribed_vehicles: set[tuple[str, str]] = set()
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._dropped_messages = 0

    async def start_workers(self) -> None:
        """Create the queue and worker tasks. Must run on the service's event loop."""
        if self._queue is not None:
            return
        self._queue = asyncio.Queue(maxsize=settings.MQTT_MAX_QUEUED_MESSAGES)
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"mqtt-worker-{i}")
            for i in range(settings.MQTT_WORKER_COUNT)
        ]
        logger.info(
            "Started %d MQTT worker(s) with a queue capacity of %d.",
            settings.MQTT_WORKER_COUNT, settings.MQTT_MAX_QUEUED_MESSAGES,
        )

    async def stop_workers(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queue = None

    async def _worker(self, index: int) -> None:
        while True:
            coro = await self._queue.get()
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad message must never take a worker down; the queue would then
                # silently lose throughput until nothing is processed at all.
                logger.exception("MQTT worker %d failed while processing a message", index)
            finally:
                self._queue.task_done()

    def connect(self, loop: asyncio.AbstractEventLoop):
        if not all([settings.MQTT_BROKER_HOST, settings.MQTT_USER, settings.MQTT_PASSWORD]):
            logger.error("MQTT client cannot connect: Host or credentials not configured.")
            return

        self._loop = loop
        self._client = mqtt.Client(client_id="karto_service_subscriber", clean_session=False)
        self._client.username_pw_set(settings.MQTT_USER, settings.MQTT_PASSWORD)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe

        logger.info(f"Connecting to MQTT broker at {settings.MQTT_BROKER_HOST}...")
        try:
            self._client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, 60)
            self._client.loop_start()
        except Exception as e:
            logger.error(f"MQTT client failed to connect: {e}", exc_info=True)

    def _query_enabled_vehicles(self) -> list[tuple[str, str]]:
        if database.OvmsSessionLocal is None:
            logger.warning("OVMS database not ready, cannot query tracking-enabled vehicles.")
            return []
        try:
            with database.OvmsSessionLocal() as db:
                vehicles = crud.get_tracking_enabled_vehicles(db)
            logger.debug(f"Trip tracking enabled for {len(vehicles)} vehicle(s):")
            for owner, vehicle_id in vehicles:
                logger.debug(f"  owner={owner}  vehicle={vehicle_id}")
            return vehicles
        except Exception as e:
            logger.error(f"Failed to query tracking-enabled vehicles: {e}", exc_info=True)
            return []

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._is_connected = True
            logger.info("MQTT client connected successfully.")
            # On (re)connect the broker forgets our subscriptions, so start fresh.
            self._subscribed_vehicles.clear()
            self._subscribe()
        else:
            self._is_connected = False
            logger.error(f"MQTT connection failed with code {rc}")

    def _subscribe(self):
        """Subscribe to topics for any enabled vehicle not yet subscribed."""
        if not self._is_connected or self._client is None:
            return

        all_vehicles = self._query_enabled_vehicles()
        enabled = set(all_vehicles)
        new_vehicles = [(o, v) for o, v in all_vehicles if (o, v) not in self._subscribed_vehicles]

        stale = [pair for pair in self._subscribed_vehicles if pair not in enabled]
        for owner, vehicle_id in stale:
            topics = [f"ovms/{owner}/{vehicle_id}/{suffix}" for suffix in _METRIC_SUFFIXES]
            topics.append(f"ovms/{owner}/{vehicle_id}/{_DATA_NOTIFY_SUFFIX}")
            result, _mid = self._client.unsubscribe(topics)
            if result != 0:
                logger.error(
                    "MQTT unsubscribe failed for owner=%s vehicle=%s (code %s); will retry.",
                    owner, vehicle_id, result,
                )
                continue
            self._subscribed_vehicles.discard((owner, vehicle_id))
            logger.info("MQTT unsubscribed: owner=%s vehicle=%s (tracking disabled)", owner, vehicle_id)

        if not new_vehicles:
            logger.debug("MQTT subscriptions up-to-date, no new vehicles to subscribe.")
            return

        topics = [
            (f"ovms/{owner}/{vehicle_id}/{suffix}", 0)
            for owner, vehicle_id in new_vehicles
            for suffix in _METRIC_SUFFIXES
        ] + [
            (f"ovms/{owner}/{vehicle_id}/{_DATA_NOTIFY_SUFFIX}", 2)
            for owner, vehicle_id in new_vehicles
        ]

        result, mid = self._client.subscribe(topics)
        if result != 0:
            logger.error(f"MQTT subscribe call failed with code {result}")
        else:
            for owner, vehicle_id in new_vehicles:
                self._subscribed_vehicles.add((owner, vehicle_id))
                logger.info(f"MQTT subscribed: owner={owner}  vehicle={vehicle_id}")
            logger.debug(f"MQTT subscribe request sent for {len(topics)} topics (mid={mid}), awaiting broker confirmation.")

    def refresh_subscriptions(self):
        """Check for newly-enabled vehicles and subscribe to their topics."""
        self._subscribe()

    def _is_known_pair(self, owner: str, vehicle_id: str) -> bool:
        """
        True if (owner, vehicle_id) is a pair we deliberately subscribed to.

        Usernames are compared verbatim — the main server validates them against
        ^[a-zA-Z0-9_-]+$ and stores them as typed. Vehicle ids are compared
        upper-cased because that is how they are normalised everywhere else.
        """
        return any(
            sub_owner == owner and sub_vehicle.upper() == vehicle_id
            for sub_owner, sub_vehicle in self._subscribed_vehicles
        )

    def _on_disconnect(self, client, userdata, rc, properties=None):
        self._is_connected = False
        logger.warning(f"MQTT client disconnected with code {rc}. Will attempt to reconnect.")

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        logger.info(f"MQTT broker confirmed subscription (mid={mid}): granted QoS={granted_qos}")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode().strip()
            parts = topic.split('/')

            if len(parts) < 5 or parts[0] != 'ovms':
                logger.debug(f"Ignoring message on unexpected topic structure: {topic}")
                return

            topic_owner = parts[1]
            vehicle_id = parts[2].upper()

            if not self._is_known_pair(topic_owner, vehicle_id):
                logger.warning(
                    "Dropping message on %s: vehicle %s is not subscribed for owner '%s'.",
                    topic, vehicle_id, topic_owner,
                )
                return

            if parts[3] == 'metric':
                metric_name = ".".join(parts[4:])
                coro = trip_tracker_service.process_message(vehicle_id, metric_name, payload)
            elif len(parts) >= 8 and parts[3] == 'notify' and parts[4] == 'data':
                # ovms/<owner>/<vehicle>/notify/data/<subtype...>/<msg_id>/<-age_seconds>
                # The last segment is the record age (published as a negative offset), used
                # for record formats that carry no timestamp of their own.
                try:
                    age_seconds = -int(parts[-1])
                except ValueError:
                    age_seconds = 0
                coro = trip_tracker_service.process_data_notification(
                    vehicle_id, payload, age_seconds, received_at=datetime.now(timezone.utc))
            else:
                logger.debug(f"Ignoring message on unexpected topic structure: {topic}")
                return

            if coro and self._loop:
                self._loop.call_soon_threadsafe(self._enqueue, coro)

        except Exception as e:
            logger.error(f"Error processing MQTT message on topic {topic}: {e}", exc_info=True)

    def _enqueue(self, coro) -> None:
        """Hand a message to the workers, dropping it if the backlog is full."""
        if self._queue is None:
            coro.close()
            return
        try:
            self._queue.put_nowait(coro)
        except asyncio.QueueFull:
            coro.close()
            self._dropped_messages += 1
            if self._dropped_messages % 100 == 1:
                logger.warning(
                    "MQTT queue full (%d slots); dropped %d message(s) so far. A vehicle "
                    "is publishing faster than the service can persist.",
                    settings.MQTT_MAX_QUEUED_MESSAGES, self._dropped_messages,
                )

    def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.critical("MQTT client disconnected.")

mqtt_subscriber = MqttSubscriber()
