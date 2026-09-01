import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, NamedTuple, Optional, Coroutine
from uuid import UUID

from geopy.distance import great_circle
from pydantic import BaseModel, Field

from . import crud
from .config import settings
from . import database
from .map_generator import generate_trip_map
from .timestamps import as_utc

logger = logging.getLogger(__name__)


class GpsLogPoint(NamedTuple):
    """A GPS point parsed from a buffered history record (data notification)."""
    timestamp: Optional[datetime]  # None if the record format carries no time of its own
    lat: float
    lon: float
    speed_kph: Optional[float] = None
    soc: Optional[float] = None
    altitude_m: Optional[float] = None


def _opt_float(fields: list, index: int) -> Optional[float]:
    try:
        return float(fields[index])
    except (IndexError, ValueError):
        return None


def _valid_latlon(lat: float, lon: float) -> bool:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)


def parse_xne_gps_log(payload: str) -> Optional[GpsLogPoint]:
    """
    NIU GT EVO (vehicle type NEVO):
      XNE-GPS-Log,<utc>,<expiry>,<lat>,<lon>,<gpsspeed_kph>,<course_deg>,<hdop>,
                  <satcount>,<speed_kph>,<odometer_km>,<soc>
    The record number (field 1) is the UTC timestamp of the fix.
    """
    fields = payload.split(',')
    if len(fields) < 5:
        return None
    try:
        ts = datetime.fromtimestamp(int(fields[1]), tz=timezone.utc)
        lat = float(fields[3])
        lon = float(fields[4])
    except (ValueError, OverflowError, OSError):
        return None
    if not _valid_latlon(lat, lon):
        return None
    # Reject records with an unsynced module clock or from the future
    now = datetime.now(timezone.utc)
    if ts > now + timedelta(minutes=5) or ts < datetime(2020, 1, 1, tzinfo=timezone.utc):
        return None

    # Vehicle speed (field 9) matches the semantics of v.p.speed; fall back to GPS speed
    speed = _opt_float(fields, 9)
    if speed is None:
        speed = _opt_float(fields, 5)

    return GpsLogPoint(timestamp=ts, lat=lat, lon=lon, speed_kph=speed, soc=_opt_float(fields, 11))


def parse_rt_gps_log(payload: str) -> Optional[GpsLogPoint]:
    """
    Renault Twizy (vehicle type RT):
      RT-GPS-Log,<odometer_mi/10>,<expiry>,<lat>,<lon>,<altitude_m>,<direction_deg>,
                 <speed_kph>,<gpslock>,...
    Carries no timestamp of its own - the record age from the MQTT topic is used instead.
    """
    fields = payload.split(',')
    if len(fields) < 9:
        return None
    try:
        lat = float(fields[3])
        lon = float(fields[4])
    except ValueError:
        return None
    if not _valid_latlon(lat, lon):
        return None
    if fields[8].strip() != '1':  # no GPS lock: the position is stale
        return None

    return GpsLogPoint(timestamp=None, lat=lat, lon=lon,
                       speed_kph=_opt_float(fields, 7), altitude_m=_opt_float(fields, 5))


# Known GPS history record formats, keyed by the record type (first CSV field of the
# payload). The delivery channel (notify/data/...) is generic OVMS framework, the record
# layout is a convention of the individual vehicle module - add new vehicles here.
GPS_LOG_PARSERS: Dict[str, Callable[[str], Optional[GpsLogPoint]]] = {
    'XNE-GPS-Log': parse_xne_gps_log,
    'RT-GPS-Log': parse_rt_gps_log,
}
class GPSPointInternal(BaseModel):
    timestamp: datetime
    lat: float
    lon: float
    speed_kph: Optional[float] = None
    altitude_m: Optional[float] = None

    def to_wkt(self) -> str:
        return f'POINT({self.lon} {self.lat})'
