import logging
import math
import re
from pathlib import Path
from typing import List, Literal, Optional
from uuid import UUID
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import crud, gpx_generator, kml_generator
from .api_models import (PaginatedTripSummary, PaginationDetails, TripDetail,
                         TripStatistics, HeatmapPoint, PaginatedTripSearchSummary)
from .config import settings
from .database import get_db, get_ovms_db
from .security import get_current_user

_MAP_FILENAME_RE = re.compile(
    r"[A-Z0-9-]{1,32}_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.png"
)
from .models_ovms import User as OvmsUser

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/karto/v1",
    tags=["Karto API"],
)

def _authorize_vehicle(ovms_db: Session, vehicle_id: str, user_id: int, action: str):
    """
    Authorize access to a vehicle's trip data and return the registration cutoff.

    Returns the timestamp before which trips must not be served — they belong to a
    previous holder of this vehicle id — or None when there is none to apply.

    The two halves are deliberately one call. Ownership was checked everywhere, but the
    age guard existed only inside resolve_owned_trip(), so the listing, the search, the
    statistics and the heatmap answered for the full history of a recycled id. Handing
    the cutoff back as the *result* of authorizing means a route cannot take the
    permission without also receiving the limit.
    """
    if not crud.check_vehicle_ownership(ovms_db, user_id=user_id, vehicle_id=vehicle_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not authorized to {action}",
        )
    return crud.registration_cutoff(ovms_db, vehicle_id)


def _authorize_trip(db: Session, ovms_db: Session, trip_id, user_id: int):
    """
    Resolve a trip on behalf of a user, or raise the right HTTP error.

    Wraps crud.resolve_owned_trip so the ownership rule lives in the CRUD layer and
    the routes cannot forget it — the previous shape ("load, then check") worked, but
    only for as long as every route remembered the second half.

    404 for both "no such trip" and "not yours": the split used to confirm that a
    trip id existed for someone else, which is a membership oracle over UUIDs an
    attacker may have seen in a shared link or a log.
    """
    try:
        return crud.resolve_owned_trip(db, ovms_db, trip_id, user_id)
    except crud.TripNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    except crud.TripNotAuthorized:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")


