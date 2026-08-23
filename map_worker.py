import asyncio
import json
import logging
import sys
import time
from pathlib import Path
import os
import secrets
import socket
import subprocess
from urllib.parse import quote

try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False

from app import crud, database
from app.config import settings

try:
    from playwright.async_api import Page, async_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False
    Page = None

try:
    from staticmap3 import CircleMarker, Line, StaticMap
    STATICMAP3_OK = True
except ImportError:
    STATICMAP3_OK = False

logging.basicConfig(level=settings.LOG_LEVEL.upper(),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("map_worker")

POLL_INTERVAL_SECONDS = 2
PLAYWRIGHT_RESTART_INTERVAL_SECONDS = 12 * 60 * 60  # 12 hours

project_root = Path(__file__).resolve().parent


def _pick_free_port() -> int:
    """
    Reserve an ephemeral port for the render server.

    A fixed port (it used to be 8082) makes the server trivially discoverable and
    collides when two workers run side by side.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def geojson_url(server_base_url: str, geojson_path: Path) -> str:
    """
    URL for a per-trip GeoJSON file under the render server's /geojson route.

    Only the file *name* is appended: the server maps that route to a single directory,
    so nothing outside it is addressable even if the path were manipulated.
    """
    return f"{server_base_url}/geojson/{quote(geojson_path.name)}"


def template_url(server_base_url: str) -> str:
    return f"{server_base_url}/template/template.html"


def pmtiles_url(server_base_url: str) -> str:
    return f"{server_base_url}/tiles.pmtiles"


def log_memory_usage(context: str):
    """Logs the current memory usage of the process if psutil is available."""
    if not PSUTIL_OK:
        return
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    logger.debug(f"MEMORY({context}): RSS = {mem_info.rss / 1024 / 1024:.2f} MB")


async def render_map_with_playwright(page: Page, trip_id: str, server_url: str):
    """
    Renders a single map using Playwright by loading resources from a local HTTP server
    to avoid issues with file:// schemes.
    """
    db = database.SessionLocal()
    temp_files = []
    servable_temp_dir = project_root / "temp_render_files"
    try:
        log_memory_usage(f"Start of render for trip {trip_id}")
        
        trip = crud.get_trip_with_points(db, trip_id)
        if not trip or not trip.geojson:
            logger.warning(f"Skipping job {trip_id}: Trip or its GeoJSON data not found.")
            return

        geojson_data = json.loads(trip.geojson)
        servable_temp_dir.mkdir(exist_ok=True)
        temp_geojson_path = servable_temp_dir / f"{trip.id}_trip.json"
        with open(temp_geojson_path, "w") as f:
            json.dump(geojson_data, f)
        temp_files.append(temp_geojson_path)

        trip_geojson_url = geojson_url(server_url, temp_geojson_path)
        trip_pmtiles_url = pmtiles_url(server_url)
        page_url = template_url(server_url)

        log_memory_usage("After creating temp files")

        await page.goto(page_url, timeout=10000)

        result = await page.evaluate(
            """(params) => renderMap(params.tripGeoJsonUrl, params.pmtilesUrl)
                    .then(() => ({ success: true }))
                    .catch(e => ({ success: false, error: e.message, stack: e.stack }))""",
            {
                "tripGeoJsonUrl": trip_geojson_url,
                "pmtilesUrl": trip_pmtiles_url
            }
        )
        
        log_memory_usage("After page.evaluate call")

        if not result or not result.get('success'):
            error_message = result.get('error', 'Unknown error in browser context.')
            error_stack = result.get('stack', '')
            raise RuntimeError(f"Map rendering in browser failed: {error_message}\n{error_stack}")

        map_element = await page.query_selector("#map")
        screenshot_bytes = await map_element.screenshot(timeout=10000)

        map_filename = f"{trip.vehicle_id}_{trip.id}.png"
        map_path = settings.MAPS_STORAGE_PATH / map_filename

        settings.MAPS_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
        
        with open(map_path, "wb") as f: f.write(screenshot_bytes)

        relative_path = f"maps/{map_filename}"
        crud.update_trip_map_path(db, trip.id, relative_path)
        log_memory_usage("After screenshot and save")
        logger.info(f"Successfully generated map for trip {trip.id}")

    except PlaywrightTimeoutError as e:
        logger.error(f"Playwright timed out for trip {trip_id}. Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to render map for trip {trip_id}: {e}", exc_info=True)
        raise
    finally:
        db.close()
        for temp_file in temp_files:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                    logger.debug(f"Cleaned up temporary file: {temp_file}")
                except OSError as e:
                    logger.error(f"Error removing temp file {temp_file}: {e}")
        if servable_temp_dir.exists():
            try:
                if not any(servable_temp_dir.iterdir()):
                    servable_temp_dir.rmdir()
            except OSError:
                 pass


async def render_map_with_staticmap3(trip_id: str):
    """Renders a single map using staticmap3."""
    db = database.SessionLocal()
    try:
        trip = crud.get_trip_with_points(db, trip_id)
        if not trip or not trip.geojson:
            logger.warning(f"Skipping job {trip_id}: Trip or its GeoJSON data not found.")
            return

        geojson_data = json.loads(trip.geojson)
        coords = geojson_data.get("coordinates")
        if not coords or len(coords) < 2: return

        # Rendering fetches one tile per map cell in the trip's bounding box, at high
        # zoom. Against a public tile server that is a location disclosure for every
        # trip of every user, so require an explicit decision rather than defaulting to
        # staticmap3's tile.openstreetmap.org.
        if settings.KARTO_TILE_URL_TEMPLATE:
            m = StaticMap(600, 400, url_template=settings.KARTO_TILE_URL_TEMPLATE)
        elif settings.KARTO_ALLOW_EXTERNAL_TILES:
            m = StaticMap(600, 400)
        else:
            logger.error(
                "Skipping map for trip %s: no local tile source configured. Set "
                "KARTO_PMTILES_PATH (preferred) or KARTO_TILE_URL_TEMPLATE to your own "
                "tile server. Setting KARTO_ALLOW_EXTERNAL_TILES=true would send this "
                "trip's coordinates to tile.openstreetmap.org instead.",
                trip_id,
            )
            return

        m.add_line(Line([tuple(c) for c in coords], "blue", 3))
        m.add_marker(CircleMarker(tuple(coords[0]), "green", 12))
        m.add_marker(CircleMarker(tuple(coords[-1]), "red", 12))
        
        image = m.render(zoom=None)
        
        map_filename = f"{trip.vehicle_id}_{trip.id}.png"
        map_path = settings.MAPS_STORAGE_PATH / map_filename
        image.save(map_path)

        relative_path = f"maps/{map_filename}"
        crud.update_trip_map_path(db, trip.id, relative_path)
        logger.info(f"Successfully generated map for trip {trip.id}")
    except Exception as e:
        logger.error(f"Failed to render map for trip {trip_id}: {e}", exc_info=True)
        raise
    finally:
        db.close()


BROWSER_RESTART_MAX_RETRIES = 3
BROWSER_RESTART_RETRY_DELAY_SECONDS = 10


async def _restart_browser(browser, playwright_context):
    """
    Restart the Playwright browser with retries. If simple browser relaunch
    fails, tears down and recreates the entire Playwright context.
    Returns (browser, playwright_context, browser_start_time) or (None, None, 0) on total failure.
    """
    # Step 1: Close the old browser safely
    try:
        await browser.close()
    except Exception as e:
        logger.warning(f"Error closing old browser (non-fatal): {e}")

    # Step 2: Try relaunching browser with retries
    for attempt in range(1, BROWSER_RESTART_MAX_RETRIES + 1):
        try:
            logger.info(f"Browser launch attempt {attempt}/{BROWSER_RESTART_MAX_RETRIES}...")
            new_browser = await playwright_context.chromium.launch()
            logger.info("Playwright browser restarted successfully.")
            return new_browser, playwright_context, time.time()
        except Exception as e:
            logger.warning(f"Browser launch attempt {attempt} failed: {e}")
            if attempt < BROWSER_RESTART_MAX_RETRIES:
                await asyncio.sleep(BROWSER_RESTART_RETRY_DELAY_SECONDS)

    # Step 3: Full context restart as last resort
    logger.warning("All browser launch attempts failed. Restarting entire Playwright context...")
    try:
        await playwright_context.stop()
    except Exception as e:
        logger.warning(f"Error stopping old Playwright context (non-fatal): {e}")

    try:
        new_context = await async_playwright().start()
        new_browser = await new_context.chromium.launch()
        logger.info("Playwright browser restarted successfully with new context.")
        return new_browser, new_context, time.time()
    except Exception as e:
        logger.error(f"Full Playwright context restart also failed: {e}", exc_info=True)
        return None, None, 0


async def main_worker_loop():
    """The main loop for the map generation worker."""
    use_pmtiles = settings.KARTO_PMTILES_PATH and settings.KARTO_PMTILES_PATH.exists()
    if use_pmtiles and not PSUTIL_OK:
        logger.warning("`psutil` is not installed. Memory logging will be disabled. Run `pip install psutil`.")
    
    log_memory_usage("Worker Start")

    browser = None
    playwright_context = None
    server_process = None
    
    if use_pmtiles:
        if not PLAYWRIGHT_OK:
            logger.critical("Playwright not installed. Run 'pip install playwright' and 'playwright install'.")
            sys.exit(1)
            
        # Serve only the three things the renderer needs — never the project root.
        # karto.env lives in the project root, and exposing it over loopback HTTP leaks
        # SECRET_KEY_JWT (shared with the OVMS main server) plus both database URLs to
        # every local account and co-located container.
        render_token = secrets.token_urlsafe(16)
        render_port = _pick_free_port()
        servable_temp_dir = project_root / "temp_render_files"
        servable_temp_dir.mkdir(exist_ok=True)

        server_script = project_root / "app" / "render_server.py"
        # The token travels through the environment rather than argv so it does not
        # show up in `ps` output.
        server_env = {
            **os.environ,
            "KARTO_RENDER_PORT": str(render_port),
            "KARTO_RENDER_TOKEN": render_token,
            "KARTO_RENDER_GEOJSON_DIR": str(servable_temp_dir),
            "KARTO_RENDER_TEMPLATE_DIR": str(project_root / "app" / "map_templates"),
            "KARTO_RENDER_PMTILES": str(settings.KARTO_PMTILES_PATH.resolve()),
        }
        cmd = [sys.executable, str(server_script)]
        logger.debug(f"Launching local render server for Playwright on port {render_port}")
        try:
            server_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=server_env
            )
            time.sleep(2)
            if server_process.poll() is not None:
                stderr = server_process.stderr.read().decode() if server_process.stderr else "No output."
                raise RuntimeError(f"Failed to start local render server. Stderr: {stderr}")
            logger.debug(f"Local render server started successfully on port {render_port}.")
        except Exception as e:
            logger.critical(f"Could not launch the local render server for Playwright: {e}", exc_info=True)
            sys.exit(1)

        server_url = f"http://127.0.0.1:{render_port}/{render_token}"

        playwright_context = await async_playwright().start()
        browser = await playwright_context.chromium.launch()
        browser_start_time = time.time()
        logger.debug("Map Worker started in Playwright mode. Waiting for jobs...")
    else:
        if not STATICMAP3_OK:
            logger.critical("staticmap3 not installed. Run 'pip install staticmap3'.")
            sys.exit(1)
        logger.debug("Map Worker started in staticmap3 mode. Waiting for jobs...")

    idle_polls = 0
    LOG_JOB_COUNT_INTERVAL = 6

    try:
        while True:
            if use_pmtiles and browser:
                elapsed_time = time.time() - browser_start_time
                if elapsed_time >= PLAYWRIGHT_RESTART_INTERVAL_SECONDS:
                    logger.info(f"Playwright browser has been running for {elapsed_time / 3600:.2f} hours. Restarting browser...")
                    browser, playwright_context, browser_start_time = await _restart_browser(
                        browser, playwright_context
                    )
                    if browser is None:
                        logger.critical("All attempts to restart Playwright failed. Exiting worker.")
                        break

            job_id_to_process = None

            db_session = database.SessionLocal()
            try:
                job = crud.claim_map_generation_job(db_session)
                if job:
                    job_id_to_process = job.trip_id
                    db_session.commit()
                    logger.info(f"Claimed map generation job for trip {job_id_to_process}")
                    idle_polls = 0
                else:
                    db_session.rollback()
                    idle_polls += 1
            except Exception as e:
                logger.error(f"Error while claiming job: {e}", exc_info=True)
                db_session.rollback()
                idle_polls += 1
            finally:
                db_session.close()

            if job_id_to_process:
                log_memory_usage(f"Before processing job {job_id_to_process}")
                render_successful = False
                try:
                    if use_pmtiles:
                        page = None
                        try:
                            page = await browser.new_page()
                            page.on("console", lambda msg, jid=job_id_to_process: logger.debug(f"BROWSER CONSOLE: [{msg.type}] {msg.text}"))
                            await render_map_with_playwright(page, job_id_to_process, server_url)
                            render_successful = True
                        finally:
                            if page:
                                await page.close()
                                logger.debug(f"Closed Playwright page for job {job_id_to_process}.")
                                log_memory_usage(f"After closing page for job {job_id_to_process}")
                    else:
                        await render_map_with_staticmap3(job_id_to_process)
                        render_successful = True
                except Exception as render_err:
                    logger.error(
                        f"Render process for job {job_id_to_process} failed: {render_err!r}. "
                        f"It will be retried after the timeout.",
                        exc_info=True,
                    )
                    # If browser-level error, attempt immediate restart
                    if use_pmtiles and browser and not browser.is_connected():
                        logger.warning("Browser disconnected during render. Triggering immediate restart...")
                        browser, playwright_context, browser_start_time = await _restart_browser(
                            browser, playwright_context
                        )
                        if browser is None:
                            logger.critical("All attempts to restart Playwright failed. Exiting worker.")
                            break

                if render_successful:
                    db_session_complete = database.SessionLocal()
                    try:
                        crud.complete_map_generation_job(db_session_complete, job_id_to_process)
                        db_session_complete.commit()
                        logger.info(f"Completed and removed job for trip {job_id_to_process}")
                    except Exception as e:
                        logger.error(f"Error while completing job {job_id_to_process}: {e}", exc_info=True)
                        db_session_complete.rollback()
                    finally:
                        db_session_complete.close()

                    db_session_count = database.SessionLocal()
                    try:
                        pending_jobs = crud.count_pending_map_jobs(db_session_count)
                        logger.info(f"{pending_jobs} jobs remaining in queue.")
                    except Exception as e:
                        logger.error(f"Could not query pending job count after completion: {e}", exc_info=True)
                    finally:
                        db_session_count.close()
            else:
                if idle_polls % LOG_JOB_COUNT_INTERVAL == 1:
                    db_session_count = database.SessionLocal()
                    try:
                        pending_jobs = crud.count_pending_map_jobs(db_session_count)
                        logger.info(f"Waiting for jobs... {pending_jobs} jobs pending in queue.")
                    except Exception as e:
                        logger.error(f"Could not query pending job count: {e}", exc_info=True)
                    finally:
                        db_session_count.close()
            
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.debug("Worker loop cancelled. Shutting down...")
    finally:
        if browser:
            await browser.close()
        if playwright_context:
            await playwright_context.stop()
        if server_process:
            logger.debug("Stopping local HTTP server...")
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Server process did not terminate gracefully, killing.")
                server_process.kill()
        logger.debug("Worker gracefully shut down.")

if __name__ == "__main__":
    try:
        database.init_db_engines()
        settings.MAPS_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
        asyncio.run(main_worker_loop())
    except KeyboardInterrupt:
        logger.debug("Worker stopped by user.")
    except Exception as e:
        logger.critical(f"A critical error occurred in the worker: {e}", exc_info=True)