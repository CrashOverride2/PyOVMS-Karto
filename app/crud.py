import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Tuple
from uuid import UUID

from dateutil.relativedelta import relativedelta
from geoalchemy2 import Geography, Geometry
from geoalchemy2.functions import (ST_AsGeoJSON, ST_DWithin, ST_MakeEnvelope,
                                   ST_Within, ST_Simplify, ST_GeogFromText)
from sqlalchemy import and_, func, null, or_
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from . import models_ovms
from .config import settings
from .models import GPSPoint, Trip, MapRegenerationQueue

logger = logging.getLogger(__name__)

DOW_MAPPING = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}

def enqueue_map_generation(db: Session, trip_id: UUID):
    """
    Adds a trip to the map generation queue or resets its status to 'pending' if it already exists.
    The caller is responsible for committing the transaction.
    """
    stmt = insert(MapRegenerationQueue).values(
        trip_id=trip_id, 
        status='pending'
    ).on_conflict_do_update(
        index_elements=['trip_id'],
        set_={'status': 'pending', 'updated_at': func.now()}
    )
    db.execute(stmt)

def claim_map_generation_job(db: Session) -> Optional[MapRegenerationQueue]:
    """
    Atomically finds a 'pending' job or a stale 'processing' job, and marks it as 'processing'.
    The caller is responsible for committing the transaction.
    """
    stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
    
    job = db.query(MapRegenerationQueue).filter(
        (MapRegenerationQueue.status == 'pending') | 
        ((MapRegenerationQueue.status == 'processing') & (MapRegenerationQueue.updated_at < stale_threshold))
    ).order_by(MapRegenerationQueue.created_at).with_for_update(skip_locked=True).first()
    
    if job:
        job.status = 'processing'
        return job
    return None

def complete_map_generation_job(db: Session, trip_id: UUID):
    """
    Stages the deletion of a completed job from the queue.
    The caller is responsible for committing the transaction.
    """
    db.query(MapRegenerationQueue).filter(MapRegenerationQueue.trip_id == trip_id).delete(synchronize_session=False)

def count_pending_map_jobs(db: Session) -> int:
    """Counts the number of jobs in 'pending' status."""
    return db.query(MapRegenerationQueue).filter(MapRegenerationQueue.status == 'pending').count()

def get_in_progress_trip_by_vehicle(db: Session, vehicle_id: str) -> Optional[Trip]:
    return db.query(Trip).filter(Trip.vehicle_id == vehicle_id, Trip.status == 'in_progress').first()

class TripNotFound(Exception):
    """No trip with this id exists."""


class TripNotAuthorized(Exception):
    """The trip exists but does not belong to the requesting user."""


def resolve_owned_trip(db: Session, ovms_db: Session, trip_id: UUID, user_id: int) -> Trip:
    """
    Fetch a trip *and* authorize it in one step.

    Authorization used to live purely in the API layer, and several routes loaded the
    trip — including ST_MakeLine/ST_AsGeoJSON over every point — *before* checking who
    was asking. Nothing was exploitable, because every route did call the check, but
    the design made a single forgetful new route enough to leak another user's
    movements silently. Binding the two together means a caller cannot obtain a trip
    without stating on whose behalf.

    The cheap lookup runs first, so an unauthorized caller never pays for (or
    triggers) the geometry work.
    """
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        raise TripNotFound(str(trip_id))
    if not check_vehicle_ownership(ovms_db, user_id=user_id, vehicle_id=trip.vehicle_id):
        raise TripNotAuthorized(str(trip_id))

    vehicle = get_vehicle_registration(ovms_db, trip.vehicle_id)
    if vehicle is not None and trip_predates_registration(vehicle, trip):
        logger.warning(
            "Refusing trip %s: it predates the current registration of vehicle %s "
            "(orphaned history from a previous owner).", trip_id, trip.vehicle_id
        )
        raise TripNotAuthorized(str(trip_id))
    return trip


def get_trip_by_id(db: Session, trip_id: UUID) -> Optional[Trip]:
    return db.query(Trip).filter(Trip.id == trip_id).first()

def get_all_completed_trip_ids(db: Session) -> List[UUID]:
    """Fetches the UUIDs of all completed trips."""
    results = db.query(Trip.id).filter(Trip.status == 'completed').all()
    return [result[0] for result in results]

