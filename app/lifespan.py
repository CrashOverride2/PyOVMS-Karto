import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command

from .mqtt_subscriber import mqtt_subscriber
from .reaper import trip_reaper_task
from .trip_tracker import trip_tracker_service
from .config import settings
from . import database

MQTT_SUBSCRIPTION_REFRESH_INTERVAL_SECONDS = 60

logger = logging.getLogger(__name__)
background_tasks = set()


async def mqtt_subscription_refresh_task():
    """Periodically checks for newly-enabled vehicles and subscribes to their MQTT topics."""
    while True:
        await asyncio.sleep(MQTT_SUBSCRIPTION_REFRESH_INTERVAL_SECONDS)
        mqtt_subscriber.refresh_subscriptions()


async def run_migrations():
    """Applies database migrations using Alembic."""
    logger.info("Attempting to apply database migrations...")
    try:
        alembic_cfg_path = Path(__file__).parent.parent / "alembic.ini"
        if not alembic_cfg_path.exists():
            logger.warning(f"alembic.ini not found at {alembic_cfg_path}, skipping migrations")
            return

        alembic_cfg = AlembicConfig(str(alembic_cfg_path))
        alembic_cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, alembic_command.upgrade, alembic_cfg, "head")

        logger.info("Database migrations applied successfully (or already up-to-date).")
    except Exception as e:
        logger.error(f"Failed to apply database migrations: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app):
    logger.info("Initializing database engines...")
    database.init_db_engines()

    await run_migrations()

    logger.info("Karto service starting up...")
    
    loop = asyncio.get_running_loop()

    # Workers must exist before the client connects: _on_message runs on paho's network
    # thread and hands work to the queue, so a message arriving before start_workers()
    # would otherwise be dropped.
    await mqtt_subscriber.start_workers()
    mqtt_subscriber.connect(loop)

    reaper_task = asyncio.create_task(trip_reaper_task())
    background_tasks.add(reaper_task)

    refresh_task = asyncio.create_task(mqtt_subscription_refresh_task())
    background_tasks.add(refresh_task)

    yield

    logger.info("Karto service shutting down...")
    
    mqtt_subscriber.disconnect()
    await mqtt_subscriber.stop_workers()
    # Debounced GPS points would otherwise still be built against a disposed engine.
    await trip_tracker_service.cancel_pending_flushes()

    for task in background_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Background task cancelled successfully.")

    if database.engine:
        await loop.run_in_executor(None, database.engine.dispose)
        logger.info("Karto DB connection pool disposed.")
    if database.ovms_engine:
        await loop.run_in_executor(None, database.ovms_engine.dispose)
        logger.info("OVMS DB connection pool disposed.")