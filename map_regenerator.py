import argparse
import logging
import sys
from uuid import UUID

from app import crud, database

logger = logging.getLogger("map_regenerator")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def main():
    parser = argparse.ArgumentParser(
        description="Karto Map Regeneration Enqueuer",
        formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Enqueue ALL completed trips for map regeneration.")
    group.add_argument("--trip-id", type=str, help="Enqueue a single trip for map regeneration by its UUID.")
    
    args = parser.parse_args()
    
    try:
        database.init_db_engines()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}", exc_info=True)
        sys.exit(1)
        
    db = database.SessionLocal()
    try:
        if args.trip_id:
            try:
                trip_uuid = UUID(args.trip_id)
                if not crud.get_trip_by_id(db, trip_uuid):
                    logger.error(f"No trip found with ID: {args.trip_id}")
                    sys.exit(1)
                trip_ids_to_enqueue = [trip_uuid]
            except ValueError:
                logger.error(f"Invalid UUID format for --trip-id: {args.trip_id}")
                sys.exit(1)
        else: # --all
            logger.info("Fetching all completed trip IDs from the database...")
            trip_ids_to_enqueue = crud.get_all_completed_trip_ids(db)

        if not trip_ids_to_enqueue:
            logger.info("No trips found to enqueue.")
            return

        logger.info(f"Enqueuing {len(trip_ids_to_enqueue)} trips for map regeneration...")
        for trip_id in trip_ids_to_enqueue:
            crud.enqueue_map_generation(db, trip_id)

        db.commit()
        
        logger.info("All specified trips have been successfully added to the generation queue.")
        logger.info("Run 'python map_worker.py' to process the queue.")

    finally:
        db.close()

if __name__ == "__main__":
    main()