def create_trip(db: Session, vehicle_id: str, start_time: datetime, start_soc: Optional[float], start_point_wkt: str) -> Trip:
    """
    Creates a new Trip object using the provided start_time from the first GPS point.
    """
    new_trip = Trip(
        vehicle_id=vehicle_id,
        status='in_progress',
        start_time=start_time,
        start_soc=start_soc,
        start_location=start_point_wkt
    )
    db.add(new_trip)
    logger.info(f"Created new trip object for vehicle {vehicle_id} (pending commit)")
    return new_trip

def update_trip_on_completion(db: Session, trip: Trip, end_time: datetime, end_soc: Optional[float], end_point: GPSPoint, energy_used_kwh: Optional[float] = None, battery_capacity_kwh: Optional[float] = None) -> bool:
    """
    Finalizes a trip. If the trip is too short, it is deleted.
    Returns True if the trip was valid and completed, False if it was deleted.
    """
    trip.end_time = end_time
    trip.end_soc = end_soc
    trip.end_location = end_point.location
    
    if trip.start_time:
        trip.duration_seconds = int((trip.end_time - trip.start_time).total_seconds())
    
    if trip.start_soc is not None and end_soc is not None:
        trip.soc_used = trip.start_soc - end_soc

    trip.energy_used_kwh = energy_used_kwh
    if trip.energy_used_kwh is None and battery_capacity_kwh is not None and trip.soc_used is not None and trip.soc_used > 0:
        trip.energy_used_kwh = round((trip.soc_used / 100.0) * battery_capacity_kwh, 3)

    point_count = db.query(GPSPoint).filter(GPSPoint.trip_id == trip.id).count()
    distance_km = 0.0
    if point_count < 2:
        distance_km = 0.0
        trip.average_speed_kph = 0.0
    else:
        points_geom_subquery = db.query(
            GPSPoint.location.cast(Geometry).label('location_geom')
        ).filter(GPSPoint.trip_id == trip.id).order_by(GPSPoint.timestamp).subquery()
        
        line_geom = func.ST_MakeLine(points_geom_subquery.c.location_geom)
        distance_meters = db.query(func.ST_Length(line_geom.cast(Geography))).scalar()
        
        if distance_meters is not None:
            distance_km = distance_meters / 1000
            if trip.duration_seconds and trip.duration_seconds > 0:
                trip.average_speed_kph = (distance_km / trip.duration_seconds) * 3600
    
    trip.distance_km = distance_km
    
    if trip.distance_km < settings.KARTO_TRIP_MIN_DISTANCE_KM:
        logger.info(f"Discarding short trip {trip.id} ({trip.distance_km:.2f} km) for vehicle {trip.vehicle_id}.")
        db.delete(trip)
        db.commit()
        return False
    else:
        trip.status = 'completed'
        db.commit()
        logger.info(f"Completed trip {trip.id} for vehicle {trip.vehicle_id}. Distance: {trip.distance_km:.2f} km")
        return True

def update_trip_map_path(db: Session, trip_id: UUID, path: str):
    db.query(Trip).filter(Trip.id == trip_id).update({"map_preview_path": path})
    db.commit()
    logger.debug(f"Updated map path for trip {trip_id}")

def get_trip_by_map_filename(db: Session, filename: str) -> Optional[Trip]:
    """
    Resolve a map preview file back to the trip that owns it.

    The /maps endpoint used to derive the vehicle id by splitting the filename on '_'.
    That made authorization depend on an invariant enforced in a *different* repository
    (the OVMS server's `^[A-Z0-9-]+$` vehicle-id rule): the moment an underscore became
    legal there, a victim id such as ALICE_CAR would parse as the vehicle "ALICE", and
    whoever owned that would be authorized. Look the file up instead — the mapping is in
    our own table.
    """
    return db.query(Trip).filter(Trip.map_preview_path == f"maps/{filename}").first()


def get_trips_for_vehicle(
    db: Session,
    vehicle_id: str,
    limit: int,
    offset: int,
    cutoff: Optional[datetime] = None,
) -> Tuple[List[Trip], int]:
    query = db.query(Trip).filter(Trip.vehicle_id == vehicle_id, Trip.status == 'completed')
    if cutoff is not None:
        # Trips older than the current registration belong to a previous holder of this
        # vehicle id. See registration_cutoff().
        query = query.filter(Trip.start_time >= cutoff)
    total_items = query.count()
    trips = query.order_by(Trip.start_time.desc()).limit(limit).offset(offset).all()
    return trips, total_items

