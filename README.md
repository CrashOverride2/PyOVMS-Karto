# Karto - Trip Tracking Microservice

Karto is a Python-based microservice designed to track, store, and analyze vehicle trips. It integrates with an [OVMS](https://www.openvehicles.com/) (Open Vehicle Monitoring System) environment, listening to vehicle data via an MQTT broker, processing it into distinct trips, and exposing the data through a secure REST API.

Karto has no user interface of its own — it sits behind the same reverse proxy as the [PyOVMS main server](https://github.com/CrashOverride2/PyOVMS), which renders the trip views and calls this API on the user's behalf.

## Features

-   **Automatic Trip Detection**: Listens to MQTT messages (`v.e.on`, GPS coordinates, etc.) to automatically start and stop trip recording.
-   **Per-Vehicle Opt-In**: Only vehicles with `enable_trip_tracking` set in the OVMS database are subscribed. Karto subscribes to the explicit `ovms/{owner}/{vehicle}/…` topics of those vehicles and re-checks periodically, so enabling or disabling tracking takes effect without a restart.
-   **Intelligent Trip Logic**:
    -   Configurable grace period to prevent premature trip endings.
    -   Timeout for "stale" trips that stop sending data (trip reaper).
    -   Filtering of insignificant GPS points to reduce data noise.
    -   Discarding of short, invalid trips (e.g., driving around a parking lot).
    -   Live GPS points are assembled from a whole MQTT transmit burst (debounced) instead of being committed on the first metric that arrives, so position and timestamp actually belong together.
-   **Recovery of Buffered GPS History**: OVMS modules buffer GPS points as data notifications (`notify/data/#`) during LTE outages and deliver them after reconnect. Karto subscribes with QoS 2 and a persistent session and folds late points into the in-progress trip, into a completed trip covering that time (recomputing statistics and re-rendering the map), or into a newly created trip. Known record formats are `XNE-GPS-Log` (NIU GT EVO), `RT-GPS-Log` (Renault Twizy) and `XSQ-GPS-Log` (smart EQ). The smart EQ only sends its log once the owner enables "GPS history log" in the vehicle's web settings. For the NIU, a "Route log interval" of 5–60 s is a good choice: records closer than `KARTO_GPSLOG_DEDUPE_SECONDS` (4 s) to an existing point are dropped anyway, and above `KARTO_GPSLOG_FILTER_RESET_SECONDS` (120 s) every record counts as the start of a new ride, which switches the standstill filter off.
-   **Secure REST API**:
    -   Built with FastAPI.
    -   Authentication via session JWT cookie (verified with the main server's Ed25519 **public** key) or per-user API keys.
    -   Authorization checks so users only ever see their own vehicles; a registration cutoff prevents a recycled vehicle id from exposing a previous holder's trips.
    -   Per-IP failure tracking with automatic banning, persisted in the database so bans survive a restart.
-   **Data-Rich Models**:
    -   Trip details: start/end time, duration, distance, average speed, State of Charge (SoC) usage and energy consumption.
    -   Derived values in both metric and imperial units (`distance_miles`, `average_speed_mph`, `consumption_kwh_per_100km` / `_per_100mi`).
    -   Full GPS track per trip, stored as a PostGIS LineString.
-   **Search and Heatmaps**: Geographic trip search by proximity radius or bounding box (start or end of trip), plus an aggregated heatmap endpoint for all points of a vehicle.
-   **Flexible Map Generation**:
    -   Automatically generates a PNG map preview for each completed trip, rendered by a separate worker process.
    -   Offline rendering with local [Protomaps](https://protomaps.com/) (`.pmtiles`) files, Playwright and a custom JSON style — no tile requests leave your server.
    -   `staticmap3` fallback, which requires you to point it at your own tile server (or explicitly accept the disclosure to a third-party tile provider).
-   **Data Export**: Endpoints to export trip tracks to standard `GPX` and `KML` formats.
-   **Trip Statistics**: Aggregated statistics (distance, duration, trip count, averages, extremes) for daily, weekly, monthly, and lifetime periods.
-   **Asynchronous Architecture**: Leverages `asyncio` for background tasks like the MQTT subscriber and the trip reaper.
-   **Fail-Closed Startup Checks**: The service refuses to start on a missing/malformed session key or a trust-everything proxy configuration.
-   **Database Migrations**: Uses Alembic to manage database schema changes.

## Technology Stack

-   **Backend**: Python 3.12 and newer
-   **Framework**: FastAPI, Uvicorn
-   **Database**: PostgreSQL with PostGIS extension (for geographic data).
-   **ORM**: SQLAlchemy with GeoAlchemy2.
-   **Messaging**: Paho-MQTT for connecting to an MQTT broker.
-   **Security**: `PyJWT[crypto]` (Ed25519 / EdDSA session verification)
-   **Map Generation**: `playwright`, `pmtiles`, `protomaps-leaflet`, `Pillow` (offline) or `staticmap3` (fallback).
-   **Configuration**: `pydantic-settings`
-   **Migrations**: `Alembic`

## Setup and Installation

### 1. Prerequisites

-   Python 3.12 or newer.
-   An existing OVMS setup with a running database and an MQTT Broker (e.g., FlashMQ, Mosquitto).
-   A PostgreSQL server with PostGIS for Karto's own data.
-   A `.pmtiles` basemap for offline map generation (strongly recommended).

### 2. Environment Configuration

The service is configured using environment variables.

1.  Copy the environment template file. The application looks for `karto.env` first, then `.env`.
    ```bash
    cp .env-template karto.env
    ```

2.  Edit `karto.env` and fill in the required values, especially:
    -   `DATABASE_URL`: The connection string for Karto's own PostgreSQL/PostGIS database.
    -   `OVMS_DATABASE_URL`: The **read-only** connection string to your existing OVMS database.
    -   `MQTT_*`: Credentials for your MQTT broker. The user needs read access to the `ovms/+/+/metric/#` and `ovms/+/+/notify/data/#` topic trees.
    -   `JWT_PUBLIC_KEY`: The **public half** of the OVMS main server's Ed25519 session signing key, base64-encoded (32 raw bytes). Read it back in the OVMS server directory with `python -m app.jwt_keys`.
    -   `KARTO_PMTILES_PATH`: Path to your local `.pmtiles` file to enable offline map rendering.

Other settings worth knowing (all optional, see `.env-template` for the full list and reasoning):

| Variable | Default | Purpose |
|---|---|---|
| `SERVER_HOST` / `HTTP_PORT` | `127.0.0.1` / `8001` | Bind address. Loopback by default — Karto belongs behind the reverse proxy. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Peers whose `X-Forwarded-For` is trusted. Never `*` — startup aborts on that. |
| `ENABLE_API_DOCS` | `false` | Publishes `/docs`, `/redoc`, `/openapi.json`. There is no way to authenticate them here. |
| `API_FAIL_LIMIT` / `API_FAIL_WINDOW_SECONDS` / `API_BAN_MINUTES` | `10` / `60` / `60` | Authentication failure tracking and IP banning. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `10` | Connection pooling — Karto shares the PostgreSQL server with the main server. |
| `MQTT_MAX_QUEUED_MESSAGES` / `MQTT_WORKER_COUNT` | `2000` / `4` | Bounded inbound work queue. |
| `KARTO_TRIP_END_GRACE_PERIOD_SECONDS` | `120` | Wait after `v.e.on=0` before finalizing. |
| `KARTO_TRIP_TIMEOUT_SECONDS` | `7200` | Inactivity before the reaper finalizes a trip. |
| `KARTO_TRIP_MIN_DISTANCE_KM` | `1.0` | Shorter trips are discarded. |
| `KARTO_GPS_MIN_SPEED_KPH` / `KARTO_GPS_MIN_DISTANCE_METERS` | `5.0` / `20.0` | Point filtering, for live metrics and GPS log records alike. |
| `KARTO_GPS_BATCH_DEBOUNCE_SECONDS` / `KARTO_GPS_PAIR_MAX_SKEW_SECONDS` | `0.4` / `3.0` | Live point assembly from an MQTT burst. |
| `KARTO_GPSLOG_*` | see `app/config.py` | Handling of buffered GPS log records (backdating, attach slack, de-duplication, reset of the point filter after a pause, maximum position age while moving). |

### 3. Database Setup

Karto requires its own PostgreSQL database with the PostGIS extension enabled.

```sql
-- Connect to your PostgreSQL instance as a superuser
CREATE DATABASE karto;
\c karto
CREATE EXTENSION postgis;
```

Once the database is created and `karto.env` is configured, run the database migrations using Alembic:

```bash
# Creates trips, gps_points, map_regeneration_queue and the auth ban tables
alembic upgrade head
```

### 4. Map Generation Setup

Karto can generate maps in two modes:

*   **Offline (Protomaps, recommended)**: Set `KARTO_PMTILES_PATH` to a local `.pmtiles` basemap. Rendering runs through Playwright/Chromium against a loopback-only static server, so nothing about a trip leaves your machine.
*   **Fallback (`staticmap3`)**: Without a PMTiles basemap, Karto uses `staticmap3`. Set `KARTO_TILE_URL_TEMPLATE` to your own XYZ tile server. If you set neither, map previews are simply **skipped** (trip tracking is unaffected) unless you explicitly set `KARTO_ALLOW_EXTERNAL_TILES=true` — which downloads tiles from `tile.openstreetmap.org` and thereby hands each trip's bounding box to a third party.

For the offline renderer, install the browser dependencies once:

```bash
# Install system dependencies required by Playwright's browsers
playwright install-deps

# Download the browser binaries (e.g., Chromium)
playwright install
```

## Running the Service

```bash
# Make sure your Python virtual environment is activated
pip install -r requirements.txt

# Run the API service (starts the MQTT subscriber and trip reaper automatically)
python run.py

# Run the map worker in a second process
python map_worker.py
```

The API listens on `http://127.0.0.1:8001` by default.

`requirements.txt` is generated output — never edit it by hand. Change `requirements.in` and recompile (see `requirements-dev.txt` for the exact `uv pip compile` invocation).

### Running as a systemd Service (Recommended for Production)

For production deployments on Linux, run Karto as a `systemd` service so that API and map worker start on boot and restart on failure. Two unit templates are provided in [`systemd/`](systemd/): `karto-api.service` and `karto-map-worker.service`.

1.  **Customize the Service Files**:
    -   Replace the placeholder user (`User=karto`, `Group=karto`) with a dedicated non-root user on your system.
    -   Update all placeholder paths (`/path/to/your/karto/project`) to the absolute path of the Karto project directory.

2.  **Copy and Enable the Services**:
    ```bash
    sudo cp systemd/karto-api.service /etc/systemd/system/
    sudo cp systemd/karto-map-worker.service /etc/systemd/system/

    sudo systemctl daemon-reload
    sudo systemctl enable --now karto-api.service karto-map-worker.service
    ```

3.  **Manage the Services**:
    ```bash
    sudo systemctl status karto-api.service
    sudo journalctl -fu karto-api.service
    sudo systemctl restart karto-map-worker.service
    ```

## Development

### Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt

# Full suite (offline and deterministic)
pytest

# A single file, with logs
pytest tests/test_gps_point_assembly.py -o log_cli=true -o log_cli_level=DEBUG

# CVE scan of the pinned dependencies (needs the online advisory database)
pytest -m cve
```

### API documentation export

The main server documents this API in its own `/docs` without holding any of this service's code — it reads `doc/karto-openapi.json` from the PyOVMS checkout.

```bash
# Defaults to ../PyOVMS/doc/karto-openapi.json — which assumes the two checkouts
# are siblings and the main one carries that name. Override either way:
python export_openapi.py ../PyOVMS/doc/karto-openapi.json
PYOVMS_OPENAPI_OUTPUT=/opt/PyOVMS/doc/karto-openapi.json python export_openapi.py
```

### Database migrations

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
```

## Tools and Simulators

### `simulators/trip_simulator.py` / `simulators/trip_simulator_long.py`

These scripts simulate a vehicle driving along a predefined path by publishing MQTT messages.

-   `trip_simulator.py`: A short trip around San Francisco.
-   `trip_simulator_long.py`: A longer trip from Berlin to Hamburg.

```bash
# Make sure karto.env is present and has the correct MQTT credentials
python simulators/trip_simulator.py --vehicle-id "SIM-VEHICLE-1"
```

### `simulators/api_client_simulator.py`

Acts as a client to the Karto API, demonstrating how to fetch trip lists, details, and statistics.

1.  Ensure the Karto service is running.
2.  Get a valid API key from your OVMS user profile.

```bash
python simulators/api_client_simulator.py --vehicle-id "SIM-VEHICLE-1" --api-key "your-full-api-key-here"
```

### `map_regenerator.py`

Regenerates map previews for existing trips — useful after changing the map style or updating the `.pmtiles` file.

```bash
python map_regenerator.py --trip-id "your-trip-uuid-here"
python map_regenerator.py --all
```

## API Endpoints

All trip endpoints are prefixed with `/api/karto/v1`. Every one of them requires authentication, either via the `ovms_session` JWT cookie or an `X-API-Key` header.

| Method   | Path                               | Description                                                          |
| :------- | :--------------------------------- | :------------------------------------------------------------------- |
| `GET`    | `/vehicles/{vehicle_id}/trips`     | Get a paginated list of trips for a vehicle.                         |
| `DELETE` | `/vehicles/{vehicle_id}/trips`     | Delete **all** trips, points and map previews of a vehicle.          |
| `GET`    | `/vehicles/{vehicle_id}/stats`     | Total, daily, weekly, and monthly statistics (date range selectable). |
| `GET`    | `/vehicles/{vehicle_id}/heatmap`   | Aggregated GPS point weights for heatmap rendering (`grid_size_m`).  |
| `GET`    | `/trips/search`                    | Geographic search: proximity (`lat`/`lon`/`radius_m`) or `bbox`, matched against the trip's start or end via `relation`. |
| `GET`    | `/trips/{trip_id}`                 | Get the full details and GeoJSON track for a single trip.            |
| `DELETE` | `/trips/{trip_id}`                 | Delete a trip and its associated data (map, points).                 |
| `GET`    | `/trips/{trip_id}/gpx`             | Export a trip's GPS track as a GPX file.                             |
| `GET`    | `/trips/{trip_id}/kml`             | Export a trip's GPS track as a KML file.                             |
| `GET`    | `/maps/{filename}`                 | Get the static map image for a trip (e.g., `VEHICLE-ID_trip-id.png`). |

Outside the prefix:

| Method | Path      | Description                                     |
| :----- | :-------- | :---------------------------------------------- |
| `GET`  | `/health` | Health check, returns status and service version. |

Interactive documentation (`/docs`, `/redoc`, `/openapi.json`) is **disabled by default**; enable it with `ENABLE_API_DOCS=true` on a trusted network only.

---

## License

Copyright (C) 2026 Carsten Schmiemann

Karto is free software, licensed under the **GNU General Public License, version 3 only** ([`LICENSE`](LICENSE), [SPDX](https://spdx.org/licenses/GPL-3.0-only.html): `GPL-3.0-only`) — the same license as the [PyOVMS main server](https://github.com/CrashOverride2/PyOVMS) it accompanies. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Karto has no UI of its own; its API is reached through the PyOVMS web interface, whose footer carries the source link. If you modify Karto, pointing that link at your own fork is a courtesy rather than a duty — but a useful one:

```dotenv
# in the PyOVMS main server's .env
SOURCE_CODE_URL="https://github.com/you/PyOVMS-fork"
```

### Third-party components

| Component | License |
|---|---|
| Python dependencies (~60 packages) | MIT, Apache-2.0, BSD, PSF |
| `psycopg2-binary` | LGPL with exceptions |
| `certifi` | MPL-2.0 |
| `paho-mqtt` | EPL-2.0 OR BSD-3-Clause (used under BSD-3-Clause) |
| Leaflet | BSD-2-Clause |
| pmtiles, protomaps-leaflet | BSD-3-Clause |
| `staticmap3`, `playwright` | Apache-2.0 |

Map data rendered into trip previews comes from OpenStreetMap and is licensed under the [ODbL](https://opendatacommons.org/licenses/odbl/) — a data license, separate from the code license above. Attribute OpenStreetMap wherever you publish generated maps.
