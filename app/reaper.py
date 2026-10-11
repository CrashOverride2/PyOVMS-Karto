import asyncio
import logging
from typing import Optional

from . import crud, database
from .config import settings
from .trip_tracker import trip_tracker_service

logger = logging.getLogger(__name__)

ORPHAN_SWEEP_FIRST_DELAY_SECONDS = 600
ORPHAN_SWEEP_INTERVAL_SECONDS = 3600
# More unregistered ids than this share (and the floor below) means a wrong
# OVMS_DATABASE_URL; the sweep then refuses.
ORPHAN_SWEEP_MAX_SHARE = 0.5
ORPHAN_SWEEP_MIN_ALLOWED = 3


async def trip_reaper_task():
    """
    A background task that periodically finalizes timed-out trips — open trips that
    stopped receiving points (KARTO_TRIP_TIMEOUT_SECONDS). They are finalized through the
    trip tracker, not deleted: a trip whose `v.e.on=0` was lost is still a ride.
    """
    logger.info("Trip Reaper task started.")
    await asyncio.sleep(60)

    while True:
        try:
            if not database.SessionLocal:
                logger.warning("Trip Reaper: Database not initialized yet, skipping run.")
                await asyncio.sleep(300)
                continue

            logger.debug("Running Trip Reaper check...")
            finalized = await trip_tracker_service.reap_timed_out_trips()
            if finalized > 0:
                logger.info(f"Trip Reaper: finalized {finalized} timed-out trip(s).")

            await asyncio.sleep(300)

        except asyncio.CancelledError:
            logger.info("Trip Reaper task is shutting down.")
            break
        except Exception as e:
            logger.error(f"An error occurred in the Trip Reaper task: {e}", exc_info=True)
            await asyncio.sleep(600)


def _find_unregistered_vehicle_ids() -> list[str]:
    """
    The vehicle ids Karto holds trips for that the OVMS server no longer registers —
    or [] when the answer looks like a misconfiguration rather than deleted vehicles.
    """
    with database.SessionLocal() as db:
        vehicle_ids = crud.get_trip_vehicle_ids(db)
    if not vehicle_ids:
        return []
    with database.OvmsSessionLocal() as ovms_db:
        registered = crud.get_registered_vehicle_ids(ovms_db, vehicle_ids)
        total_registered = crud.count_registered_vehicles(ovms_db)

    orphans = sorted(vid for vid in vehicle_ids if vid.upper() not in registered)
    if not orphans:
        return []
    if total_registered == 0:
        logger.error(
            f"Orphan sweep: the OVMS database holds no vehicle at all, but Karto has trips for "
            f"{len(vehicle_ids)}. Is OVMS_DATABASE_URL right? Nothing deleted."
        )
        return []
    limit = max(ORPHAN_SWEEP_MIN_ALLOWED, int(len(vehicle_ids) * ORPHAN_SWEEP_MAX_SHARE))
    if len(orphans) > limit:
        logger.error(
            f"Orphan sweep: {len(orphans)} of {len(vehicle_ids)} vehicle ids with trips are not "
            f"registered on the OVMS server (limit {limit}); that looks like the wrong database, "
            f"not deleted vehicles. Nothing deleted. Check OVMS_DATABASE_URL, or delete the trips "
            f"by hand if the vehicles really are gone: {', '.join(orphans[:20])}"
        )
        return []
    return orphans


def _delete_if_still_unregistered(vehicle_id: str) -> Optional[int]:
    """None when the id was registered again meanwhile — those trips are then the new holder's."""
    with database.OvmsSessionLocal() as ovms_db:
        if crud.get_vehicle_registration(ovms_db, vehicle_id) is not None:
            return None
    with database.SessionLocal() as db:
        return crud.delete_all_trips_for_vehicle(db, vehicle_id=vehicle_id, include_in_progress=True)


async def sweep_unregistered_vehicles() -> int:
    """
    Deletes the trips of every vehicle id the OVMS server no longer registers, and
    returns how many trips went. Each vehicle goes through the tracker's lock.
    """
    if not settings.KARTO_ORPHAN_SWEEP_ENABLED:
        return 0
    if database.SessionLocal is None or database.OvmsSessionLocal is None:
        return 0

    orphans = await asyncio.to_thread(_find_unregistered_vehicle_ids)
    removed = 0
    for vehicle_id in orphans:
        try:
            count = await trip_tracker_service.purge_vehicle(
                vehicle_id, lambda vid=vehicle_id: _delete_if_still_unregistered(vid))
        except Exception as e:
            logger.error(f"Orphan sweep: could not delete the trips of vehicle {vehicle_id}: {e}", exc_info=True)
            continue
        if count:
            logger.warning(f"Orphan sweep: deleted {count} trip(s) of vehicle {vehicle_id}, which is not "
                           f"registered on the OVMS server any more.")
            removed += count
    return removed


async def orphan_sweep_task():
    """Runs sweep_unregistered_vehicles() hourly, the first time ten minutes after start."""
    logger.info("Orphan sweep task started.")
    try:
        await asyncio.sleep(ORPHAN_SWEEP_FIRST_DELAY_SECONDS)
        while True:
            try:
                await sweep_unregistered_vehicles()
            except Exception as e:
                logger.error(f"An error occurred in the orphan sweep: {e}", exc_info=True)
            await asyncio.sleep(ORPHAN_SWEEP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Orphan sweep task is shutting down.")