def get_trip_with_points(db: Session, trip_id: UUID, simplify_tolerance: Optional[float] = None) -> Optional[Trip]:
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if not trip:
        return None
    
    point_count = db.query(GPSPoint).filter(GPSPoint.trip_id == trip.id).count()
    if point_count < 2:
        trip.geojson = None
        return trip

    points_geom_subquery = db.query(
        GPSPoint.location.cast(Geometry).label('location_geom')
    ).filter(GPSPoint.trip_id == trip.id).order_by(GPSPoint.timestamp).subquery()

    line_geom = func.ST_MakeLine(points_geom_subquery.c.location_geom)

    if simplify_tolerance and simplify_tolerance > 0:
        line_geom = ST_Simplify(line_geom, simplify_tolerance)
    
    geojson_result = db.query(ST_AsGeoJSON(line_geom)).first()
    
    if geojson_result and geojson_result[0]:
        trip.geojson = geojson_result[0]
    else:
        trip.geojson = None
        
    return trip

def get_trip_points_for_export(db: Session, trip_id: UUID) -> List:
    """
    Fetches all GPS points for a specific trip, ordered by timestamp.
    Extracts latitude, longitude, and altitude for export.
    """
    return db.query(
        GPSPoint.timestamp,
        GPSPoint.speed_kph,
        GPSPoint.altitude_m,
        func.ST_Y(GPSPoint.location.cast(Geometry)).label('latitude'),
        func.ST_X(GPSPoint.location.cast(Geometry)).label('longitude')
    ).filter(GPSPoint.trip_id == trip_id).order_by(GPSPoint.timestamp).all()

def delete_trip(db: Session, trip_id: UUID) -> bool:
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip:
        db.delete(trip)
        db.commit()
        return True
    return False

def get_map_paths_for_vehicle(db: Session, vehicle_id: str) -> List[str]:
    """Retrieves all map_preview_path strings for a given vehicle."""
    results = db.query(Trip.map_preview_path).filter(
        Trip.vehicle_id == vehicle_id,
        Trip.map_preview_path.isnot(None)
    ).all()
    return [row[0] for row in results]

def delete_all_trips_for_vehicle(db: Session, vehicle_id: str) -> int:
    """
    Delete every trace of a vehicle's trips and return how many trips were removed.

    "Every trace" needs saying, because the previous version deleted only the `trips`
    rows and left two things behind:

    * `map_regeneration_queue` has no foreign key to `trips`, so the bulk delete could
      not cascade into it and orphaned rows stayed queued forever.
    * The rendered map previews on disk (`{vehicle}_{trip}.png`) were never touched.
      Those images *are* the route — deleting the trip while leaving a picture of it
      on the filesystem is not a deletion.

    GPS points are covered: the bulk delete bypasses the ORM cascade, but
    gps_points.trip_id carries ON DELETE CASCADE at the database level.

    Raises on failure rather than returning a count, so the caller can refuse to
    delete the vehicle when its data could not be removed.
    """
    trips = db.query(Trip.id, Trip.map_preview_path).filter(Trip.vehicle_id == vehicle_id).all()
    if not trips:
        return 0

    trip_ids = [row[0] for row in trips]

    # Queue entries first: they key on trip_id and would otherwise be unreachable.
    db.query(MapRegenerationQueue).filter(
        MapRegenerationQueue.trip_id.in_(trip_ids)
    ).delete(synchronize_session=False)

    db.query(Trip).filter(Trip.vehicle_id == vehicle_id).delete(synchronize_session=False)
    db.commit()

    # Files last: the database is the record of what exists, so it is the part that
    # must be consistent. A file left behind after a successful commit is logged and
    # swept next time rather than rolling the deletion back.
    for _, preview_path in trips:
        _remove_map_preview(preview_path)

    return len(trip_ids)


def _remove_map_preview(preview_path: Optional[str]) -> None:
    """Delete one rendered map image, refusing anything that escapes the maps dir."""
    if not preview_path:
        return
    try:
        # preview_path is stored as "maps/<name>.png"; only the basename is trusted.
        name = Path(str(preview_path)).name
        if not name or name in (".", ".."):
            return
        target = (settings.MAPS_STORAGE_PATH / name).resolve()
        maps_root = settings.MAPS_STORAGE_PATH.resolve()
        if maps_root not in target.parents and target != maps_root:
            logger.warning("Refusing to delete map preview outside the maps directory: %s", target)
            return
        if target.is_file():
            target.unlink()
            logger.info("Deleted map preview %s", target.name)
    except OSError as e:
        logger.warning("Could not delete map preview %r: %s", preview_path, e)

