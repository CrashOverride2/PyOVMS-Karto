import argparse
import logging
import sys
import time
from uuid import UUID

from app import crud, database
from app.config import settings

logger = logging.getLogger("map_regenerator")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Previews younger than this are treated as in flight, not as orphans. The worker writes
# an image to disk before it stores the path, and with two variants per job that gap
# spans a whole second render pass — long enough for a sweep to catch a file mid-job.
# Deleting one leaves the worker storing a path to a file that no longer exists, and
# nothing checks that direction afterwards: the map just 404s until someone notices and
# regenerates it. Real orphans come from deleted trips and renames and are never fresh,
# so the wait costs nothing.
ORPHAN_MIN_AGE_SECONDS = 15 * 60


def enqueue(db, args) -> None:
    """Put trips into the map generation queue for the worker to pick up."""
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
    else:  # --all
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


def prune_orphans(db, delete: bool) -> None:
    """
    Report — and with --delete, remove — preview images no trip points at any more.

    Regenerating maps overwrites files in place, so it never produces orphans; these
    come from trips deleted while their image could not be unlinked, and from renames.
    Listing is the default because the alternative is deleting a user's data on the
    strength of a database query that may have failed halfway.
    """
    maps_dir = settings.MAPS_STORAGE_PATH
    if not maps_dir.is_dir():
        logger.error(f"Maps directory does not exist: {maps_dir}")
        sys.exit(1)

    referenced = crud.get_referenced_map_filenames(db)
    logger.info(f"Database references {len(referenced)} preview files.")

    cutoff = time.time() - ORPHAN_MIN_AGE_SECONDS
    orphans = []
    in_flight = 0
    for entry in maps_dir.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".png":
            continue
        if entry.name in referenced:
            continue
        try:
            recently_written = entry.stat().st_mtime > cutoff
        except OSError as e:
            # Unreadable now means unreadable for the unlink too; leave it alone.
            logger.warning(f"Skipping {entry.name}: {e}")
            continue
        if recently_written:
            in_flight += 1
            continue
        orphans.append(entry)
    orphans.sort()

    if in_flight:
        logger.info(
            f"Ignoring {in_flight} unreferenced previews written in the last "
            f"{ORPHAN_MIN_AGE_SECONDS // 60} minutes — the map worker may still be "
            f"holding them. Re-run later if they are really orphans."
        )

    if not orphans:
        logger.info(f"No orphaned previews in {maps_dir}.")
        return

    total_bytes = 0
    for orphan in orphans:
        try:
            total_bytes += orphan.stat().st_size
        except OSError:
            pass
        logger.info(f"{'Deleting' if delete else 'Orphan'}: {orphan.name}")

    logger.info(f"{len(orphans)} orphaned previews, {total_bytes / 1024 / 1024:.2f} MB.")

    if not delete:
        logger.info("Dry run — nothing was removed. Re-run with --delete to remove them.")
        return

    removed = 0
    for orphan in orphans:
        try:
            orphan.unlink()
            removed += 1
        except OSError as e:
            logger.error(f"Could not delete {orphan.name}: {e}")

    logger.info(f"Deleted {removed} of {len(orphans)} orphaned previews.")


def main():
    parser = argparse.ArgumentParser(
        description="Karto Map Regeneration Enqueuer",
        formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Enqueue ALL completed trips for map regeneration.")
    group.add_argument("--trip-id", type=str, help="Enqueue a single trip for map regeneration by its UUID.")
    group.add_argument(
        "--prune-orphans",
        action="store_true",
        help="List preview images in MAPS_STORAGE_PATH that no trip references any more."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="With --prune-orphans: actually delete the listed files instead of only reporting them."
    )

    args = parser.parse_args()

    if args.delete and not args.prune_orphans:
        parser.error("--delete is only meaningful together with --prune-orphans.")

    try:
        database.init_db_engines()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}", exc_info=True)
        sys.exit(1)

    db = database.SessionLocal()
    try:
        if args.prune_orphans:
            prune_orphans(db, delete=args.delete)
        else:
            enqueue(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
