import asyncio
import logging
from uuid import UUID

from . import crud
from . import database

logger = logging.getLogger(__name__)


async def generate_trip_map(trip_id: UUID):
    """
    Adds a request to the database queue for a map to be generated.
    The actual generation is handled by a separate worker process.
    """
    await asyncio.sleep(2) 
    
    db = database.SessionLocal()
    try:
        trip = crud.get_trip_by_id(db, trip_id)
        if not trip:
            logger.warning(f"Map generation skipped for trip {trip_id}: Trip not found.")
            return

        if trip.status != 'completed':
            logger.warning(f"Map generation skipped for trip {trip_id}: Trip status is '{trip.status}'.")
            return

        logger.debug(f"Enqueuing map generation task for trip {trip_id}")
        crud.enqueue_map_generation(db, trip_id)
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to enqueue map generation for trip {trip_id}: {e}", exc_info=True)
    finally:
        db.close()