def get_completed_trip_covering(db: Session, vehicle_id: str, timestamp: datetime, slack_seconds: int) -> Optional[Trip]:
    """
    Finds a completed trip of the vehicle whose time range covers the given timestamp,
    allowing `slack_seconds` of tolerance on both ends. Used to attach late-delivered
    GPS log records to a trip that was already finalized.
    """
    slack = timedelta(seconds=slack_seconds)
    return db.query(Trip).filter(
        Trip.vehicle_id == vehicle_id,
        Trip.status == 'completed',
        Trip.start_time <= timestamp + slack,
        Trip.end_time >= timestamp - slack,
    ).order_by(Trip.end_time.desc()).first()

def trip_has_point_near(db: Session, trip_id: UUID, timestamp: datetime, tolerance_seconds: float) -> bool:
    """Checks whether the trip already has a GPS point within the given time tolerance."""
    tol = timedelta(seconds=tolerance_seconds)
    return db.query(GPSPoint.id).filter(
        GPSPoint.trip_id == trip_id,
        GPSPoint.timestamp >= timestamp - tol,
        GPSPoint.timestamp <= timestamp + tol,
    ).first() is not None

def get_last_gps_point(db: Session, trip_id: UUID) -> Optional[GPSPoint]:
    """Returns the chronologically last stored point of a trip, if it has any."""
    return db.query(GPSPoint).filter(GPSPoint.trip_id == trip_id).order_by(GPSPoint.timestamp.desc()).first()

def refresh_trip_stats(db: Session, trip: Trip):
    """
    Recomputes distance, duration and average speed of a trip after points were inserted
    out of band (late GPS log records). Extends start/end time and location to the actual
    point range. The caller is responsible for committing.
    """
    first_point = db.query(GPSPoint).filter(GPSPoint.trip_id == trip.id).order_by(GPSPoint.timestamp).first()
    last_point = db.query(GPSPoint).filter(GPSPoint.trip_id == trip.id).order_by(GPSPoint.timestamp.desc()).first()

    if first_point and trip.start_time and first_point.timestamp < trip.start_time:
        trip.start_time = first_point.timestamp
        trip.start_location = first_point.location
    if last_point and trip.end_time and last_point.timestamp > trip.end_time:
        trip.end_time = last_point.timestamp
        trip.end_location = last_point.location
    if trip.start_time and trip.end_time:
        trip.duration_seconds = int((trip.end_time - trip.start_time).total_seconds())
    if trip.start_soc is not None and trip.end_soc is not None:
        trip.soc_used = trip.start_soc - trip.end_soc

    point_count = db.query(GPSPoint).filter(GPSPoint.trip_id == trip.id).count()
    if point_count >= 2:
        points_geom_subquery = db.query(
            GPSPoint.location.cast(Geometry).label('location_geom')
        ).filter(GPSPoint.trip_id == trip.id).order_by(GPSPoint.timestamp).subquery()

        line_geom = func.ST_MakeLine(points_geom_subquery.c.location_geom)
        distance_meters = db.query(func.ST_Length(line_geom.cast(Geography))).scalar()

        if distance_meters is not None:
            trip.distance_km = distance_meters / 1000
            if trip.duration_seconds and trip.duration_seconds > 0:
                trip.average_speed_kph = (trip.distance_km / trip.duration_seconds) * 3600

def add_gps_point(db: Session, trip_id: UUID, timestamp: datetime, location_wkt: str, speed_kph: Optional[float], altitude_m: Optional[float]) -> GPSPoint:
    new_point = GPSPoint(
        trip_id=trip_id,
        timestamp=timestamp,
        location=location_wkt,
        speed_kph=speed_kph,
        altitude_m=altitude_m
    )
    db.add(new_point)
    return new_point