@router.get("/vehicles/{vehicle_id}/trips", response_model=PaginatedTripSummary, summary="List trips for a vehicle")
def get_trips_for_vehicle(
    vehicle_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Retrieve a paginated list of completed trips for a specific vehicle.

    Each trip summary includes:
    - `distance_km` / `distance_miles` — total trip distance
    - `average_speed_kph` / `average_speed_mph` — average moving speed
    - `soc_used` — battery State-of-Charge consumed (percentage points)
    - `energy_used_kwh` — energy consumed in kWh (from MQTT or derived from SoC + capacity)
    - `consumption_kwh_per_100km` / `consumption_kwh_per_100mi` — average energy efficiency
    - `start_soc` / `end_soc` — battery level at trip boundaries
    """
    cutoff = _authorize_vehicle(
        ovms_db, vehicle_id.upper(), current_user.id, "access this vehicle's trips"
    )

    offset = (page - 1) * limit
    trips, total_items = crud.get_trips_for_vehicle(
        db, vehicle_id=vehicle_id.upper(), limit=limit, offset=offset, cutoff=cutoff
    )
    
    total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
    
    return PaginatedTripSummary(
        pagination=PaginationDetails(
            total_items=total_items,
            total_pages=total_pages,
            current_page=page,
            limit=limit
        ),
        trips=trips
    )

@router.get("/vehicles/{vehicle_id}/stats", response_model=TripStatistics)
def get_trip_statistics(
    vehicle_id: str,
    start_date: Optional[date] = Query(None, description="Start date for stats (YYYY-MM-DD). If omitted, goes back indefinitely."),
    end_date: Optional[date] = Query(None, description="End date for stats (YYYY-MM-DD). If omitted, defaults to today."),
    daily_limit: int = Query(14, ge=1, le=90, description="Limit for the number of daily stat entries."),
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Retrieve total, daily, weekly, and monthly trip statistics for a specific vehicle.

    Provides aggregated data including total distance, duration, SoC used, and
    the number of trips for each period, plus a lifetime total for the given date range.
    """
    cutoff = _authorize_vehicle(
        ovms_db, vehicle_id.upper(), current_user.id, "access this vehicle's stats"
    )

    total, daily, weekly, monthly = crud.get_trip_statistics(
        db,
        vehicle_id=vehicle_id.upper(),
        start_date=start_date,
        end_date=end_date,
        daily_limit=daily_limit,
        cutoff=cutoff,
    )

    return TripStatistics(
        total=total,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
    )

@router.delete("/vehicles/{vehicle_id}/trips", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_trips_for_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Deletes ALL trips, associated GPS points, and map previews for a vehicle.
    This is an irreversible operation. Ensures the authenticated user has
    permission before deleting.
    """
    vehicle_id_upper = vehicle_id.upper()
    if not crud.check_vehicle_ownership(ovms_db, user_id=current_user.id, vehicle_id=vehicle_id_upper):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this vehicle's trips")

    map_paths_to_delete = crud.get_map_paths_for_vehicle(db, vehicle_id=vehicle_id_upper)

    for path in map_paths_to_delete:
        if path:
            try:
                map_file = Path(settings.MAPS_STORAGE_PATH) / Path(path).name
                if map_file.is_file():
                    map_file.unlink()
                    logger.info(f"Deleted map preview file: {map_file}")
            except Exception as e:
                logger.error(f"Could not delete map file {path} for vehicle {vehicle_id_upper}: {e}")

    deleted_count = crud.delete_all_trips_for_vehicle(db, vehicle_id=vehicle_id_upper)
    logger.info(f"Deleted {deleted_count} trips for vehicle {vehicle_id_upper}.")

    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.get("/vehicles/{vehicle_id}/heatmap", response_model=List[HeatmapPoint], tags=["Trip Search"])
def get_vehicle_heatmap(
    vehicle_id: str,
    grid_size_m: float = Query(100.0, ge=10, le=1000, description="The grid cell size in meters for aggregating points."),
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Generates heatmap data from all GPS points for a specific vehicle.
    Returns a list of points with weights, suitable for use with libraries
    like Leaflet.heat.
    """
    cutoff = _authorize_vehicle(
        ovms_db, vehicle_id.upper(), current_user.id, "access this vehicle's data"
    )

    heatmap_data = crud.get_heatmap_for_vehicle(
        db, vehicle_id=vehicle_id.upper(), grid_size_m=grid_size_m, cutoff=cutoff
    )
    return heatmap_data

@router.get("/trips/search", response_model=PaginatedTripSearchSummary, tags=["Trip Search"])
def search_trips_by_location(
    vehicle_id: Optional[str] = Query(None, description="Filter results to a single vehicle ID."),
    lat: Optional[float] = Query(None, description="Latitude for proximity search."),
    lon: Optional[float] = Query(None, description="Longitude for proximity search."),
    radius_m: Optional[float] = Query(None, description="Radius in meters for proximity search."),
    bbox: Optional[str] = Query(None, description="Bounding box: min_lon,min_lat,max_lon,max_lat"),
    relation: Literal[
        "starts_within_radius", "ends_within_radius", "starts_within_bbox", "ends_within_bbox"
    ] = Query("starts_within_radius"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Searches for trips based on geographic criteria for all vehicles owned by the user.

    **Search Modes:**
    1.  **Proximity (Circle):** Provide `lat`, `lon`, and `radius_m`.
        - `relation`: `starts_within_radius` or `ends_within_radius`.
        - Results are sorted by distance from the search point.
    2.  **Bounding Box:** Provide `bbox` as a comma-separated string.
        - `relation`: `starts_within_bbox` or `ends_within_bbox`.
    """
    bbox_tuple = None
    if bbox:
        try:
            bbox_parts = [float(x) for x in bbox.split(',')]
            if len(bbox_parts) != 4: raise ValueError
            bbox_tuple = tuple(bbox_parts)
        except (ValueError, IndexError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bbox format. Use min_lon,min_lat,max_lon,max_lat.")

    if not bbox_tuple and not (lat is not None and lon is not None and radius_m is not None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Either bbox or lat/lon/radius_m must be provided.")

    offset = (page - 1) * limit
    trips, total_items = crud.search_trips(
        db,
        ovms_db,
        user_id=current_user.id,
        vehicle_id=vehicle_id,
        lat=lat,
        lon=lon,
        radius_m=radius_m,
        bbox=bbox_tuple,
        relation=relation,
        limit=limit,
        offset=offset
    )

    total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
    
    return PaginatedTripSearchSummary(
        pagination=PaginationDetails(
            total_items=total_items,
            total_pages=total_pages,
            current_page=page,
            limit=limit
        ),
        trips=trips
    )

@router.get("/trips/{trip_id}", response_model=TripDetail, tags=["Trips"], summary="Get trip details")
def get_trip_details(
    trip_id: UUID,
    simplify_tolerance: Optional[float] = Query(None, ge=0, description="Simplification tolerance in degrees (e.g., 0.0001). Reduces track resolution."),
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Retrieve the full details and GeoJSON track for a single trip,
    ensuring the authenticated user has permission.
    An optional `simplify_tolerance` can be provided to reduce the number of
    points in the returned GeoJSON track.
    """
    _authorize_trip(db, ovms_db, trip_id, current_user.id)
    trip = crud.get_trip_with_points(db, trip_id=trip_id, simplify_tolerance=simplify_tolerance)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    return trip

@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Trips"], summary="Delete a trip")
def delete_trip(
    trip_id: UUID,
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Deletes a trip, its associated GPS points, and its map preview image.
    Ensures the authenticated user has permission before deleting.
    """
    trip = _authorize_trip(db, ovms_db, trip_id, current_user.id)
    
    if trip.map_preview_path:
        try:
            map_file = Path(settings.MAPS_STORAGE_PATH) / Path(trip.map_preview_path).name
            if map_file.is_file():
                map_file.unlink()
                logger.info(f"Deleted map preview file: {map_file}")
        except Exception as e:
            logger.error(f"Could not delete map file for trip {trip_id}: {e}")

    crud.delete_trip(db, trip_id=trip_id)
    
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.get("/trips/{trip_id}/gpx", response_class=Response, tags=["Exports"], summary="Export trip as GPX")
def get_trip_gpx(
    trip_id: UUID,
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Exports the trip's track data as a GPX file.
    """
    trip = _authorize_trip(db, ovms_db, trip_id, current_user.id)
    
    points = crud.get_trip_points_for_export(db, trip_id=trip_id)
    if not points:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No GPS points found for this trip to export.")

    gpx_content = gpx_generator.generate_gpx_for_trip(trip, points)

    return Response(
        content=gpx_content,
        media_type="application/gpx+xml",
        headers={
            "Content-Disposition": f'attachment; filename="karto_trip_{trip.id}.gpx"'
        }
    )

@router.get("/trips/{trip_id}/kml", response_class=Response, tags=["Exports"], summary="Export trip as KML")
def get_trip_kml(
    trip_id: UUID,
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Exports the trip's track data as a KML file.
    """
    trip = _authorize_trip(db, ovms_db, trip_id, current_user.id)

    points = crud.get_trip_points_for_export(db, trip_id=trip_id)
    if not points:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No GPS points found for this trip to export.")

    kml_content = kml_generator.generate_kml_for_trip(trip, points)
    
    media_type = "application/vnd.google-earth.kml+xml"

    return Response(
        content=kml_content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="karto_trip_{trip.id}.kml"'
        }
    )

@router.get("/maps/{filename}", tags=["Trips"], summary="Serve trip map preview")
def get_map_image(
    filename: str,
    db: Session = Depends(get_db),
    ovms_db: Session = Depends(get_ovms_db),
    current_user: OvmsUser = Depends(get_current_user)
):
    """
    Serves a specific map image after verifying ownership.
    The filename is expected to be in the format: {VEHICLE_ID}_{TRIP_ID}.png
    """
    try:
        if not _MAP_FILENAME_RE.fullmatch(filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid map filename format.")

        trip = crud.get_trip_by_map_filename(db, filename)
        if trip is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found.")

        if not crud.check_vehicle_ownership(ovms_db, user_id=current_user.id, vehicle_id=trip.vehicle_id.upper()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found.")

        vehicle = crud.get_vehicle_registration(ovms_db, trip.vehicle_id.upper())
        if vehicle is not None and crud.trip_predates_registration(vehicle, trip):
            logger.warning(
                "Refusing map %s: trip %s predates the current registration of vehicle %s.",
                filename, trip.id, trip.vehicle_id,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found.")

        file_path = settings.MAPS_STORAGE_PATH.joinpath(filename).resolve()

        if not file_path.is_relative_to(settings.MAPS_STORAGE_PATH.resolve()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found (invalid path).")

        if file_path.is_file():
            return FileResponse(
                file_path,
                media_type="image/png",
                headers={"X-Content-Type-Options": "nosniff"},
            )
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found.")

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error serving map file {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not retrieve map.")