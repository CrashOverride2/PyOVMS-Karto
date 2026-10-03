import asyncio
import logging

from . import database
from .trip_tracker import trip_tracker_service

logger = logging.getLogger(__name__)

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