def find_and_reap_timed_out_trips(db: Session, timeout_seconds: int) -> int:
    """
    Finds and DELETES 'in_progress' trips that have not received an update
    within the specified timeout period.
    """
    timeout_threshold = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    
    latest_gps_subquery = db.query(
        GPSPoint.trip_id,
        func.max(GPSPoint.timestamp).label('latest_timestamp')
    ).join(Trip).filter(Trip.status == 'in_progress').group_by(GPSPoint.trip_id).subquery()

    timed_out_trips_with_points = db.query(Trip).join(
        latest_gps_subquery, Trip.id == latest_gps_subquery.c.trip_id
    ).filter(latest_gps_subquery.c.latest_timestamp < timeout_threshold).all()

    timed_out_trips_without_points = db.query(Trip).outerjoin(GPSPoint).filter(
        Trip.status == 'in_progress',
        Trip.start_time < timeout_threshold,
        GPSPoint.id.is_(None)
    ).all()
    
    all_timed_out_trips = timed_out_trips_with_points + timed_out_trips_without_points
    count = len(all_timed_out_trips)

    if count == 0:
        return 0

    for trip in all_timed_out_trips:
        logger.warning(
            f"Reaping and DELETING timed-out trip {trip.id} for vehicle {trip.vehicle_id}."
        )
        db.delete(trip)

    db.commit()
    
    return count

def _get_most_active_day(db: Session, vehicle_id: str, start_dt: Optional[datetime] = None, end_dt_exclusive: Optional[datetime] = None) -> Optional[str]:
    """Finds the most active day of the week within a given date range (inclusive start, exclusive end)."""
    dow_column = func.extract('isodow', Trip.start_time).label('dow')
    query = db.query(
        dow_column,
        func.count(Trip.id).label('trip_count')
    ).filter(
        Trip.vehicle_id == vehicle_id,
        Trip.status == 'completed',
        Trip.distance_km > 0
    )

    if start_dt:
        query = query.filter(Trip.start_time >= start_dt)
    if end_dt_exclusive:
        query = query.filter(Trip.start_time < end_dt_exclusive)

    result = query.group_by(dow_column).order_by(func.count(Trip.id).desc()).first()

    if result and result.dow:
        return DOW_MAPPING.get(int(result.dow))
    return None

def _get_stats_by_period(
    db: Session, 
    vehicle_id: str, 
    period: str, 
    start_dt: Optional[datetime] = None, 
    end_dt_exclusive: Optional[datetime] = None, 
    limit: Optional[int] = None
) -> List[dict]:
    """Helper to get aggregated trip stats for a given period ('day', 'week', 'month')."""
    period_start = func.date_trunc(period, Trip.start_time).label('period_start')
    
    query = db.query(
        period_start,
        func.coalesce(func.sum(Trip.distance_km), 0.0).label('total_distance_km'),
        func.coalesce(func.sum(Trip.duration_seconds), 0).label('total_duration_seconds'),
        func.count(Trip.id).label('trip_count'),
        func.coalesce(func.max(Trip.distance_km), 0.0).label('longest_trip_km'),
        func.coalesce(func.min(Trip.distance_km), 0.0).label('shortest_trip_km'),
        func.sum(Trip.soc_used).label('total_soc_used'),
        func.sum(Trip.energy_used_kwh).label('total_energy_used_kwh'),
    ).filter(
        Trip.vehicle_id == vehicle_id,
        Trip.status == 'completed',
        Trip.distance_km.isnot(None),
        Trip.distance_km > 0
    )

    if start_dt:
        query = query.filter(Trip.start_time >= start_dt)
    if end_dt_exclusive:
        query = query.filter(Trip.start_time < end_dt_exclusive)

    query = query.group_by(
        period_start
    ).order_by(
        period_start.desc()
    )

    if period == 'day' and limit:
        query = query.limit(limit)

    results = query.all()
    
    stats_list = []
    for row in results:
        trip_count = int(row.trip_count)
        total_duration = int(row.total_duration_seconds)
        total_distance = float(row.total_distance_km)

        avg_dist = total_distance / trip_count if trip_count > 0 else 0.0
        avg_dur = total_duration // trip_count if trip_count > 0 else 0
        avg_speed = (total_distance / total_duration) * 3600 if total_duration > 0 else 0.0
        
        start_dt_period = row.period_start
        if period == 'day':
            end_dt_period_exclusive = start_dt_period + timedelta(days=1)
        elif period == 'week':
            end_dt_period_exclusive = start_dt_period + timedelta(weeks=1)
        else: # month
            end_dt_period_exclusive = start_dt_period + relativedelta(months=1)

        most_active_day = _get_most_active_day(db, vehicle_id, start_dt_period, end_dt_period_exclusive)
        shortest_trip = float(row.shortest_trip_km) if trip_count > 0 else 0.0

        total_soc_used = float(row.total_soc_used) if row.total_soc_used is not None else None
        total_energy_used_kwh = float(row.total_energy_used_kwh) if row.total_energy_used_kwh is not None else None

        stats_list.append({
            "period": start_dt_period.date(),
            "total_distance_km": total_distance,
            "total_duration_seconds": total_duration,
            "trip_count": trip_count,
            "average_distance_per_trip_km": avg_dist,
            "average_duration_per_trip_seconds": avg_dur,
            "overall_average_speed_kph": avg_speed,
            "longest_trip_km": float(row.longest_trip_km),
            "shortest_trip_km": shortest_trip,
            "most_active_day_of_week": most_active_day,
            "total_soc_used": total_soc_used,
            "total_energy_used_kwh": total_energy_used_kwh,
        })
    return stats_list

