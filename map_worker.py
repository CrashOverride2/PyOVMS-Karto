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
from typing import Optional
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
PLAYWRIGHT_RESTART_AFTER_PAGES = 250
PLAYWRIGHT_RESTART_MEMORY_MB = 2048
PLAYWRIGHT_START_TIMEOUT_SECONDS = 60
PAGE_CLOSE_TIMEOUT_SECONDS = 15
PAGE_OPEN_TIMEOUT_SECONDS = 30
JOB_RENDER_TIMEOUT_SECONDS = 120
BROWSER_CLOSE_TIMEOUT_SECONDS = 30
TILE_REQUEST_TIMEOUT_SECONDS = 15
RENDER_FAILURE_DELAY_SECONDS = POLL_INTERVAL_SECONDS
RENDER_FAILURE_THRESHOLD = 3
RENDER_FAILURE_BACKOFF_SECONDS = 30
RENDER_FAILURE_GIVE_UP_SECONDS = 20 * 60
RENDER_FAILURE_GIVE_UP_THRESHOLD = max(
    RENDER_FAILURE_THRESHOLD + 1,
    RENDER_FAILURE_GIVE_UP_SECONDS // (JOB_RENDER_TIMEOUT_SECONDS + RENDER_FAILURE_BACKOFF_SECONDS),
)

# (theme name passed to the renderer, file name suffix). The app shows whichever
# variant matches its brightness, so both are produced in a single page load: after the
# first one the browser, the PMTiles data and the trip GeoJSON are already in place and
# the second costs little more than a repaint and a screenshot.
#
# The dark variant keeps the empty suffix so previously rendered files and the URLs the
# clients already hold stay valid.
MAP_VARIANTS = (("dark", ""), ("light", "_light"))

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
    if not logger.isEnabledFor(logging.DEBUG):
        return
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    browser_mb = child_process_memory_mb()
    browser_note = f", children = {browser_mb:.2f} MB" if browser_mb is not None else ""
    logger.debug(
        f"MEMORY({context}): RSS = {mem_info.rss / 1024 / 1024:.2f} MB{browser_note}"
    )


def child_process_memory_mb() -> Optional[float]:
    """
    Resident memory of everything the worker spawned, or None if it cannot be read.

    Dominated by the Playwright browser: Chromium runs as its own tree of browser,
    renderer and GPU processes, so the worker's own RSS says nothing about the component
    that actually grows. Measuring only os.getpid() left the usual suspect invisible in
    the logs. The local render server is in here too, but contributes a few MB.
    """
    if not PSUTIL_OK:
        return None
    try:
        total = 0
        for child in psutil.Process(os.getpid()).children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue  # exited or not ours mid-walk; the rest of the tree still counts
        return total / 1024 / 1024
    except psutil.Error as e:
        logger.debug(f"Could not read child process memory: {e}")
        return None


