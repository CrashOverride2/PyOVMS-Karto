import logging.config

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app import startup_checks
from app.api import router as api_router
from app.config import settings
from app.exceptions import IPBannedException
from app.lifespan import lifespan
from app.version import __version__

log_level = settings.LOG_LEVEL.upper()

def _apply_logging_config():
    """Apply logging configuration. Can be called multiple times to reset logging after Alembic."""
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(asctime)s - %(name)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "": {"handlers": ["default"], "level": log_level},
            "app": {"handlers": ["default"], "level": log_level, "propagate": False},
            "alembic": {"handlers": ["default"], "level": "INFO", "propagate": False},
        },
    })

# Apply logging configuration on module import
_apply_logging_config()

logger = logging.getLogger(__name__)

# Run before the app object is built, so `uvicorn main:app` aborts on a bad configuration
# exactly like `python run.py` does. A placeholder SECRET_KEY_JWT here means anyone can
# forge a session for any user of the OVMS main server.
startup_checks.run_all()

_DESCRIPTION = """
Karto is a trip tracking and analysis service for vehicles monitored via
[OVMS (Open Vehicle Monitoring System)](https://www.openvehicles.com/).

It ingests GPS and telemetry data from MQTT, assembles them into discrete trips,
and exposes them through this REST API.

## Authentication

All endpoints require authentication via one of:
- **JWT cookie** – issued by the OVMS web UI (cookie name `ovms_session`)
- **API key** – passed in the `X-API-Key` request header

Both methods are validated against the OVMS user database.

## Units

Unless noted otherwise, all source data from MQTT is metric:
- Distances: **km** (plus `_miles` variants)
- Speeds: **km/h** (plus `_mph` variants)
- Energy: **kWh**
- Consumption: **kWh/100 km** (plus `_per_100mi` variants)

## Data model

A **Trip** begins when `v.e.on=1` is received together with a first GPS fix, and ends
after a configurable grace period once `v.e.on=0` is received. Trips shorter than
`KARTO_TRIP_MIN_DISTANCE_KM` are automatically discarded.
"""

_TAGS_METADATA = [
    {
        "name": "Trips",
        "description": "List, retrieve, and delete individual trips. Includes GeoJSON track data.",
    },
    {
        "name": "Karto API",
        "description": "Core per-vehicle trip list, statistics, and vehicle-level operations.",
    },
    {
        "name": "Trip Search",
        "description": "Geographic search across all trips (proximity radius or bounding box).",
    },
    {
        "name": "Exports",
        "description": "Download trip tracks as GPX or KML files.",
    },
    {
        "name": "System",
        "description": "Health and version information.",
    },
]

app = FastAPI(
    title="Karto Trip Service",
    description=_DESCRIPTION,
    version=__version__,
    openapi_tags=_TAGS_METADATA,
    contact={
        "name": "OVMS Karto",
        "url": "https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3",
    },
    lifespan=lifespan,
    # Unauthenticated by nature — FastAPI has no hook to guard these. Publishing the
    # full API surface (paths, parameters, schemas) to anyone who can reach the port is
    # a free reconnaissance step, so they stay off unless explicitly enabled.
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

@app.exception_handler(IPBannedException)
async def ip_banned_exception_handler(request: Request, exc: IPBannedException):
    """
    Returns a 429 Too Many Requests response when an IP is banned.
    """
    logger.warning(f"Blocked request from banned IP: {exc.ip_address}")
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many failed authentication attempts. Please try again later."},
    )



app.include_router(api_router)

@app.get("/health", tags=["System"], summary="Health check")
async def health_check():
    """Returns service status and current version."""
    return {"status": "ok", "version": __version__}