def _get_total_stats(db: Session, vehicle_id: str, start_dt: Optional[datetime] = None, end_dt_exclusive: Optional[datetime] = None) -> dict:
    """Helper to get lifetime aggregated trip stats for a vehicle within a date range."""
    query = db.query(
        func.coalesce(func.sum(Trip.distance_km), 0.0).label('total_distance_km'),
        func.coalesce(func.sum(Trip.duration_seconds), 0).label('total_duration_seconds'),
        func.count(Trip.id).label('total_trips'),
        func.coalesce(func.max(Trip.distance_km), 0.0).label('longest_trip_km'),
        func.coalesce(func.min(Trip.distance_km), 0.0).label('shortest_trip_km'),
        func.sum(Trip.soc_used).label('total_soc_used'),
        func.sum(Trip.energy_used_kwh).label('total_energy_used_kwh'),
    ).filter(
        Trip.vehicle_id == vehicle_id,
        Trip.status == 'completed',
        Trip.distance_km.isnot(None),
        Trip.distance_km > 0
    )

    if start_dt:
        query = query.filter(Trip.start_time >= start_dt)
    if end_dt_exclusive:
        query = query.filter(Trip.start_time < end_dt_exclusive)
    
    result = query.first()

    total_trips = int(result.total_trips)
    total_duration = int(result.total_duration_seconds)
    total_distance = float(result.total_distance_km)

    avg_dist = total_distance / total_trips if total_trips > 0 else 0.0
    avg_dur = total_duration // total_trips if total_trips > 0 else 0
    avg_speed = (total_distance / total_duration) * 3600 if total_duration > 0 else 0.0
    
    most_active_day = _get_most_active_day(db, vehicle_id, start_dt, end_dt_exclusive)
    shortest_trip = float(result.shortest_trip_km) if total_trips > 0 else 0.0

    return {
        "total_distance_km": total_distance,
        "total_duration_seconds": total_duration,
        "total_trips": total_trips,
        "average_distance_per_trip_km": avg_dist,
        "average_duration_per_trip_seconds": avg_dur,
        "overall_average_speed_kph": avg_speed,
        "longest_trip_km": float(result.longest_trip_km),
        "shortest_trip_km": shortest_trip,
        "most_active_day_of_week": most_active_day,
        "total_soc_used": float(result.total_soc_used) if result.total_soc_used is not None else None,
        "total_energy_used_kwh": float(result.total_energy_used_kwh) if result.total_energy_used_kwh is not None else None,
    }

def get_trip_statistics(
    db: Session, 
    vehicle_id: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    daily_limit: int = 14,
    cutoff: Optional[datetime] = None,
) -> Tuple[dict, List[dict], List[dict], List[dict]]:
    """
    Retrieves total, daily, weekly, and monthly trip statistics for a given vehicle.

    Returns:
        A tuple containing: (total_stats, daily_stats, weekly_stats, monthly_stats)
    """
    end_dt_exclusive = None
    if end_date:
        # Add one day to the end date to make the query's upper bound exclusive.
        # e.g., for end_date 2025-08-22, we want trips where start_time < 2025-08-23 00:00:00
        end_dt_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc)
    
    start_dt_inclusive = None
    if start_date:
        start_dt_inclusive = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    # Fold the registration cutoff into the lower bound rather than adding a filter to
    # each of the four aggregations below — one of them would eventually be added
    # without it. A caller-supplied start_date can only ever narrow the window further.
    if cutoff is not None:
        start_dt_inclusive = max(start_dt_inclusive, cutoff) if start_dt_inclusive else cutoff

    total_stats = _get_total_stats(db, vehicle_id, start_dt=start_dt_inclusive, end_dt_exclusive=end_dt_exclusive)
    daily_stats = _get_stats_by_period(db, vehicle_id, 'day', start_dt=start_dt_inclusive, end_dt_exclusive=end_dt_exclusive, limit=daily_limit)
    weekly_stats = _get_stats_by_period(db, vehicle_id, 'week', start_dt=start_dt_inclusive, end_dt_exclusive=end_dt_exclusive)
    monthly_stats = _get_stats_by_period(db, vehicle_id, 'month', start_dt=start_dt_inclusive, end_dt_exclusive=end_dt_exclusive)

    return total_stats, daily_stats, weekly_stats, monthly_stats

