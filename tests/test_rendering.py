import pytest
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID
import secrets
import subprocess
import time

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app import crud, database
from app.config import settings
from playwright.async_api import async_playwright, Error as PlaywrightError

logger = logging.getLogger(__name__)

TEST_TRIP_ID = os.environ.get("PYTEST_TRIP_ID")
SERVER_PORT = 8081
HEADLESS_MODE = True

@pytest.fixture(scope="session")
def local_http_server():
    """
    Start the same render server the worker uses, in a separate process.

    This deliberately no longer serves the project root: the old tests/server.py did,
    and the project root is where karto.env lives (SECRET_KEY_JWT, both database URLs).
    Sharing one implementation with map_worker.py also means this test exercises the
    real routing instead of a more permissive stand-in.
    """
    output_dir = project_root / "test_outputs"
    output_dir.mkdir(exist_ok=True)

    token = secrets.token_urlsafe(16)
    server_script = project_root / "app" / "render_server.py"
    env = {
        **os.environ,
        "KARTO_RENDER_PORT": str(SERVER_PORT),
        "KARTO_RENDER_TOKEN": token,
        "KARTO_RENDER_GEOJSON_DIR": str(output_dir),
        "KARTO_RENDER_TEMPLATE_DIR": str(project_root / "app" / "map_templates"),
        "KARTO_RENDER_PMTILES": (
            str(settings.KARTO_PMTILES_PATH.resolve()) if settings.KARTO_PMTILES_PATH else ""
        ),
    }

    logger.info("Launching render server process")
    server_process = subprocess.Popen(
        [sys.executable, str(server_script)], stdout=sys.stdout, stderr=sys.stderr, env=env
    )

    time.sleep(2)

    server_url = f"http://127.0.0.1:{SERVER_PORT}/{token}"
    yield server_url, project_root

    logger.info("Stopping server process...")
    server_process.terminate()
    server_process.wait()


@pytest.fixture(scope="session")
def db_session():
    """Initializes the database and provides a session for the tests."""
    database.init_db_engines()
    try:
        db = database.SessionLocal()
        yield db
    finally:
        if 'db' in locals():
            db.close()

@pytest.mark.skipif(not TEST_TRIP_ID, reason="PYTEST_TRIP_ID environment variable not set.")
@pytest.mark.asyncio
async def test_playwright_map_rendering(db_session, local_http_server):
    """Tests the full map rendering pipeline using a robust, isolated server."""
    server_url, project_root = local_http_server

    try:
        trip_uuid = UUID(TEST_TRIP_ID)
    except ValueError:
        pytest.fail(f"Invalid UUID format for PYTEST_TRIP_ID: {TEST_TRIP_ID}")

    logger.info(f"--- Starting Playwright Render Test for Trip ID: {trip_uuid} ---")

    trip = crud.get_trip_with_points(db_session, trip_uuid)
    assert trip is not None, f"Trip with ID {trip_uuid} not found in the database."
    assert trip.geojson is not None, f"Trip {trip_uuid} has no GeoJSON data to render."

    output_dir = project_root / "test_outputs"
    output_dir.mkdir(exist_ok=True)
    temp_files = []

    try:
        temp_geojson_path = output_dir / f"{trip_uuid}_trip.json"

        with open(temp_geojson_path, "w") as f:
            json.dump(json.loads(trip.geojson), f)
        temp_files.append(temp_geojson_path)
        
        trip_geojson_url = f"{server_url}/geojson/{temp_geojson_path.name}"
        pmtiles_url = f"{server_url}/tiles.pmtiles"
        page_url = f"{server_url}/template/template.html"
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS_MODE)
            page = await browser.new_page()

            page.on("console", lambda msg: logger.info(f"BROWSER CONSOLE: [{msg.type}] {msg.text}"))
            page.on("pageerror", lambda exc: logger.error(f"BROWSER PAGE ERROR: {exc.name}: {exc.message}\n{exc.stack}"))

            await page.goto(page_url, wait_until="load", timeout=15000)
            
            try:
                logger.info("Calling renderMap in browser...")
                await page.evaluate(
                    """(params) => renderMap(params.tripGeoJsonUrl, params.pmtilesUrl)""",
                    {
                        "tripGeoJsonUrl": trip_geojson_url,
                        "pmtilesUrl": pmtiles_url,
                    }
                )
            except PlaywrightError as e:
                pytest.fail(f"Map rendering script failed inside the browser. Error: {e}")

            logger.info("Taking screenshot...")
            map_element = await page.query_selector("#map")
            assert map_element is not None, "Could not find #map element on the page."
            screenshot_bytes = await map_element.screenshot(timeout=10000)

            output_path = output_dir / f"test_render_{trip_uuid}.png"
            with open(output_path, "wb") as f:
                f.write(screenshot_bytes)
            
            logger.info(f"✔✔✔ Successfully generated map! Output saved to: {output_path.resolve()} ✔✔✔")
            await browser.close()
    finally:
        for temp_file in temp_files:
            if temp_file.exists():
                temp_file.unlink()
                logger.info(f"Cleaned up temporary file: {temp_file}")