async def _close_quietly(awaitable, what: str):
    """
    Await a teardown call, bounded, and never let it raise.

    Every caller is on a path where the thing being closed is already suspect, and none
    of them has anything better to do with a failure than log it: the browser is being
    thrown away either way. See BROWSER_CLOSE_TIMEOUT_SECONDS for why the ceiling is not
    optional.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=BROWSER_CLOSE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning(f"{what} did not complete cleanly (non-fatal): {e!r}")


class BrowserLifecycle:
    """
    The browser's condition, as far as the worker can tell from the outside.

    These counters used to be five variables threaded through main_worker_loop(). They
    belong together because they are only ever read together: each restart trigger
    weighs more than one of them, and the reset after a relaunch has to touch the right
    subset at once or the next decision is made on stale evidence.
    """

    def __init__(self):
        self.consecutive_render_failures = 0
        self.note_launched()

    def note_launched(self):
        """A browser has just started; everything the old one accumulated is gone."""
        self.started_at = time.monotonic()
        self.pages_rendered = 0
        self._pages_at_last_memory_check = 0
        self.restarted_for_fault = False

    def note_page_opened(self):
        """
        Counted on the attempt, not on success.

        A render that fails wears the browser as much as one that succeeds — and a
        browser so wedged that it cannot even hand out a page would otherwise never
        reach the page count that would have replaced it.
        """
        self.pages_rendered += 1

    def note_render_result(self, successful: bool):
        if successful:
            self.consecutive_render_failures = 0
            self.restarted_for_fault = False
        else:
            self.consecutive_render_failures += 1

    def note_fault_restart(self):
        """
        A relaunch was spent on a browser that looked broken.

        Once per run of trouble, cleared again by the next successful render: if the
        fresh browser misbehaves the same way, the cause is elsewhere — missing PMTiles,
        an unreadable database, a page that will not close for reasons of its own — and
        relaunching Chromium per job would only add churn.
        """
        self.note_launched()
        self.restarted_for_fault = True

    def scheduled_restart_reason(self) -> Optional[str]:
        """Why the browser is due for replacement before the next job, or None."""
        elapsed = time.monotonic() - self.started_at
        if elapsed >= PLAYWRIGHT_RESTART_INTERVAL_SECONDS:
            return f"running for {elapsed / 3600:.2f} hours"
        if self.pages_rendered >= PLAYWRIGHT_RESTART_AFTER_PAGES:
            return f"{self.pages_rendered} pages rendered"
        if self.pages_rendered == self._pages_at_last_memory_check:
            return None
        self._pages_at_last_memory_check = self.pages_rendered
        memory_mb = child_process_memory_mb()
        if memory_mb is not None and memory_mb >= PLAYWRIGHT_RESTART_MEMORY_MB:
            return f"browser process tree at {memory_mb:.0f} MB"
        return None

    def fault_restart_reason(
        self, is_connected: bool, page_close_failed: bool
    ) -> Optional[str]:
        """
        Why the browser looks broken rather than merely due, or None.

        is_connected() only catches one that died outright. The failure that actually
        strands the queue is a live browser whose renderers are wedged or OOM-killed:
        the connection holds, every job runs into its timeouts, and nothing notices
        until the scheduled restart hours later. Failures in a row are the evidence for
        that, and a page that would not close is a direct symptom — that one counts even
        when the render itself succeeded.
        """
        if not is_connected:
            return "browser disconnected during render"
        if self.restarted_for_fault:
            return None
        if page_close_failed:
            return "the render page could not be closed"
        if self.consecutive_render_failures >= RENDER_FAILURE_THRESHOLD:
            return f"{self.consecutive_render_failures} renders failed in a row"
        return None

    def give_up_reason(self) -> Optional[str]:
        """
        Why the worker should stop rather than keep claiming jobs it cannot render.

        The bottom rung of the ladder at RENDER_FAILURE_GIVE_UP_THRESHOLD. Exiting hands
        the problem to the service manager, whose restart is the one reset that also
        covers a wedged Playwright driver or a browser a relaunch cannot fix.
        """
        if self.consecutive_render_failures >= RENDER_FAILURE_GIVE_UP_THRESHOLD:
            return f"{self.consecutive_render_failures} renders failed in a row"
        return None

    def failure_delay_seconds(self) -> int:
        """
        How long to wait before claiming the next job after a failure.

        A failure has to wait even though a success does not. Nothing else slows that
        branch down, so a persistent fault — missing PMTiles, an unreadable database, a
        renderer that throws on every trip — would otherwise claim, fail and mark
        'processing' one job after the next at full speed, burning through an entire
        backfill queue in seconds and leaving every job stuck until the stale-job
        timeout. One bad trip among healthy ones still costs only a single poll interval.
        """
        if self.consecutive_render_failures >= RENDER_FAILURE_THRESHOLD:
            return RENDER_FAILURE_BACKOFF_SECONDS
        return RENDER_FAILURE_DELAY_SECONDS


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

        settings.MAPS_STORAGE_PATH.mkdir(parents=True, exist_ok=True)

        await page.goto(page_url, timeout=10000)

        rendered = {}
        for theme, suffix in MAP_VARIANTS:
            result = await page.evaluate(
                """(params) => renderMap(params.tripGeoJsonUrl, params.pmtilesUrl, params.theme)
                        .then(() => ({ success: true }))
                        .catch(e => ({ success: false, error: e.message, stack: e.stack }))""",
                {
                    "tripGeoJsonUrl": trip_geojson_url,
                    "pmtilesUrl": trip_pmtiles_url,
                    "theme": theme
                }
            )

            log_memory_usage(f"After page.evaluate call ({theme})")

            if not result or not result.get('success'):
                error_message = result.get('error', 'Unknown error in browser context.') if result else \
                    'Renderer returned no result.'
                error_stack = result.get('stack', '') if result else ''
                raise RuntimeError(
                    f"Map rendering in browser failed for the {theme} variant: "
                    f"{error_message}\n{error_stack}"
                )

            map_element = await page.query_selector("#map")
            screenshot_bytes = await map_element.screenshot(timeout=10000)

            map_filename = f"{trip.vehicle_id}_{trip.id}{suffix}.png"
            map_path = settings.MAPS_STORAGE_PATH / map_filename

            with open(map_path, "wb") as f: f.write(screenshot_bytes)

            rendered[theme] = f"maps/{map_filename}"
            log_memory_usage(f"After screenshot and save ({theme})")

        crud.update_trip_map_path(db, trip.id, rendered["dark"], rendered["light"])
        logger.info(f"Successfully generated map for trip {trip.id} (dark and light)")

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
            m = StaticMap(
                600, 400,
                url_template=settings.KARTO_TILE_URL_TEMPLATE,
                tile_request_timeout=TILE_REQUEST_TIMEOUT_SECONDS,
            )
        elif settings.KARTO_ALLOW_EXTERNAL_TILES:
            m = StaticMap(600, 400, tile_request_timeout=TILE_REQUEST_TIMEOUT_SECONDS)
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
        
        image = await asyncio.to_thread(m.render, zoom=None)
        
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
    Restart the Playwright browser with retries. If a simple relaunch fails, tears down
    and recreates the entire Playwright context.

    Returns (browser, playwright_context), or (None, None) on total failure. The caller
    tells its BrowserLifecycle about the new browser; nothing here keeps time.
    """
    # Step 1: Close the old browser safely
    await _close_quietly(browser.close(), "Closing the old browser")

    # Step 2: Try relaunching browser with retries
    for attempt in range(1, BROWSER_RESTART_MAX_RETRIES + 1):
        try:
            logger.info(f"Browser launch attempt {attempt}/{BROWSER_RESTART_MAX_RETRIES}...")
            new_browser = await playwright_context.chromium.launch()
            logger.info("Playwright browser restarted successfully.")
            return new_browser, playwright_context
        except Exception as e:
            logger.warning(f"Browser launch attempt {attempt} failed: {e}")
            if attempt < BROWSER_RESTART_MAX_RETRIES:
                await asyncio.sleep(BROWSER_RESTART_RETRY_DELAY_SECONDS)

    # Step 3: Full context restart as last resort
    logger.warning("All browser launch attempts failed. Restarting entire Playwright context...")
    await _close_quietly(playwright_context.stop(), "Stopping the old Playwright context")

    try:
        new_context = await asyncio.wait_for(
            async_playwright().start(), timeout=PLAYWRIGHT_START_TIMEOUT_SECONDS
        )
        new_browser = await new_context.chromium.launch()
        logger.info("Playwright browser restarted successfully with new context.")
        return new_browser, new_context
    except Exception as e:
        logger.error(f"Full Playwright context restart also failed: {e}", exc_info=True)
        return None, None


