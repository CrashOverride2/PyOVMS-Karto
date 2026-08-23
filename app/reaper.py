import asyncio
import logging

from . import database
from .config import settings
from . import crud

logger = logging.getLogger(__name__)

async def trip_reaper_task():
    """
    A background task that periodically checks for and "reaps" timed-out trips.
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
            db = database.SessionLocal()
            try:
                reaped_count = crud.find_and_reap_timed_out_trips(db, settings.KARTO_TRIP_TIMEOUT_SECONDS)
                if reaped_count > 0:
                    logger.debug(f"Reaped {reaped_count} timed-out trips.")
            finally:
                db.close()
            
            await asyncio.sleep(300)

        except asyncio.CancelledError:
            logger.info("Trip Reaper task is shutting down.")
            break
        except Exception as e:
            logger.error(f"An error occurred in the Trip Reaper task: {e}", exc_info=True)
            await asyncio.sleep(600)