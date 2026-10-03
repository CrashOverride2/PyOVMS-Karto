import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, NamedTuple, Optional, Coroutine, Tuple
from uuid import UUID

from geopy.distance import great_circle
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

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
    # Seconds since the position last changed, for formats that report it (XSQ-GPS-Log)
    fix_age_s: Optional[float] = None


# Upper bound for the record age taken from the topic. The modules buffer their records in
# RAM and cap the queue (the NIU at 2000 records), so a larger value is a corrupt topic -
# and an absurd one would not even survive the datetime arithmetic.
_GPS_LOG_MAX_AGE_SECONDS = 7 * 86400


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

    # The module writes 0 while no battery is detected - that is no reading, and taken as one
    # it would turn the trip's SoC usage into the whole charge
    soc = _opt_float(fields, 11)
    if soc is not None and soc <= 0:
        soc = None

    return GpsLogPoint(timestamp=ts, lat=lat, lon=lon, speed_kph=speed, soc=soc)


def _parse_untimed_gps_log(payload: str) -> Optional[GpsLogPoint]:
    """
    Shared layout of the GPS logs that follow the V2 history record convention:
      <type>,<odometer>,<expiry>,<lat>,<lon>,<altitude_m>,<direction_deg>,<speed_kph>,<gpslock>,...
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


def parse_rt_gps_log(payload: str) -> Optional[GpsLogPoint]:
    """
    Renault Twizy (vehicle type RT):
      RT-GPS-Log,<odometer_mi/10>,<expiry>,<lat>,<lon>,<altitude_m>,<direction_deg>,
                 <speed_kph>,<gpslock>,...
    """
    return _parse_untimed_gps_log(payload)


def parse_xsq_gps_log(payload: str) -> Optional[GpsLogPoint]:
    """
    smart EQ fortwo/forfour 453 (vehicle type SQ):
      XSQ-GPS-Log,<odometer_km/10>,<expiry>,<lat>,<lon>,<altitude_m>,<heading_deg>,
                  <speed_kph>,<gpslock>,<latitude_age_s>,<net_sq>,<bat_power_kw>,
                  <bat_energy_used_kwh>,<bat_energy_recd_kwh>,<bat_current_a>
    Same layout as RT-GPS-Log up to the GPS lock. Only sent while the car is on.
    latitude_age_s is the age of the latitude metric, i.e. how long ago the position last
    changed - not the age of the fix itself, see _is_stale_log_fix().
    """
    point = _parse_untimed_gps_log(payload)
    if point is None:
        return None
    return point._replace(fix_age_s=_opt_float(payload.split(','), 9))


# Known GPS history record formats, keyed by the record type (first CSV field of the
# payload). The delivery channel (notify/data/...) is generic OVMS framework, the record
# layout is a convention of the individual vehicle module - add new vehicles here.
GPS_LOG_PARSERS: Dict[str, Callable[[str], Optional[GpsLogPoint]]] = {
    'XNE-GPS-Log': parse_xne_gps_log,
    'RT-GPS-Log': parse_rt_gps_log,
    'XSQ-GPS-Log': parse_xsq_gps_log,
}
class GPSPointInternal(BaseModel):
    timestamp: datetime
    lat: float
    lon: float
    speed_kph: Optional[float] = None
    altitude_m: Optional[float] = None

    def to_wkt(self) -> str:
        return f'POINT({self.lon} {self.lat})'


def _passes_point_filter(reference: GPSPointInternal, lat: float, lon: float,
                         speed_kph: Optional[float]) -> bool:
    """
    The speed/distance filter shared by live points and GPS log records: a point is kept if
    the vehicle is moving (KARTO_GPS_MIN_SPEED_KPH) or has moved away from the reference, the
    last kept point (KARTO_GPS_MIN_DISTANCE_METERS). Below both it is noise from a vehicle
    standing still - except for the first such point after a moving one. That one marks
    where the vehicle came to a halt: at a traffic light, and above all at the destination,
    which would otherwise end up to the distance threshold short of where it parked.
    """
    min_speed = settings.KARTO_GPS_MIN_SPEED_KPH
    if (speed_kph or 0.0) >= min_speed:
        return True
    if (reference.speed_kph or 0.0) >= min_speed:
        return True
    distance = great_circle((reference.lat, reference.lon), (lat, lon)).meters
    return distance >= settings.KARTO_GPS_MIN_DISTANCE_METERS

class VehicleState(BaseModel):
    is_driving: bool = False
    # Whether this run has seen a v.e.on for the vehicle yet. The first one after a start is
    # the retained value, and it is the moment to deal with a trip the previous run left
    # open, see handle_driving_state_change().
    driving_reported: bool = False
    last_driving_update: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_trip_id: Optional[str] = None
    last_saved_gps_point: Optional[GPSPointInternal] = None

    # Set while the state holds a trip the previous run left open and that is being
    # ended because the vehicle reported off (_adopt_open_trip_for_ending()): the time of
    # the trip's newest stored point. A restart receives the retained position with a
    # fresh m.time.utc, which would otherwise stretch the finished ride to the restart
    # and draw a line to wherever the car is parked now — so no live point is added to
    # such a trip. Cleared by v.e.on=1, see _continue_or_end_inherited_trip().
    inherited_trip_last_seen: Optional[datetime] = None

    latest_soc: Optional[float] = None
    latest_energy_kwh: Optional[float] = None
    trip_start_energy_kwh: Optional[float] = None
    # The trip was taken over from the database without a baseline (opened before the
    # baseline was stored, or by a run that never saw the counter). Its energy is unknown:
    # the counter read now covers more than the trip, and a baseline taken now covers
    # less. See _energy_delta().
    energy_baseline_unknown: bool = False
    latest_battery_capacity_kwh: Optional[float] = None

    pending_lat: Optional[float] = None
    pending_lon: Optional[float] = None
    pending_speed_kph: Optional[float] = None
    pending_altitude_m: Optional[float] = None
    pending_gpstime: Optional[datetime] = None

    # Last v.p.gpslock value, None until the module published one (not every vehicle does).
    # Kept across bursts: OVMS only re-sends a metric when it changes.
    gps_lock: Optional[bool] = None
    # Position of the last burst that came without a GPS lock, see _flush_pending_gps_point()
    last_unlocked_position: Optional[Tuple[float, float]] = None

    # Monotonic clock readings of when the position and the timestamp were received.
    # A point is only built from the two if they arrived in the same transmit burst.
    pending_pos_received: Optional[float] = None
    pending_time_received: Optional[float] = None

    # Debounce bookkeeping for the burst currently being collected (monotonic clock)
    flush_deadline: Optional[float] = None
    burst_started: Optional[float] = None

    last_gps_update_time: Optional[datetime] = None
    last_gpslog_ts: Optional[datetime] = None
    # Last GPS log point that passed the speed/distance filter, see _is_redundant_log_point()
    last_gpslog_point: Optional[GPSPointInternal] = None
class TripTrackerService:
    def __init__(self):
        self._vehicle_states: Dict[str, VehicleState] = {}
        self._vehicle_locks: Dict[str, asyncio.Lock] = {}
        self._state_creation_lock = asyncio.Lock()
        # One in-flight debounce task per vehicle, see _arm_flush()
        self._flush_tasks: Dict[str, asyncio.Task] = {}
        # The trip end waiting out its grace period, one per vehicle, see
        # _schedule_trip_end(). Held here so it is not garbage-collected mid-wait, can be
        # replaced by a newer one and cancelled on shutdown.
        self._trip_end_tasks: Dict[str, asyncio.Task] = {}

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

    def process_data_notification(self, vehicle_id: str, payload: str, age_seconds: int = 0,
                                  received_at: Optional[datetime] = None) -> Optional[Coroutine]:
        """
        Entry point for OVMS data notifications (topic notify/data/...). These are history
        records the module buffered while its server connection was down and delivers after
        the reconnect. Records whose type (first CSV field) is a known GPS log format are
        turned into GPS points with their historical time, closing the LTE gaps in the
        recorded route; other record types are ignored.
        `age_seconds` is the record age taken from the topic, used for formats that carry
        no timestamp of their own. It counts from `received_at`, the moment the MQTT client
        got the message - not from whenever a worker gets round to the record.
        """
        if not payload:
            return None
        record_type = payload.split(',', 1)[0]
        parser = GPS_LOG_PARSERS.get(record_type)
        if parser is None:
            logger.debug(f"Vehicle {vehicle_id}: ignoring data notification of unknown type '{record_type}'")
            return None
        return self.handle_gps_log_record(vehicle_id, payload, parser, age_seconds, received_at)

    async def handle_gps_log_record(self, vehicle_id: str, payload: str,
                                    parser: Callable[[str], Optional[GpsLogPoint]], age_seconds: int,
                                    received_at: Optional[datetime] = None):
        point = parser(payload)
        if point is None:
            logger.warning(f"Vehicle {vehicle_id}: dropping unusable GPS log record: '{payload}'")
            return

        # Formats without an own timestamp (RT-GPS-Log, XSQ-GPS-Log) get it from the record age
        # the module publishes in the topic. Counted from the receive time: a reconnect burst
        # can keep records waiting in the MQTT queue, which would otherwise shift each of them
        # by however long it waited there.
        ts = point.timestamp
        if ts is None:
            age = max(0, age_seconds)
            if age > _GPS_LOG_MAX_AGE_SECONDS:
                logger.warning(f"Vehicle {vehicle_id}: dropping GPS log record with implausible age {age_seconds}s: '{payload}'")
                return
            ts = (received_at or datetime.now(timezone.utc)) - timedelta(seconds=age)

        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            # Records are delivered in order (one at a time, acknowledged); skip replays
            previous_record_ts = state.last_gpslog_ts
            if previous_record_ts is not None and ts <= previous_record_ts:
                return
            state.last_gpslog_ts = ts

            if self._is_stale_log_fix(point):
                logger.debug(f"Vehicle {vehicle_id}: GPS log record at {ts.isoformat()} repeats a position "
                             f"{point.fix_age_s:.0f}s old while moving, dropped.")
                return
            if self._is_redundant_log_point(state, previous_record_ts, ts, point):
                return

            db = database.SessionLocal()
            try:
                in_track = self._store_log_point(db, state, ts, point, vehicle_id)
            except Exception as e:
                db.rollback()
                in_track = False
                logger.error(f"Failed to process GPS log record for {vehicle_id}: {e}", exc_info=True)
            finally:
                db.close()

            # Only a point that is part of a track may become the filter reference. One that
            # was dropped (orphan, too old, database error) would otherwise suppress the
            # standstill records after it, although none of them made it into a track either.
            if in_track:
                state.last_gpslog_point = GPSPointInternal(lat=point.lat, lon=point.lon, speed_kph=point.speed_kph,
                                                           altitude_m=point.altitude_m, timestamp=ts)

    def _store_log_point(self, db, state: VehicleState, ts: datetime, point: GpsLogPoint,
                         vehicle_id: str) -> bool:
        """
        Puts a GPS log point into the trip it belongs to. Returns whether the track now has
        a point at that time - stored here, or already there (de-duplication).
        """
        wkt = f'POINT({point.lon} {point.lat})'
        soc = point.soc

        trip = None
        if state.current_trip_id:
            trip = crud.get_trip_by_id(db, UUID(state.current_trip_id))
            if trip is None:
                logger.warning(f"Trip {state.current_trip_id} no longer exists. Clearing trip state for vehicle {vehicle_id}.")
                self._clear_trip_state(state)
        if trip is None:
            # e.g. after a Karto restart the in-memory state is gone but the trip is not
            trip = crud.get_in_progress_trip_by_vehicle(db, vehicle_id)
            if trip is not None and not self._log_point_continues_open_trip(db, trip, ts, vehicle_id):
                trip = None

        if trip is not None:
            return self._insert_log_point_into_trip(db, trip, state, ts, point, wkt, vehicle_id)

        # No trip in progress: the record may belong to a trip that was already finalized
        trip = crud.get_completed_trip_covering(db, vehicle_id, ts, settings.KARTO_GPSLOG_COMPLETED_ATTACH_SLACK_SECONDS)
        if trip is not None:
            if crud.trip_has_point_near(db, trip.id, ts, settings.KARTO_GPSLOG_DEDUPE_SECONDS):
                return True
            crud.add_gps_point(db, trip.id, ts, wkt, point.speed_kph, point.altitude_m)
            if soc is not None and trip.end_time and ts > trip.end_time:
                trip.end_soc = soc
            crud.refresh_trip_stats(db, trip)
            crud.enqueue_map_generation(db, trip.id)
            db.commit()
            logger.info(f"Vehicle {vehicle_id}: late GPS log point at {ts.isoformat()} attached to completed trip {trip.id}, map regeneration queued.")
            return True

        if state.is_driving:
            # Driving, but the live flow has no trip yet (e.g. the ride started inside
            # an LTE dead zone): start the trip from the buffered record
            new_trip = self._create_trip_or_adopt(db, vehicle_id, state, ts,
                                                  soc if soc is not None else state.latest_soc, wkt)
            if new_trip is None:
                trip = crud.get_trip_by_id(db, UUID(state.current_trip_id))
                return self._insert_log_point_into_trip(db, trip, state, ts, point, wkt, vehicle_id)
            crud.add_gps_point(db, new_trip.id, ts, wkt, point.speed_kph, point.altitude_m)
            db.commit()
            state.current_trip_id = str(new_trip.id)
            state.trip_start_energy_kwh = state.latest_energy_kwh
            state.last_saved_gps_point = GPSPointInternal(lat=point.lat, lon=point.lon, speed_kph=point.speed_kph,
                                                         altitude_m=point.altitude_m, timestamp=ts)
            logger.info(f"TRIP STARTED: Vehicle {vehicle_id} started trip {new_trip.id} from a buffered GPS log record at {ts.isoformat()}.")
            return True

        logger.info(f"Vehicle {vehicle_id}: orphan GPS log record at {ts.isoformat()} (no matching trip), dropped.")
        return False

    def _log_point_continues_open_trip(self, db, trip, ts: datetime, vehicle_id: str) -> bool:
        """
        Whether a GPS log record may go into an open trip this run does not track — the
        resume path's gap rule, applied to the buffered records. After a restart the QoS 2
        queue can hold the records of a ride that began long after the open trip last
        moved: folded in, two rides became one with a line across whatever lay between,
        and since the reaper finalizes rather than deletes, it stayed that way. Past
        KARTO_TRIP_RESUME_MAX_GAP_SECONDS the trip is an earlier ride whose end was
        missed: finalized at its last point without readings, as the reaper would, and
        the record goes wherever it would have gone with no trip open.
        """
        last = crud.get_last_point_position(db, trip.id)
        last_ts = last[0] if last is not None else as_utc(trip.start_time)
        gap = (ts - last_ts).total_seconds()
        if gap <= settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS:
            return True
        logger.info(f"Vehicle {vehicle_id}: GPS log record at {ts.isoformat()} is {gap:.0f}s past the last point "
                    f"of open trip {trip.id}, finalizing that trip rather than extending it.")
        self._finalize_trip(db, vehicle_id, trip, None)
        return False

    @staticmethod
    def _is_stale_log_fix(point: GpsLogPoint) -> bool:
        """
        XSQ-GPS-Log records carry how long ago the position last changed. While the vehicle
        moves that is a second or two; much longer means the GPS lost its fix (tunnel,
        garage) and the record repeats an old position under a new time. At a standstill a
        large age is normal - the position simply does not change - so it only counts while
        moving.
        """
        if point.fix_age_s is None:
            return False
        return ((point.speed_kph or 0.0) >= settings.KARTO_GPS_MIN_SPEED_KPH
                and point.fix_age_s > settings.KARTO_GPSLOG_MAX_FIX_AGE_SECONDS)

    @staticmethod
    def _is_redundant_log_point(state: VehicleState, previous_record_ts: Optional[datetime],
                                ts: datetime, point: GpsLogPoint) -> bool:
        """
        The speed/distance filter of the live flow (_passes_point_filter()), applied to GPS
        log records. Some modules (e.g. the smart EQ) also send a record while standing still
        whenever a battery value changes, which would otherwise pile up points at every
        traffic light.
        Because records arrive in order, the reference is simply the last record that made
        it into a track, kept in memory - no database lookup. A pause in the record stream
        means the vehicle was off in between, so the next record starts afresh instead of
        being compared with where the previous ride ended.
        """
        reference = state.last_gpslog_point
        if reference is None or previous_record_ts is None:
            return False
        if (ts - previous_record_ts).total_seconds() > settings.KARTO_GPSLOG_FILTER_RESET_SECONDS:
            return False
        return not _passes_point_filter(reference, point.lat, point.lon, point.speed_kph)

    def _insert_log_point_into_trip(self, db, trip, state: VehicleState, ts: datetime,
                                    point: GpsLogPoint, wkt: str, vehicle_id: str) -> bool:
        """Inserts a buffered GPS log point into an in-progress trip, backdating the trip
        start if the ride began during the outage. Returns whether the track has the point."""
        if ts >= trip.start_time:
            if crud.trip_has_point_near(db, trip.id, ts, settings.KARTO_GPSLOG_DEDUPE_SECONDS):
                return True
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
            return False

        # Keep the end-point candidate current so trip finalization uses the newest position
        if str(trip.id) == state.current_trip_id and \
                (state.last_saved_gps_point is None or ts > state.last_saved_gps_point.timestamp):
            state.last_saved_gps_point = GPSPointInternal(lat=point.lat, lon=point.lon, speed_kph=point.speed_kph,
                                                          altitude_m=point.altitude_m, timestamp=ts)
        return True

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
                soc = float(payload)
            except (ValueError, TypeError):
                logger.warning(f"Could not parse SOC value '{payload}' for {vehicle_id}")
                return
            if soc <= 0:
                # Some modules publish 0 for "no reading" - the NIU GT EVO whenever no battery
                # is detected, e.g. once it has been taken out for charging, typically within
                # the trip-end grace period. Keep the last real value rather than closing the
                # trip at 0 %. A vehicle genuinely run down to 0 % ends at its last reading.
                logger.debug(f"Vehicle {vehicle_id}: ignoring SOC {soc}, keeping {state.latest_soc}")
                return
            state.latest_soc = soc

    async def handle_energy_update(self, vehicle_id: str, payload: str):
        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)
            try:
                state.latest_energy_kwh = float(payload)
            except (ValueError, TypeError):
                logger.warning(f"Could not parse energy value '{payload}' for {vehicle_id}")
                return
            # Not for a trip taken over without a baseline: the reading of now would be
            # stored as its start and the ride before the restart would count for nothing.
            if state.current_trip_id is not None and state.trip_start_energy_kwh is None \
                    and not state.energy_baseline_unknown:
                state.trip_start_energy_kwh = state.latest_energy_kwh
                logger.debug(f"Vehicle {vehicle_id}: energy start captured mid-trip: {state.trip_start_energy_kwh:.3f} kWh")
                # On the row as well, or a restart before the trip ends loses the baseline.
                db = database.SessionLocal()
                try:
                    crud.set_trip_start_energy(db, UUID(state.current_trip_id), state.trip_start_energy_kwh)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning(f"Vehicle {vehicle_id}: could not store the trip's start energy: {e}")
                finally:
                    db.close()
            else:
                logger.debug(f"Vehicle {vehicle_id}: energy update {state.latest_energy_kwh:.3f} kWh (trip_start={state.trip_start_energy_kwh})")

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

        async with lock:
            state = await self._get_or_create_state(vehicle_id)

            event_timestamp = datetime.now(timezone.utc)
            first_report = not state.driving_reported
            state.driving_reported = True
            adopt_open_trip = False

            if state.is_driving == is_driving:
                # A fresh state starts at "not driving", so the retained v.e.on=0 a restart
                # receives changes nothing — and a trip the previous run left open (the car
                # was switched off while Karto was down, or inside the grace period) would
                # wait two hours for the reaper. It is adopted and ended here instead,
                # through the same grace period as any other trip.
                if not (first_report and not is_driving and state.current_trip_id is None):
                    return
                adopt_open_trip = True
            else:
                logger.debug(f"Vehicle {vehicle_id}: Driving state changed to {is_driving} at {event_timestamp.isoformat()}.")

                state.is_driving = is_driving

                if is_driving:
                    ovms_db = database.OvmsSessionLocal()
                    try:
                        if not crud.is_trip_tracking_enabled(ovms_db, vehicle_id):
                            # Before last_driving_update moves: an end still waiting out
                            # its grace period checks that stamp, and a v.e.on=1 that is
                            # ignored must not cancel it — the trip would stay open until
                            # the reaper ended it without its end readings.
                            logger.debug(f"Ignoring trip start for vehicle {vehicle_id}: Trip tracking is disabled in OVMS config.")
                            state.is_driving = False
                            return
                    finally:
                        ovms_db.close()
                    state.last_driving_update = event_timestamp
                    if state.inherited_trip_last_seen is not None:
                        self._continue_or_end_inherited_trip(vehicle_id, state, event_timestamp)

                else:
                    state.last_driving_update = event_timestamp
                    if state.current_trip_id:
                        trip_id_to_end = state.current_trip_id
                        logger.debug(f"Vehicle {vehicle_id}: Driving stopped. Starting {settings.KARTO_TRIP_END_GRACE_PERIOD_SECONDS}s grace period for trip {trip_id_to_end}.")
                    else:
                        # Off, and this run tracks no trip — but the vehicle may still
                        # have one open: a restart mid-ride whose first report was the
                        # retained v.e.on=1, with the car parked before a live point
                        # resumed the trip, or a trip only the GPS log records fed. Not
                        # only on the first report, then: on every switch to off.
                        adopt_open_trip = True

            if adopt_open_trip:
                trip_id_to_end = self._adopt_open_trip_for_ending(vehicle_id, state, event_timestamp)
                if trip_id_to_end is None:
                    return
                state.last_driving_update = event_timestamp
                logger.info(f"Vehicle {vehicle_id}: trip {trip_id_to_end} is open although this run did not "
                            f"track it, and the vehicle is off. Ending it after the grace period.")

        if trip_id_to_end:
            self._schedule_trip_end(vehicle_id, trip_id_to_end, event_timestamp)

    def _schedule_trip_end(self, vehicle_id: str, trip_id: str, event_timestamp: datetime) -> None:
        """
        Waits out the grace period in a task of its own rather than in the MQTT worker
        that delivered the v.e.on=0. The workers are few and the queue in front of them
        drops what does not fit: after a restart every vehicle that is off starts its
        grace period at once, exactly while the retained values and the queued QoS 2
        records arrive, and a worker asleep for two minutes per vehicle let the queue
        overflow and discard messages the broker had already been told were delivered.

        One per vehicle: a newer end replaces the one still waiting. That one could only
        have ended in "cancelled by a newer event" anyway — scheduling a new end has just
        moved `last_driving_update` past it — and a module toggling `v.e.on` would
        otherwise leave a task per toggle sleeping out its two minutes. The old task is
        cancelled while it sleeps or waits for the lock; once it holds the lock it runs
        to its end without another await, so it is never cut off mid-finalization.
        """
        previous = self._trip_end_tasks.pop(vehicle_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._end_trip_after_grace(vehicle_id, trip_id, event_timestamp),
                                   name=f"trip-end-{vehicle_id}")
        self._trip_end_tasks[vehicle_id] = task
        task.add_done_callback(lambda t, vid=vehicle_id: self._trip_end_task_done(vid, t))

    def _trip_end_task_done(self, vehicle_id: str, task: asyncio.Task) -> None:
        # Only its own slot: a replaced task finishes after its successor took the slot.
        if self._trip_end_tasks.get(vehicle_id) is task:
            del self._trip_end_tasks[vehicle_id]
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"Trip end task {task.get_name()} failed: {task.exception()}", exc_info=task.exception())

    async def _end_trip_after_grace(self, vehicle_id: str, trip_id_to_end: str, event_timestamp: datetime) -> None:
        """
        Ends the trip once the grace period is over — unless a newer v.e.on arrived
        meanwhile (`last_driving_update` moved on), which cancels it. Runs under the
        vehicle's lock and re-reads the row, so a trip the reaper or its owner dealt with
        in the meantime is left alone.
        """
        await asyncio.sleep(settings.KARTO_TRIP_END_GRACE_PERIOD_SECONDS)

        lock = await self._get_vehicle_lock(vehicle_id)
        async with lock:
            state = await self._get_or_create_state(vehicle_id)
            if state.last_driving_update != event_timestamp:
                logger.debug(f"Vehicle {vehicle_id}: Trip-end for event at {event_timestamp.isoformat()} was cancelled by a newer event.")
                return

            logger.debug(f"Vehicle {vehicle_id}: Grace period ended. Finalizing trip {trip_id_to_end}.")
            db = database.SessionLocal()
            try:
                trip = crud.get_trip_by_id(db, UUID(trip_id_to_end))
                if trip and trip.status == 'in_progress':
                    self._finalize_trip(db, vehicle_id, trip, state)
            except Exception as e:
                db.rollback()
                logger.error(f"Vehicle {vehicle_id}: could not finalize trip {trip_id_to_end}: {e}", exc_info=True)
            finally:
                db.close()

            if state.current_trip_id == trip_id_to_end:
                self._clear_trip_state(state)

    async def cancel_pending_trip_ends(self) -> None:
        """
        Cancels the trip ends still in their grace period on shutdown. The trips stay
        open on their rows; the next start ends them through the retained v.e.on=0.
        """
        tasks = list(self._trip_end_tasks.values())
        self._trip_end_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _adopt_open_trip_for_ending(self, vehicle_id: str, state: VehicleState,
                                    now: datetime) -> Optional[str]:
        """
        Makes the vehicle's open trip, if it has one, the state's current trip, to be
        ended after the grace period. A trip whose last point is further back than
        KARTO_TRIP_RESUME_MAX_GAP_SECONDS is finalized at once instead, as the resume
        path does it, and without the vehicle's readings: the SoC and the energy counter
        of now belong to now, and the car may have been charged or driven again while
        nobody was listening — a negative consumption, or a second ride folded in.
        """
        db = database.SessionLocal()
        try:
            trip = crud.get_in_progress_trip_by_vehicle(db, vehicle_id)
            if trip is None:
                return None
            last = crud.get_last_point_position(db, trip.id)
            last_ts = last[0] if last is not None else as_utc(trip.start_time)
            idle = (now - last_ts).total_seconds()
            if idle > settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS:
                logger.info(f"Vehicle {vehicle_id}: trip {trip.id} was left open by the previous run and last "
                            f"moved {idle:.0f}s ago, finalizing it at its last point.")
                self._finalize_trip(db, vehicle_id, trip, None)
                return None
            self._adopt(state, trip, last)
            state.inherited_trip_last_seen = last_ts
            return str(trip.id)
        except Exception as e:
            db.rollback()
            logger.error(f"Vehicle {vehicle_id}: could not deal with a trip left open: {e}", exc_info=True)
            return None
        finally:
            db.close()

    def _continue_or_end_inherited_trip(self, vehicle_id: str, state: VehicleState, now: datetime) -> None:
        """
        v.e.on=1 inside the grace period of a trip the previous run left open. Within
        KARTO_TRIP_RESUME_MAX_GAP_SECONDS of its last point it is the same ride going on,
        as on the resume path, and it continues. Further back it is a new ride: the old
        trip is finalized without the vehicle's readings, and the next point starts a new
        one — merging them would join two rides with a line across whatever lay between.
        """
        last_seen = state.inherited_trip_last_seen
        if state.last_saved_gps_point is not None:
            # A buffered log record may have moved the trip's end on since it was adopted.
            last_seen = max(last_seen, state.last_saved_gps_point.timestamp)
        state.inherited_trip_last_seen = None
        idle = (now - last_seen).total_seconds()
        if idle <= settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS:
            logger.info(f"Vehicle {vehicle_id}: the vehicle is on again, continuing trip {state.current_trip_id} "
                        f"({idle:.0f}s since its last point).")
            return

        trip_id = state.current_trip_id
        logger.info(f"Vehicle {vehicle_id}: trip {trip_id} last moved {idle:.0f}s ago, finalizing it "
                    f"rather than continuing it with this ride.")
        db = database.SessionLocal()
        try:
            trip = crud.get_trip_by_id(db, UUID(trip_id))
            if trip and trip.status == 'in_progress':
                self._finalize_trip(db, vehicle_id, trip, None)
        except Exception as e:
            db.rollback()
            logger.error(f"Vehicle {vehicle_id}: could not finalize trip {trip_id}: {e}", exc_info=True)
        finally:
            db.close()
        self._clear_trip_state(state)

    def _create_trip_or_adopt(self, db, vehicle_id: str, state: VehicleState, ts: datetime,
                              soc: Optional[float], wkt: str):
        """
        Creates the vehicle's open trip, flushed so it has its id. Returns None instead
        when the database already holds one (`uq_trips_vehicle_in_progress` fired) — a
        row this run did not see when it looked, such as one a second process opened. That
        trip is adopted into the state, and the caller adds its point there: a second open
        trip per vehicle is exactly what the index exists to refuse. The rollback only
        undoes the failed insert; every caller writes nothing before it in the same
        transaction.
        """
        try:
            trip = crud.create_trip(db, vehicle_id, ts, soc, wkt, start_energy_kwh=state.latest_energy_kwh)
            db.flush()
            return trip
        except IntegrityError:
            db.rollback()
            existing = crud.get_in_progress_trip_by_vehicle(db, vehicle_id)
            if existing is None:
                raise
            logger.warning(f"Vehicle {vehicle_id}: trip {existing.id} was opened meanwhile by someone else; "
                           f"continuing it instead of opening a second one.")
            self._adopt(state, existing, crud.get_last_point_position(db, existing.id))
            return None

    @staticmethod
    def _adopt(state: VehicleState, trip, last_point) -> None:
        """
        Continues an open trip from the database: the in-memory half of a trip (its id,
        the point the next one is filtered against, its energy baseline) is rebuilt from
        the row and its newest point.
        """
        state.current_trip_id = str(trip.id)
        state.trip_start_energy_kwh = trip.start_energy_kwh
        state.energy_baseline_unknown = trip.start_energy_kwh is None
        state.inherited_trip_last_seen = None
        state.last_saved_gps_point = None
        if last_point is not None:
            ts, lat, lon, speed_kph, altitude_m = last_point
            state.last_saved_gps_point = GPSPointInternal(
                timestamp=ts, lat=lat, lon=lon, speed_kph=speed_kph, altitude_m=altitude_m)

    @staticmethod
    def _clear_trip_state(state: VehicleState) -> None:
        state.current_trip_id = None
        state.last_saved_gps_point = None
        state.trip_start_energy_kwh = None
        state.energy_baseline_unknown = False
        state.inherited_trip_last_seen = None

    @staticmethod
    def _energy_delta(vehicle_id: str, state: VehicleState) -> Optional[float]:
        """Energy used over the current trip, from the vehicle's energy counter."""
        if state.energy_baseline_unknown:
            # A trip taken over without a stored baseline. The fallback below would read
            # the whole session counter as its consumption, which is only right for a trip
            # this run saw start; unknown is honest, and the completion can still derive a
            # figure from the SoC.
            return None
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
        return energy_delta

    def _finalize_trip(self, db, vehicle_id: str, trip, state: Optional[VehicleState],
                       with_readings: bool = True) -> bool:
        """
        Completes an open trip (or discards it as too short). `state` is the vehicle state
        when it is tracking this very trip — then the end SoC and the energy come from it.
        Without one (a trip the reaper finds that no state knows), or with
        `with_readings=False` (a trip that stopped long ago), the trip ends at its last
        point with neither: the vehicle's present readings belong to now, not to when the
        trip stopped. Returns whether the trip was kept.

        The row is re-read under a row lock first, and only while it is still open: the
        per-vehicle lock keeps two finalizers apart inside one process, but nothing else
        does, and a second one would complete the trip again over the first one's figures
        — or, on a short trip, delete a row already deleted. It is a no-op instead.
        """
        locked = crud.lock_open_trip(db, trip.id)
        if locked is None:
            logger.info(f"Vehicle {vehicle_id}: trip {trip.id} is no longer open, nothing to finalize.")
            return False
        trip = locked
        end_point = crud.get_last_gps_point(db, trip.id)
        if state is not None and state.last_saved_gps_point is not None and \
                (end_point is None or as_utc(end_point.timestamp) < state.last_saved_gps_point.timestamp):
            # The last point is normally already stored - re-adding it unconditionally
            # put a duplicate final coordinate into every single track.
            saved = state.last_saved_gps_point
            end_point = crud.add_gps_point(db, trip.id, saved.timestamp, saved.to_wkt(), saved.speed_kph, saved.altitude_m)
        if end_point is None:
            logger.warning(f"Vehicle {vehicle_id}: trip {trip.id} has no points at all, discarding it.")
            db.delete(trip)
            db.commit()
            return False

        readings = state if with_readings else None
        end_soc = readings.latest_soc if readings is not None else None
        energy_delta = self._energy_delta(vehicle_id, readings) if readings is not None else None
        capacity = readings.latest_battery_capacity_kwh if readings is not None else None
        logger.debug(f"Vehicle {vehicle_id}: final energy_delta={energy_delta}, battery_capacity={capacity}")

        is_trip_valid = crud.update_trip_on_completion(db, trip, end_point.timestamp, end_soc, end_point,
                                                       energy_delta, battery_capacity_kwh=capacity)
        if is_trip_valid:
            trip_id = trip.id
            task = asyncio.create_task(generate_trip_map(trip_id))
            task.add_done_callback(lambda t: logger.error(f"Map generation failed for trip {trip_id}: {t.exception()}")
                                   if not t.cancelled() and t.exception() else None)
        return is_trip_valid

    async def reap_timed_out_trips(self) -> int:
        """
        Finalizes the open trips that have stopped receiving points — a lost `v.e.on=0`,
        a vehicle that went silent, a trip a restart left behind that nothing resumed.
        Each goes through the vehicle's lock and the same finalization as a trip that
        ended normally; a vehicle state tracking the trip is cleared with it, so its
        next point starts a new trip rather than extending a finished one.
        """
        db = database.SessionLocal()
        try:
            candidates = [(trip.id, trip.vehicle_id)
                          for trip in crud.find_timed_out_trips(db, settings.KARTO_TRIP_TIMEOUT_SECONDS)]
        finally:
            db.close()

        finalized = 0
        for trip_id, vehicle_id in candidates:
            lock = await self._get_vehicle_lock(vehicle_id)
            async with lock:
                state = self._vehicle_states.get(vehicle_id)
                db = database.SessionLocal()
                try:
                    trip = crud.get_trip_by_id(db, trip_id)
                    if trip is None or trip.status != 'in_progress':
                        continue
                    # A point may have arrived while we waited for the lock.
                    last = crud.get_last_point_position(db, trip_id)
                    last_ts = last[0] if last is not None else as_utc(trip.start_time)
                    age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                    if age < settings.KARTO_TRIP_TIMEOUT_SECONDS:
                        continue
                    tracked = state is not None and state.current_trip_id == str(trip_id)
                    logger.info(f"Vehicle {vehicle_id}: trip {trip_id} received nothing for {age:.0f}s, finalizing it.")
                    # The point the state holds may still be the end, but its readings are
                    # from now: past the timeout the car may well have been charged since.
                    self._finalize_trip(db, vehicle_id, trip, state if tracked else None, with_readings=False)
                    finalized += 1
                    if tracked:
                        self._clear_trip_state(state)
                except Exception as e:
                    db.rollback()
                    logger.error(f"Reaper: could not finalize trip {trip_id}: {e}", exc_info=True)
                finally:
                    db.close()
        return finalized

    def live_view(self, vehicle_id: str, trip_id: str) -> dict:
        """
        What only memory knows about an open trip: whether it is being driven or is in
        its end grace period, the vehicle's current SoC and the energy used so far. Empty
        values when this run is not tracking the trip (yet) — after a restart until the
        next point resumes it. Read without the lock: a snapshot for display.
        """
        state = self._vehicle_states.get(vehicle_id)
        if state is None or state.current_trip_id != trip_id:
            return {"phase": None, "current_soc": None, "energy_used_kwh": None}
        return {
            "phase": "driving" if state.is_driving else "ending",
            "current_soc": state.latest_soc,
            "energy_used_kwh": self._energy_delta(vehicle_id, state),
        }

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
                elif metric == 'v.p.gpslock':
                    state.gps_lock = payload.lower() in ['1', 'true', 'yes']
                    if state.gps_lock:
                        state.last_unlocked_position = None
                    # The lock is read when the burst is flushed; it only has to extend a burst
                    # that is already being collected, not start one of its own.
                    if state.flush_deadline is None:
                        return
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

    def _resume_open_trip(self, vehicle_id: str, state: VehicleState,
                          point: GPSPointInternal) -> Optional[bool]:
        """
        Before a new trip is started, looks for one the vehicle already has open — after a
        restart memory is empty while the row and its points are not. Within
        KARTO_TRIP_RESUME_MAX_GAP_SECONDS of its last point the trip is continued: True,
        and the state now tracks it. Further back it belongs to an earlier ride whose end
        was missed, so it is finalized and False lets the caller start a new one. None
        when the point is no newer than the trip's last one — the retained burst a
        restart receives repeats exactly that point.
        """
        db = database.SessionLocal()
        try:
            trip = crud.get_in_progress_trip_by_vehicle(db, vehicle_id)
            if trip is None:
                return False
            last = crud.get_last_point_position(db, trip.id)
            last_ts = last[0] if last is not None else as_utc(trip.start_time)
            gap = (point.timestamp - last_ts).total_seconds()
            if gap > settings.KARTO_TRIP_RESUME_MAX_GAP_SECONDS:
                logger.info(f"Vehicle {vehicle_id}: open trip {trip.id} last moved {gap:.0f}s before this point, "
                            f"finalizing it and starting a new one.")
                self._finalize_trip(db, vehicle_id, trip, None)
                return False
            self._adopt(state, trip, last)
            logger.info(f"Vehicle {vehicle_id}: resuming open trip {trip.id} ({gap:.0f}s since its last point).")
            return True if gap > 0 else None
        except Exception as e:
            db.rollback()
            logger.error(f"Vehicle {vehicle_id}: could not look for an open trip to resume: {e}", exc_info=True)
            return False
        finally:
            db.close()

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

        # Without a GPS lock the module still publishes the last position it had. After an
        # LTE outage that matters: the modem restarts, taking its GPS down with it, and the
        # reconnect burst re-sends that old position with a fresh m.time.utc - a point back
        # where the connection dropped, between the buffered log records of the outage.
        # Not every position source reports its lock, though: the Jaguar I-Pace sets the
        # position from its CAN bus while the modem GPS may have none. So an unlocked position
        # only counts as stale while it stands still; once it moves, the source is live.
        if state.gps_lock is False:
            position = (state.pending_lat, state.pending_lon)
            moved = state.last_unlocked_position is not None and position != state.last_unlocked_position
            state.last_unlocked_position = position
            if not moved:
                logger.debug(f"Vehicle {vehicle_id}: discarding GPS point without GPS lock at a position that has not moved.")
                state.pending_lat, state.pending_lon, state.pending_pos_received = None, None, None
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
            resumed = self._resume_open_trip(vehicle_id, state, current_gps_point)
            if resumed is None:
                # A replay of the point the trip already ends with (the retained burst a
                # restart receives): nothing to add.
                return

        if state.is_driving and not state.current_trip_id:
            logger.debug(f"Vehicle {vehicle_id}: First GPS point received while driving. Creating new trip.")
            db = database.SessionLocal()
            try:
                trip = self._create_trip_or_adopt(db, vehicle_id, state, current_gps_point.timestamp,
                                                  state.latest_soc, current_gps_point.to_wkt())
                if trip is not None:
                    crud.add_gps_point(db, trip.id, current_gps_point.timestamp, current_gps_point.to_wkt(), current_gps_point.speed_kph, current_gps_point.altitude_m)
                    db.commit()
                    state.current_trip_id = str(trip.id)
                    state.trip_start_energy_kwh = state.latest_energy_kwh
                    state.last_saved_gps_point = current_gps_point
                    logger.debug(f"TRIP STARTED: Vehicle {vehicle_id} started new trip {trip.id}.")
                    return
            finally:
                db.close()
            # Another open trip was adopted instead: the point goes through the filter
            # below, against that trip's last point, like any other.

        if state.current_trip_id and state.inherited_trip_last_seen is not None:
            # A trip the previous run left open, being ended while the vehicle is off: this
            # is the retained position under a fresh m.time.utc, not a point of that ride.
            logger.debug(f"Vehicle {vehicle_id}: vehicle is off, not adding a point to trip "
                         f"{state.current_trip_id} left open by the previous run.")
            return

        if state.current_trip_id:
            should_save = state.last_saved_gps_point is None or _passes_point_filter(
                state.last_saved_gps_point, current_gps_point.lat, current_gps_point.lon, current_gps_point.speed_kph)

            if should_save:
                db = database.SessionLocal()
                try:
                    # Verify the trip is still open before adding points: it may have been
                    # deleted by its owner, or finalized by the reaper.
                    trip = crud.get_trip_by_id(db, UUID(state.current_trip_id))
                    if not trip or trip.status != 'in_progress':
                        logger.warning(f"Trip {state.current_trip_id} is no longer open. Clearing trip state for vehicle {vehicle_id}.")
                        self._clear_trip_state(state)
                    else:
                        crud.add_gps_point(db, UUID(state.current_trip_id), current_gps_point.timestamp, current_gps_point.to_wkt(), current_gps_point.speed_kph, current_gps_point.altitude_m)
                        db.commit()
                        state.last_saved_gps_point = current_gps_point
                except Exception as e:
                    # Check if it's a foreign key violation (trip was deleted between check and insert)
                    if "ForeignKeyViolation" in str(type(e)) or "gps_points_trip_id_fkey" in str(e):
                        logger.warning(f"Trip {state.current_trip_id} was deleted during GPS point insertion. Clearing trip state for vehicle {vehicle_id}.")
                        self._clear_trip_state(state)
                    else:
                        logger.error(f"Failed to add GPS point for trip {state.current_trip_id}: {e}", exc_info=True)
                    db.rollback()
                finally:
                    db.close()
        else:
            state.last_saved_gps_point = current_gps_point

trip_tracker_service = TripTrackerService()