class VehicleState(BaseModel):
    is_driving: bool = False
    last_driving_update: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_trip_id: Optional[str] = None
    last_saved_gps_point: Optional[GPSPointInternal] = None

    latest_soc: Optional[float] = None
    latest_energy_kwh: Optional[float] = None
    trip_start_energy_kwh: Optional[float] = None
    latest_battery_capacity_kwh: Optional[float] = None

    pending_lat: Optional[float] = None
    pending_lon: Optional[float] = None
    pending_speed_kph: Optional[float] = None
    pending_altitude_m: Optional[float] = None
    pending_gpstime: Optional[datetime] = None

    # Monotonic clock readings of when the position and the timestamp were received.
    # A point is only built from the two if they arrived in the same transmit burst.
    pending_pos_received: Optional[float] = None
    pending_time_received: Optional[float] = None

    # Debounce bookkeeping for the burst currently being collected (monotonic clock)
    flush_deadline: Optional[float] = None
    burst_started: Optional[float] = None

    last_gps_update_time: Optional[datetime] = None
    last_gpslog_ts: Optional[datetime] = None
class TripTrackerService:
    def __init__(self):
        self._vehicle_states: Dict[str, VehicleState] = {}
        self._vehicle_locks: Dict[str, asyncio.Lock] = {}
        self._state_creation_lock = asyncio.Lock()
        # One in-flight debounce task per vehicle, see _arm_flush()
        self._flush_tasks: Dict[str, asyncio.Task] = {}

    async def _get_or_create_state(self, vehicle_id: str) -> VehicleState:
        if vehicle_id in self._vehicle_states:
            return self._vehicle_states[vehicle_id]

        async with self._state_creation_lock:
            if vehicle_id not in self._vehicle_states:
                logger.debug(f"Creating new vehicle state for {vehicle_id}")
                self._vehicle_states[vehicle_id] = VehicleState()
                self._vehicle_locks[vehicle_id] = asyncio.Lock()
            return self._vehicle_states[vehicle_id]

    async def _get_vehicle_lock(self, vehicle_id: str) -> asyncio.Lock:
        """Get the lock for a specific vehicle."""
        await self._get_or_create_state(vehicle_id)
        return self._vehicle_locks[vehicle_id]

    def process_message(self, vehicle_id: str, metric: str, payload: str) -> Optional[Coroutine]:
        if not payload:
            return None
        
        logger.debug(f"Processing message for {vehicle_id}: metric='{metric}', payload='{payload}'")

        if metric == 'v.e.on':
            is_driving = payload.lower() in ['1', 'true', 'yes']
            return self.handle_driving_state_change(vehicle_id, is_driving)
        elif metric.startswith('v.p.'):
            return self.handle_gps_update(vehicle_id, metric, payload)
        elif metric == 'm.time.utc':
            return self.handle_time_update(vehicle_id, payload)
        elif metric == 'v.b.soc':
            return self.handle_soc_update(vehicle_id, payload)
        elif metric == 'v.b.energy.used':
            return self.handle_energy_update(vehicle_id, payload)
        elif metric == 'v.b.capacity':
            return self.handle_battery_capacity_update(vehicle_id, payload)

        return None

    def process_data_notification(self, vehicle_id: str, payload: str, age_seconds: int = 0) -> Optional[Coroutine]:
        """
        Entry point for OVMS data notifications (topic notify/data/...). These are history
        records the module buffered while its server connection was down and delivers after
        the reconnect. Records whose type (first CSV field) is a known GPS log format are
        turned into GPS points with their historical time, closing the LTE gaps in the
        recorded route; other record types are ignored.
        `age_seconds` is the record age taken from the topic, used for formats that carry
        no timestamp of their own.
        """
        if not payload:
            return None
        record_type = payload.split(',', 1)[0]
        parser = GPS_LOG_PARSERS.get(record_type)
        if parser is None:
            logger.debug(f"Vehicle {vehicle_id}: ignoring data notification of unknown type '{record_type}'")
            return None
        return self.handle_gps_log_record(vehicle_id, payload, parser, age_seconds)

    async def handle_gps_log_record(self, vehicle_id: str, payload: str,
                                    parser: Callable[[str], Optional[GpsLogPoint]], age_seconds: int):
        point = parser(payload)
        if point is None:
            logger.warning(f"Vehicle {vehicle_id}: dropping unusable GPS log record: '{payload}'")
            return

        # Formats without an own timestamp (e.g. RT-GPS-Log) get it from the record age
        # the module publishes in the topic
        ts = point.timestamp
        if ts is None:
            ts = datetime.now(timezone.utc) - timedelta(seconds=max(0, age_seconds))
        lat, lon, speed_kph, soc = point.lat, point.lon, point.speed_kph, point.soc
        wkt = f'POINT({lon} {lat})'

        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            # Records are delivered in order (one at a time, acknowledged); skip replays
            if state.last_gpslog_ts is not None and ts <= state.last_gpslog_ts:
                return
            state.last_gpslog_ts = ts

            db = database.SessionLocal()
            try:
                trip = None
                if state.current_trip_id:
                    trip = crud.get_trip_by_id(db, UUID(state.current_trip_id))
                    if trip is None:
                        logger.warning(f"Trip {state.current_trip_id} no longer exists. Clearing trip state for vehicle {vehicle_id}.")
                        state.current_trip_id = None
                        state.last_saved_gps_point = None
                if trip is None:
                    # e.g. after a Karto restart the in-memory state is gone but the trip is not
                    trip = crud.get_in_progress_trip_by_vehicle(db, vehicle_id)

                if trip is not None:
                    self._insert_log_point_into_trip(db, trip, state, ts, point, wkt, vehicle_id)
                    return

                # No trip in progress: the record may belong to a trip that was already finalized
                trip = crud.get_completed_trip_covering(db, vehicle_id, ts, settings.KARTO_GPSLOG_COMPLETED_ATTACH_SLACK_SECONDS)
                if trip is not None:
                    if crud.trip_has_point_near(db, trip.id, ts, settings.KARTO_GPSLOG_DEDUPE_SECONDS):
                        return
                    crud.add_gps_point(db, trip.id, ts, wkt, speed_kph, point.altitude_m)
                    if soc is not None and trip.end_time and ts > trip.end_time:
                        trip.end_soc = soc
                    crud.refresh_trip_stats(db, trip)
                    crud.enqueue_map_generation(db, trip.id)
                    db.commit()
                    logger.info(f"Vehicle {vehicle_id}: late GPS log point at {ts.isoformat()} attached to completed trip {trip.id}, map regeneration queued.")
                    return

                if state.is_driving:
                    # Driving, but the live flow has no trip yet (e.g. the ride started inside
                    # an LTE dead zone): start the trip from the buffered record
                    new_trip = crud.create_trip(db, vehicle_id, ts, soc if soc is not None else state.latest_soc, wkt)
                    db.flush()
                    crud.add_gps_point(db, new_trip.id, ts, wkt, speed_kph, point.altitude_m)
                    db.commit()
                    state.current_trip_id = str(new_trip.id)
                    state.trip_start_energy_kwh = state.latest_energy_kwh
                    state.last_saved_gps_point = GPSPointInternal(lat=lat, lon=lon, speed_kph=speed_kph,
                                                                 altitude_m=point.altitude_m, timestamp=ts)
                    logger.info(f"TRIP STARTED: Vehicle {vehicle_id} started trip {new_trip.id} from a buffered GPS log record at {ts.isoformat()}.")
                    return

                logger.info(f"Vehicle {vehicle_id}: orphan GPS log record at {ts.isoformat()} (no matching trip), dropped.")
            except Exception as e:
                db.rollback()
                logger.error(f"Failed to process GPS log record for {vehicle_id}: {e}", exc_info=True)
            finally:
                db.close()

    def _insert_log_point_into_trip(self, db, trip, state: VehicleState, ts: datetime,
                                    point: GpsLogPoint, wkt: str, vehicle_id: str):
        """Inserts a buffered GPS log point into an in-progress trip, backdating the trip
        start if the ride began during the outage."""
        if ts >= trip.start_time:
            if crud.trip_has_point_near(db, trip.id, ts, settings.KARTO_GPSLOG_DEDUPE_SECONDS):
                return
            crud.add_gps_point(db, trip.id, ts, wkt, point.speed_kph, point.altitude_m)
            db.commit()
        elif (trip.start_time - ts).total_seconds() <= settings.KARTO_GPSLOG_BACKDATE_MAX_SECONDS:
            # The ride started while offline: move the trip start back to the buffered point
            crud.add_gps_point(db, trip.id, ts, wkt, point.speed_kph, point.altitude_m)
            trip.start_time = ts
            trip.start_location = wkt
            if point.soc is not None:
                trip.start_soc = point.soc
            db.commit()
            logger.info(f"Vehicle {vehicle_id}: trip {trip.id} start backdated to {ts.isoformat()} from a buffered GPS log record.")
        else:
            logger.info(f"Vehicle {vehicle_id}: GPS log record at {ts.isoformat()} predates trip {trip.id} start too far, dropped.")
            return

        # Keep the end-point candidate current so trip finalization uses the newest position
        if str(trip.id) == state.current_trip_id and \
                (state.last_saved_gps_point is None or ts > state.last_saved_gps_point.timestamp):
            state.last_saved_gps_point = GPSPointInternal(lat=point.lat, lon=point.lon, speed_kph=point.speed_kph,
                                                          altitude_m=point.altitude_m, timestamp=ts)

    async def handle_time_update(self, vehicle_id: str, payload: str):
        """
        Handles time updates from the m.time.utc metric. This is the timestamp source for
        live GPS points - but deliberately not the trigger that creates one.

        OVMS keeps its metric list sorted by name (ovms_metrics.cpp RegisterMetric inserts
        alphabetically) and both TransmitModifiedMetrics() and TransmitAllMetrics() walk
        exactly that list, so m.time.utc is published *before* v.p.latitude/v.p.longitude in
        every transmit pass. Committing the point here paired a fresh timestamp with the
        position of the previous pass: a systematic lag of one update interval (measured at
        5-6s), and after an LTE outage a jump back to the position the vehicle had when the
        connection dropped, because the reconnect burst re-sends m.time.utc first. Point
        creation therefore happens in _flush_pending_gps_point() once the burst is complete.

        v.p.gpstime would be the true fix time, but it is registered as DateLocal and is
        published as local wall clock plus a timezone abbreviation ("2026-08-06 09:36:22
        CEST"), which carries no usable UTC offset. m.time.utc is DateUTC and unambiguous.
        """
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            try:
                # Handle both ISO format and 'YYYY-MM-DD HH:MM:SS UTC' format
                cleaned_payload = payload.replace(' UTC', '').replace('Z', '+00:00')
                # A payload with no designator is UTC by protocol, not by local clock.
                dt_object = as_utc(datetime.fromisoformat(cleaned_payload))
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse UTC time payload '{payload}' for {vehicle_id}: {e}")
                return

            state.pending_gpstime = dt_object
            state.pending_time_received = time.monotonic()
            self._arm_flush(vehicle_id, state)

    async def handle_soc_update(self, vehicle_id: str, payload: str):
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)
            try:
                state.latest_soc = float(payload)
            except (ValueError, TypeError):
                logger.warning(f"Could not parse SOC value '{payload}' for {vehicle_id}")

    async def handle_energy_update(self, vehicle_id: str, payload: str):
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)
            try:
                state.latest_energy_kwh = float(payload)
                if state.current_trip_id is not None and state.trip_start_energy_kwh is None:
                    state.trip_start_energy_kwh = state.latest_energy_kwh
                    logger.debug(f"Vehicle {vehicle_id}: energy start captured mid-trip: {state.trip_start_energy_kwh:.3f} kWh")
                else:
                    logger.debug(f"Vehicle {vehicle_id}: energy update {state.latest_energy_kwh:.3f} kWh (trip_start={state.trip_start_energy_kwh})")
            except (ValueError, TypeError):
                logger.warning(f"Could not parse energy value '{payload}' for {vehicle_id}")

    async def handle_battery_capacity_update(self, vehicle_id: str, payload: str):
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)
            try:
                state.latest_battery_capacity_kwh = float(payload)
                logger.debug(f"Vehicle {vehicle_id}: battery capacity update {state.latest_battery_capacity_kwh:.1f} kWh")
            except (ValueError, TypeError):
                logger.warning(f"Could not parse battery capacity value '{payload}' for {vehicle_id}")

    async def handle_driving_state_change(self, vehicle_id: str, is_driving: bool):
        lock = await self._get_vehicle_lock(vehicle_id)
        trip_id_to_end = None
        event_timestamp = None

        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            event_timestamp = datetime.now(timezone.utc)

            if state.is_driving == is_driving:
                return

            logger.debug(f"Vehicle {vehicle_id}: Driving state changed to {is_driving} at {event_timestamp.isoformat()}.")

            state.is_driving = is_driving
            state.last_driving_update = event_timestamp

            if is_driving:
                ovms_db = database.OvmsSessionLocal()
                try:
                    if not crud.is_trip_tracking_enabled(ovms_db, vehicle_id):
                        logger.debug(f"Ignoring trip start for vehicle {vehicle_id}: Trip tracking is disabled in OVMS config.")
                        state.is_driving = False
                        return
                finally:
                    ovms_db.close()

            elif not is_driving and state.current_trip_id:
                trip_id_to_end = state.current_trip_id
                logger.debug(f"Vehicle {vehicle_id}: Driving stopped. Starting {settings.KARTO_TRIP_END_GRACE_PERIOD_SECONDS}s grace period for trip {trip_id_to_end}.")

        if trip_id_to_end:
            await asyncio.sleep(settings.KARTO_TRIP_END_GRACE_PERIOD_SECONDS)

            async with lock:
                state = await self._get_or_create_state(vehicle_id)
                if state.last_driving_update == event_timestamp:
                    logger.debug(f"Vehicle {vehicle_id}: Grace period ended. Finalizing trip {trip_id_to_end}.")
                    db = database.SessionLocal()
                    try:
                        trip = crud.get_trip_by_id(db, UUID(trip_id_to_end))
                        if trip and trip.status == 'in_progress' and state.last_saved_gps_point:
                            # The last point is already stored - re-adding it unconditionally
                            # put a duplicate final coordinate into every single track. Reuse
                            # what the trip actually ends with, which also picks up late GPS
                            # log records that arrived after the last live point.
                            end_point = crud.get_last_gps_point(db, trip.id)
                            if end_point is None or end_point.timestamp < state.last_saved_gps_point.timestamp:
                                end_point = crud.add_gps_point(db, trip.id, state.last_saved_gps_point.timestamp, state.last_saved_gps_point.to_wkt(), state.last_saved_gps_point.speed_kph, state.last_saved_gps_point.altitude_m)
                            energy_delta = None
                            if state.latest_energy_kwh is not None:
                                if state.trip_start_energy_kwh is None:
                                    # Metric arrived after trip start — treat as per-session counter (reset to 0 at trip start)
                                    energy_delta = state.latest_energy_kwh if state.latest_energy_kwh > 0 else None
                                    logger.debug(f"Vehicle {vehicle_id}: energy start not captured, using end value directly: {energy_delta} kWh")
                                else:
                                    delta = state.latest_energy_kwh - state.trip_start_energy_kwh
                                    if delta >= 0:
                                        energy_delta = delta
                                    elif state.latest_energy_kwh > 0:
                                        # Counter was reset mid-trip (e.g. session-based counter); use end value directly
                                        energy_delta = state.latest_energy_kwh
                                        logger.debug(f"Vehicle {vehicle_id}: energy counter reset detected (start={state.trip_start_energy_kwh:.3f}, end={state.latest_energy_kwh:.3f}), using end value")
                            if energy_delta is not None and state.latest_battery_capacity_kwh is not None:
                                if energy_delta > state.latest_battery_capacity_kwh:
                                    logger.warning(
                                        f"Vehicle {vehicle_id}: energy_delta={energy_delta:.1f} kWh exceeds battery "
                                        f"capacity {state.latest_battery_capacity_kwh:.1f} kWh — discarding (stale counter). "
                                        f"trip_start={state.trip_start_energy_kwh}, latest={state.latest_energy_kwh}"
                                    )
                                    energy_delta = None
                            logger.debug(f"Vehicle {vehicle_id}: final energy_delta={energy_delta}, battery_capacity={state.latest_battery_capacity_kwh}")
                            is_trip_valid = crud.update_trip_on_completion(db, trip, end_point.timestamp, state.latest_soc, end_point, energy_delta, battery_capacity_kwh=state.latest_battery_capacity_kwh)
                            if is_trip_valid:
                                task = asyncio.create_task(generate_trip_map(trip.id))
                                task.add_done_callback(lambda t: logger.error(f"Map generation failed for trip {trip.id}: {t.exception()}") if t.exception() else None)
                    finally:
                        db.close()

                    state.current_trip_id = None
                    state.last_saved_gps_point = None
                    state.trip_start_energy_kwh = None
                else:
                    logger.debug(f"Vehicle {vehicle_id}: Trip-end for event at {event_timestamp.isoformat()} was cancelled by a newer event.")


    async def handle_gps_update(self, vehicle_id: str, metric: str, payload: str):
        """
        Handles GPS metrics by updating the pending state. It does not create the point
        itself - that happens in _flush_pending_gps_point() once the transmit burst these
        metrics belong to has been delivered completely.
        """
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            try:
                if metric == 'v.p.latitude':
                    state.pending_lat = float(payload)
                    state.pending_pos_received = time.monotonic()
                elif metric == 'v.p.longitude':
                    state.pending_lon = float(payload)
                    state.pending_pos_received = time.monotonic()
                elif metric == 'v.p.speed': state.pending_speed_kph = float(payload)
                elif metric == 'v.p.altitude': state.pending_altitude_m = float(payload)
                else: return
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse payload '{payload}' for metric '{metric}': {e}")
                return

            self._arm_flush(vehicle_id, state)

    def _arm_flush(self, vehicle_id: str, state: VehicleState) -> None:
        """
        (Re)schedules the pending point flush. Must be called with the vehicle lock held.

        Every metric of a transmit pass pushes the deadline back, so the point is built
        from the complete burst rather than from whichever metric happened to arrive
        first. That also removes the dependency on the publish order altogether, which
        matters twice over: OVMS uses a different order in TransmitPriorityMetrics()
        (m.time.utc last) than in TransmitModifiedMetrics() (alphabetical, m.time.utc
        first), and the MQTT workers here process messages concurrently anyway.
        """
        now = time.monotonic()
        if state.burst_started is None:
            state.burst_started = now
        state.flush_deadline = min(
            now + settings.KARTO_GPS_BATCH_DEBOUNCE_SECONDS,
            state.burst_started + settings.KARTO_GPS_BATCH_MAX_WINDOW_SECONDS,
        )

        task = self._flush_tasks.get(vehicle_id)
        if task is None or task.done():
            self._flush_tasks[vehicle_id] = asyncio.create_task(
                self._flush_after_burst(vehicle_id), name=f"gps-flush-{vehicle_id}"
            )

    async def _flush_after_burst(self, vehicle_id: str) -> None:
        """Waits out the debounce window, then builds the point for this vehicle."""
        lock = await self._get_vehicle_lock(vehicle_id)
        while True:
            async with lock:
                state = await self._get_or_create_state(vehicle_id)
                remaining = (state.flush_deadline or 0.0) - time.monotonic()
                if remaining <= 0:
                    # Release our slot before doing the work, so a metric arriving after
                    # the flush starts a fresh burst instead of being swallowed.
                    if self._flush_tasks.get(vehicle_id) is asyncio.current_task():
                        del self._flush_tasks[vehicle_id]
                    state.flush_deadline = None
                    state.burst_started = None
                    await self._flush_pending_gps_point(vehicle_id, state)
                    return
            await asyncio.sleep(remaining)

    async def cancel_pending_flushes(self) -> None:
        """Cancels the outstanding debounce tasks on shutdown."""
        tasks = list(self._flush_tasks.values())
        self._flush_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _flush_pending_gps_point(self, vehicle_id: str, state: VehicleState):
        """
        Builds one GPS point from the metrics collected during the last transmit burst.
        Requires latitude, longitude and a timestamp; altitude and speed are optional.
        Must be called with the vehicle lock held; clears the pending state on success.
        """
        if not all([state.pending_lat is not None, state.pending_lon is not None, state.pending_gpstime is not None]):
            logger.debug(f"Vehicle {vehicle_id}: metric burst complete, but missing lat/lon or time to create point.")
            return

        # Position and timestamp have to come from the same transmit pass. When they do
        # not, the position is a leftover from an earlier one - the case that matters is
        # the reconnect after an LTE outage, where the burst can carry m.time.utc without
        # a fresh position and the point would otherwise be placed where the vehicle was
        # when the connection dropped, kilometres back.
        skew = abs((state.pending_pos_received or 0.0) - (state.pending_time_received or 0.0))
        if skew > settings.KARTO_GPS_PAIR_MAX_SKEW_SECONDS:
            logger.debug(
                f"Vehicle {vehicle_id}: discarding GPS point, position and timestamp are "
                f"{skew:.1f}s apart (max {settings.KARTO_GPS_PAIR_MAX_SKEW_SECONDS}s)."
            )
            return

        now = datetime.now(timezone.utc)
        if state.last_gps_update_time is not None:
            time_since_last_update = (now - state.last_gps_update_time).total_seconds()
            if time_since_last_update < settings.KARTO_GPS_UPDATE_MIN_INTERVAL_SECONDS:
                logger.debug(f"Vehicle {vehicle_id}: GPS update rate limited ({time_since_last_update:.2f}s < {settings.KARTO_GPS_UPDATE_MIN_INTERVAL_SECONDS}s)")
                return
        state.last_gps_update_time = now

        current_gps_point = GPSPointInternal(
            lat=state.pending_lat, lon=state.pending_lon,
            speed_kph=state.pending_speed_kph,
            altitude_m=state.pending_altitude_m,
            timestamp=state.pending_gpstime
        )
        state.pending_lat, state.pending_lon, state.pending_speed_kph, state.pending_altitude_m, state.pending_gpstime = None, None, None, None, None
        state.pending_pos_received, state.pending_time_received = None, None

        if state.is_driving and not state.current_trip_id:
            logger.debug(f"Vehicle {vehicle_id}: First GPS point received while driving. Creating new trip.")
            db = database.SessionLocal()
            try:
                trip = crud.create_trip(db, vehicle_id, current_gps_point.timestamp, state.latest_soc, current_gps_point.to_wkt())
                db.flush()
                crud.add_gps_point(db, trip.id, current_gps_point.timestamp, current_gps_point.to_wkt(), current_gps_point.speed_kph, current_gps_point.altitude_m)
                db.commit()
                state.current_trip_id = str(trip.id)
                state.trip_start_energy_kwh = state.latest_energy_kwh
                state.last_saved_gps_point = current_gps_point
                logger.debug(f"TRIP STARTED: Vehicle {vehicle_id} started new trip {trip.id}.")
            finally:
                db.close()
            return

        if state.current_trip_id:
            should_save = False
            if not state.last_saved_gps_point:
                should_save = True
            else:
                speed = current_gps_point.speed_kph or 0.0
                distance = great_circle((state.last_saved_gps_point.lat, state.last_saved_gps_point.lon), (current_gps_point.lat, current_gps_point.lon)).meters
                if speed >= settings.KARTO_GPS_MIN_SPEED_KPH or distance >= settings.KARTO_GPS_MIN_DISTANCE_METERS:
                    should_save = True

            if should_save:
                db = database.SessionLocal()
                try:
                    # Verify trip still exists before adding points (could have been reaped)
                    trip = crud.get_trip_by_id(db, UUID(state.current_trip_id))
                    if not trip:
                        logger.warning(f"Trip {state.current_trip_id} no longer exists (likely reaped). Clearing trip state for vehicle {vehicle_id}.")
                        state.current_trip_id = None
                        state.last_saved_gps_point = None
                    else:
                        crud.add_gps_point(db, UUID(state.current_trip_id), current_gps_point.timestamp, current_gps_point.to_wkt(), current_gps_point.speed_kph, current_gps_point.altitude_m)
                        db.commit()
                        state.last_saved_gps_point = current_gps_point
                except Exception as e:
                    # Check if it's a foreign key violation (trip was deleted between check and insert)
                    if "ForeignKeyViolation" in str(type(e)) or "gps_points_trip_id_fkey" in str(e):
                        logger.warning(f"Trip {state.current_trip_id} was deleted during GPS point insertion. Clearing trip state for vehicle {vehicle_id}.")
                        state.current_trip_id = None
                        state.last_saved_gps_point = None
                    else:
                        logger.error(f"Failed to add GPS point for trip {state.current_trip_id}: {e}", exc_info=True)
                    db.rollback()
                finally:
                    db.close()
        else:
            state.last_saved_gps_point = current_gps_point

trip_tracker_service = TripTrackerService()