def is_trip_tracking_enabled(db: Session, vehicle_id: str) -> bool:
    vehicle = db.query(models_ovms.Vehicle).filter(models_ovms.Vehicle.vehicle_id == vehicle_id).first()
    if vehicle:
        return vehicle.enable_trip_tracking
    return False

def get_tracking_enabled_vehicles(db: Session) -> list[tuple[str, str]]:
    """Return (owner_username, vehicle_id) for all vehicles with trip tracking enabled."""
    rows = (
        db.query(models_ovms.User.username, models_ovms.Vehicle.vehicle_id)
        .join(models_ovms.Vehicle, models_ovms.Vehicle.owner_id == models_ovms.User.id)
        .filter(models_ovms.Vehicle.enable_trip_tracking == True)
        .all()
    )
    return [(row.username, row.vehicle_id) for row in rows]

def get_vehicle_registration(db: Session, vehicle_id: str):
    """The OVMS vehicle row for this id, or None."""
    return db.query(models_ovms.Vehicle).filter(models_ovms.Vehicle.vehicle_id == vehicle_id).first()


def check_vehicle_ownership(db: Session, user_id: int, vehicle_id: str) -> bool:
    vehicle = get_vehicle_registration(db, vehicle_id)
    if vehicle:
        if vehicle.owner_id == user_id:
            return True
        user = db.query(models_ovms.User).filter(models_ovms.User.id == user_id).first()
        if user and user.is_admin:
            return True
    return False


def registration_cutoff(db: Session, vehicle_id: str) -> Optional[datetime]:
    """
    The earliest start_time a trip may carry to belong to the *current* registration of
    this vehicle id, or None when that cannot be determined.

    The same rule as trip_predates_registration(), expressed as a value a query can
    filter on. That guard only ever ran inside resolve_owned_trip(), so it covered the
    trip detail, the exports and the trip deletion — while the listing, the search, the
    statistics and the heatmap served whatever history the id had accumulated under a
    previous holder. The heatmap is the worst of those: it returns the raw positions,
    merely rounded to a grid.

    None means "no cutoff known" — a vehicle row from before created_at existed — and
    keeps the old behaviour rather than hiding legitimate history.
    """
    vehicle = get_vehicle_registration(db, vehicle_id)
    created_at = getattr(vehicle, "created_at", None) if vehicle is not None else None
    if created_at is None:
        return None
    return created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)


def trip_predates_registration(vehicle, trip) -> bool:
    """
    True if this trip was recorded before the current vehicle registration existed.

    Deleting a vehicle in the main server frees its id, and the Karto deletion that
    should follow is best-effort — if Karto was unreachable, the GPS history stayed
    behind. Whoever registered the same id next passed check_vehicle_ownership and
    inherited the previous owner's complete movement profile, heatmap and exports
    included.

    Comparing against the registration's creation time closes that without a schema
    change or a tombstone table: trips older than the row that currently owns the id
    cannot belong to it. Fails closed only when both timestamps are known; a NULL
    created_at (rows predating that column) keeps the old behaviour rather than
    hiding legitimate history.
    """
    created_at = getattr(vehicle, "created_at", None)
    started_at = getattr(trip, "start_time", None)
    if created_at is None or started_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at < created_at

