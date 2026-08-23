import uvicorn
import logging

from app import startup_checks
from app.config import settings

logging.basicConfig(level=settings.LOG_LEVEL.upper())
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    # Shared with main.py so both entrypoints enforce the same rules; importing main
    # would run them anyway, but failing here gives a clearer message before uvicorn
    # starts spawning anything.
    try:
        startup_checks.run_all()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    logger.info("Starting Karto Trip Tracking Service...")

    uvicorn.run(
        "main:app",
        host=settings.SERVER_HOST,
        port=settings.HTTP_PORT,
        reload=False,
        log_config=None,
        # Without these, request.client.host is the reverse proxy for every request:
        # the per-IP failure tracker in app/security.py counts all users into one
        # bucket, and API_FAIL_LIMIT bad keys ban the proxy address — taking the
        # service offline for everyone.
        proxy_headers=True,
        forwarded_allow_ips=settings.FORWARDED_ALLOW_IPS,
        server_header=False,
    )