async def _render_job(browser, job_id: str, server_url: str, lifecycle, use_pmtiles: bool):
    """
    Render one claimed job's previews. Returns (render_successful, page_close_failed).

    Every browser call in here is bounded — see the timeout constants for why none of
    them can be left to Playwright.
    """
    render_successful = False
    page_close_failed = False
    try:
        if use_pmtiles:
            page = None
            try:
                lifecycle.note_page_opened()
                page = await asyncio.wait_for(
                    browser.new_page(), timeout=PAGE_OPEN_TIMEOUT_SECONDS
                )
                page.on("console", lambda msg: logger.debug(f"BROWSER CONSOLE: [{msg.type}] {msg.text}"))
                await asyncio.wait_for(
                    render_map_with_playwright(page, job_id, server_url),
                    timeout=JOB_RENDER_TIMEOUT_SECONDS,
                )
                render_successful = True
            finally:
                if page:
                    try:
                        await asyncio.wait_for(
                            page.close(), timeout=PAGE_CLOSE_TIMEOUT_SECONDS
                        )
                        logger.debug(f"Closed Playwright page for job {job_id}.")
                        log_memory_usage(f"After closing page for job {job_id}")
                    except Exception as close_err:
                        page_close_failed = True
                        logger.warning(
                            f"Could not close the Playwright page for job "
                            f"{job_id}: {close_err!r}"
                        )
        else:
            await asyncio.wait_for(
                render_map_with_staticmap3(job_id), timeout=JOB_RENDER_TIMEOUT_SECONDS
            )
            render_successful = True
    except Exception as render_err:
        logger.error(
            f"Render process for job {job_id} failed: {render_err!r}. "
            f"It will be retried after the timeout.",
            exc_info=True,
        )
    return render_successful, page_close_failed