def search_trips(
    db: Session,
    ovms_db: Session,
    user_id: int,
    vehicle_id: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: Optional[float] = None,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    relation: str = 'starts_within',
    limit: int = 10,
    offset: int = 0,
) -> Tuple[List[Trip], int]:
    """
    Searches for trips based on geographic criteria, ensuring user owns the vehicles.
    """
    # created_at comes along so each vehicle carries its own cutoff: this query spans
    # every vehicle the user owns, and one of them having inherited a recycled id must
    # not widen the others. See registration_cutoff().
    owned_rows = ovms_db.query(
        models_ovms.Vehicle.vehicle_id, models_ovms.Vehicle.created_at
    ).filter(models_ovms.Vehicle.owner_id == user_id).all()
    owned_cutoffs = {
        row.vehicle_id: (
            None if row.created_at is None
            else (row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc))
        )
        for row in owned_rows
    }

    if not owned_cutoffs:
        return [], 0

    query = db.query(Trip).filter(Trip.status == 'completed')

    if vehicle_id:
        vehicle_id_upper = vehicle_id.upper()
        if vehicle_id_upper not in owned_cutoffs:
            return [], 0
        scope = {vehicle_id_upper: owned_cutoffs[vehicle_id_upper]}
    else:
        scope = owned_cutoffs

    query = query.filter(or_(*[
        and_(Trip.vehicle_id == vid, Trip.start_time >= cutoff) if cutoff is not None
        else Trip.vehicle_id == vid
        for vid, cutoff in scope.items()
    ]))

    distance_col = None

    if lat is not None and lon is not None and radius_m is not None:
        point_wkt = f'POINT({lon} {lat})'
        point_geog = ST_GeogFromText(point_wkt)

        if relation == 'starts_within_radius':
            query = query.filter(ST_DWithin(Trip.start_location, point_geog, radius_m))
            distance_col = Trip.start_location.ST_Distance(point_geog).label('distance_m')
        elif relation == 'ends_within_radius':
            query = query.filter(ST_DWithin(Trip.end_location, point_geog, radius_m))
            distance_col = Trip.end_location.ST_Distance(point_geog).label('distance_m')

    elif bbox:
        envelope = ST_MakeEnvelope(*bbox, 4326)
        if relation == 'starts_within_bbox':
            query = query.filter(ST_Within(Trip.start_location.cast(Geometry), envelope))
        elif relation == 'ends_within_bbox':
            query = query.filter(ST_Within(Trip.end_location.cast(Geometry), envelope))
    
    if distance_col is not None:
        query = query.add_columns(distance_col)
    else:
        query = query.add_columns(null().label('distance_m'))

    total_items_query = query.with_entities(func.count(Trip.id))
    total_items = total_items_query.scalar()

    if distance_col is not None:
        query = query.order_by(distance_col)
    else:
        query = query.order_by(Trip.start_time.desc())

    results = query.limit(limit).offset(offset).all()

    trips_with_distance = []
    for row in results:
        trip = row.Trip
        trip.distance_m = row.distance_m
        trips_with_distance.append(trip)
    
    return trips_with_distance, total_items

# A heatmap aggregates every GPS point a vehicle ever recorded into grid cells. With
# a small grid size and years of history the result set is effectively unbounded, and
# it is built in the database before anything is streamed back. The cap turns a
# potential out-of-memory into a truncated picture, which for a heatmap is a
# perfectly reasonable answer.
MAX_HEATMAP_CELLS = 20_000


def get_heatmap_for_vehicle(
    db: Session,
    vehicle_id: str,
    grid_size_m: float = 100.0,
    cutoff: Optional[datetime] = None,
) -> List[dict]:
    """
    Generates heatmap data by aggregating GPS points into a grid using coordinate rounding.
    A larger grid_size_m results in a more aggregated, less detailed heatmap.
    """
    # 1 degree of latitude is ~111.32 km. We use this to convert meters to an approximate
    # degree resolution for our grid. This is an efficient approximation.
    grid_resolution_deg = grid_size_m / 111320.0

    # Create grid cell identifiers by rounding the coordinates.
    grid_lon = func.round(func.ST_X(GPSPoint.location.cast(Geometry)) / grid_resolution_deg) * grid_resolution_deg
    grid_lat = func.round(func.ST_Y(GPSPoint.location.cast(Geometry)) / grid_resolution_deg) * grid_resolution_deg

    query = db.query(
        func.count(GPSPoint.id).label('weight'),
        grid_lat.label('lat'),
        grid_lon.label('lon')
    ).join(Trip).filter(
        Trip.vehicle_id == vehicle_id.upper()
    )

    if cutoff is not None:
        query = query.filter(Trip.start_time >= cutoff)

    query = query.group_by(grid_lat, grid_lon).having(func.count(GPSPoint.id) > 1)

    results = query.limit(MAX_HEATMAP_CELLS).all()
    if len(results) == MAX_HEATMAP_CELLS:
        logger.warning(
            "Heatmap for vehicle %s hit the %d cell cap; increase grid_size_m for a "
            "complete picture.", vehicle_id, MAX_HEATMAP_CELLS
        )
    return [
        {"lat": row.lat, "lon": row.lon, "weight": row.weight} for row in results
    ]