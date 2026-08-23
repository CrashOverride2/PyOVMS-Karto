"""
Fail-closed configuration checks, run before the service accepts any request.

Karto validates the OVMS main server's session cookies itself, using the *same*
symmetric SECRET_KEY_JWT. That makes the secret's quality a Karto problem too: with the
placeholder value from .env-template, anyone can mint
`{"sub": "<any-user>", "mfa": true, "ver": 0, "exp": <future>}`, set it as a cookie and
read (or delete) every user's trip history — and with an admin's username, the admin
bypass in crud.check_vehicle_ownership hands over every vehicle in the system.

The main server refuses to start on placeholder secrets (app/bootstrap.py) and
generates them on first run. Karto had neither check, and the gap is easy to miss in
practice because API-key authentication keeps working regardless — only the cookie path
would be affected, and that is rarely exercised during setup.

These checks run at import time of main.py, so `uvicorn main:app` is covered as well as
`python run.py`.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Mirrors the marker list in the main server's app/bootstrap.py. Keep them in sync:
# the two services share this secret, so a value that is unacceptable there is
# unacceptable here.
_PLACEHOLDER_MARKERS = (
    "change_this_for_production",
    "changeme_",
    "placeholder",
    "another_super_secret_key_for_jwt",
)

_MIN_SECRET_LENGTH = 32

_ALLOWED_ALGORITHMS = {"HS256", "HS384", "HS512"}


def check_jwt_configuration() -> None:
    """
    Require a usable Ed25519 public key for session verification.

    Karto no longer holds a signing key at all. If this is missing there is no safe
    fallback — validating cookies against the old shared symmetric secret would restore
    exactly the capability (minting tokens for the main server) that was removed.
    """
    from app import jwt_keys

    try:
        jwt_keys.get_public_key()
    except jwt_keys.JwtKeyError as exc:
        raise RuntimeError(f"STARTUP ABORTED: {exc}") from exc

    # Loud, actionable warning rather than a hard stop: a leftover SECRET_KEY_JWT is no
    # longer used for anything here, but leaving the main server's signing secret lying
    # around on this host defeats the purpose of the migration.
    leftover = (settings.SECRET_KEY_JWT or "").strip()
    if leftover and not any(marker in leftover for marker in _PLACEHOLDER_MARKERS):
        logger.warning(
            "karto.env still contains SECRET_KEY_JWT. It is no longer used — session "
            "tokens are verified with JWT_PUBLIC_KEY now. If that value is the main "
            "server's real signing secret, remove it from this host: anyone who reads it "
            "can forge a session for any OVMS user."
        )


def check_proxy_configuration() -> None:
    """
    Refuse to run when every peer's X-Forwarded-For is trusted.

    With "*" uvicorn falls back to the leftmost, client-controlled XFF entry, so a caller
    picks its own identity for the per-IP failure tracker — it can both evade its own ban
    and drive an arbitrary victim address into one. A prefix length of 0 has the same
    effect through trusted_networks, which the literal "*" check alone would miss.
    """
    import ipaddress

    entries = [entry.strip() for entry in settings.FORWARDED_ALLOW_IPS.split(",") if entry.strip()]

    if "*" in entries:
        raise RuntimeError(
            "STARTUP ABORTED: FORWARDED_ALLOW_IPS must not be '*'. uvicorn then takes the "
            "leftmost, client-controlled X-Forwarded-For entry, which lets any caller pick "
            "its own identity for the API failure tracker. Set it to the concrete address "
            "of your reverse proxy."
        )

    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            # Hostnames are resolved by uvicorn; nothing to validate here.
            continue
        if network.prefixlen == 0:
            raise RuntimeError(
                f"STARTUP ABORTED: FORWARDED_ALLOW_IPS entry '{entry}' trusts every address, "
                f"which has the same effect as '*'. List your reverse proxy explicitly."
            )


def check_tile_source() -> None:
    """
    Warn when map rendering would fetch tiles from a third party.

    Without KARTO_PMTILES_PATH the worker falls back to staticmap3, whose default tile
    URL is tile.openstreetmap.org. Every finished trip then requests tiles for exactly
    that trip's bounding box at high zoom, handing a derivable movement profile of every
    user to an outside party — the opposite of what a self-hosted tracker is for.
    """
    if settings.KARTO_PMTILES_PATH or settings.KARTO_TILE_URL_TEMPLATE:
        return

    if settings.KARTO_ALLOW_EXTERNAL_TILES:
        logger.warning(
            "KARTO_ALLOW_EXTERNAL_TILES is enabled and no local basemap is configured: "
            "every rendered trip will request tiles from tile.openstreetmap.org, "
            "disclosing that trip's location to a third party."
        )
    else:
        logger.warning(
            "No tile source configured (KARTO_PMTILES_PATH / KARTO_TILE_URL_TEMPLATE). "
            "Map previews will be skipped rather than silently fetching tiles from "
            "tile.openstreetmap.org. Trip tracking itself is unaffected."
        )


def run_all() -> None:
    check_jwt_configuration()
    check_proxy_configuration()
    check_tile_source()