def _book_job_complete(job_id: str):
    """Take the finished job off the queue and log what is left behind it."""
    db_session_complete = database.SessionLocal()
    try:
        crud.complete_map_generation_job(db_session_complete, job_id)
        db_session_complete.commit()
        logger.info(f"Completed and removed job for trip {job_id}")
    except Exception as e:
        logger.error(f"Error while completing job {job_id}: {e}", exc_info=True)
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


async def _process_job(browser, job_id: str, server_url: str, lifecycle, use_pmtiles: bool):
    """
    Run one claimed job and record what it did to the browser.

    Booking a finished job lives in here, ahead of every restart decision the caller
    makes, and that order is the point: the previews are already on disk by then, and a
    restart that cannot bring a browser back ends the worker — which would leave the row
    in 'processing' until the stale-job timeout and render the whole trip a second time.
    """
    log_memory_usage(f"Before processing job {job_id}")
    render_successful, page_close_failed = await _render_job(
        browser, job_id, server_url, lifecycle, use_pmtiles
    )
    lifecycle.note_render_result(render_successful)
    if render_successful:
        _book_job_complete(job_id)
    return render_successful, page_close_failed


async def main_worker_loop():
    """The main loop for the map generation worker."""
    use_pmtiles = settings.KARTO_PMTILES_PATH and settings.KARTO_PMTILES_PATH.exists()
    if use_pmtiles and not PSUTIL_OK:
        logger.warning("`psutil` is not installed. Memory logging will be disabled. Run `pip install psutil`.")
    
    log_memory_usage("Worker Start")

    browser = None
    playwright_context = None
    server_process = None
    lifecycle = BrowserLifecycle()
    server_url = None
    
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

        try:
            playwright_context = await asyncio.wait_for(
                async_playwright().start(), timeout=PLAYWRIGHT_START_TIMEOUT_SECONDS
            )
            browser = await playwright_context.chromium.launch()
        except Exception as e:
            logger.critical(f"Could not start Playwright: {e}", exc_info=True)
            sys.exit(1)
        lifecycle.note_launched()
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
                scheduled_reason = lifecycle.scheduled_restart_reason()
                if scheduled_reason:
                    logger.info(f"Restarting Playwright browser: {scheduled_reason}.")
                    browser, playwright_context = await _restart_browser(
                        browser, playwright_context
                    )
                    lifecycle.note_launched()
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
                render_successful, page_close_failed = await _process_job(
                    browser, job_id_to_process, server_url, lifecycle, use_pmtiles
                )

                if use_pmtiles and browser:
                    failure_reason = lifecycle.fault_restart_reason(
                        is_connected=browser.is_connected(),
                        page_close_failed=page_close_failed,
                    )
                    if failure_reason:
                        logger.warning(f"Restarting Playwright browser: {failure_reason}.")
                        browser, playwright_context = await _restart_browser(
                            browser, playwright_context
                        )
                        lifecycle.note_fault_restart()
                        if browser is None:
                            logger.critical("All attempts to restart Playwright failed. Exiting worker.")
                            break

                if not render_successful:
                    give_up_reason = lifecycle.give_up_reason()
                    if give_up_reason:
                        logger.critical(
                            f"Giving up after {give_up_reason}: replacing the browser did "
                            f"not help. Exiting so the service manager restarts the worker."
                        )
                        break

                    delay = lifecycle.failure_delay_seconds()
                    logger.warning(
                        "Render failure %d in a row. Waiting %ds before claiming the next job.",
                        lifecycle.consecutive_render_failures, delay,
                    )
                    await asyncio.sleep(delay)
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
            await _close_quietly(browser.close(), "Closing the browser on shutdown")
        if playwright_context:
            await _close_quietly(playwright_context.stop(), "Stopping Playwright on shutdown")
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