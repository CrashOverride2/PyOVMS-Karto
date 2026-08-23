import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader
from jwt import ExpiredSignatureError, InvalidTokenError
from sqlalchemy.orm import Session

from . import jwt_keys
from .config import settings
from .database import get_db as get_karto_db
from .database import get_ovms_db
from .exceptions import IPBannedException
from .models_ovms import ApiKey
from .models_ovms import User as OvmsUser
from .rate_limiter import ban_manager, failure_tracker

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_user_from_api_key(api_key_value: str, db: Session) -> Optional[OvmsUser]:
    """Validates an API key against the OVMS database."""
    hashed_key = hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()
    db_api_key = db.query(ApiKey).filter(ApiKey.hashed_key == hashed_key).first()

    if not db_api_key or not db_api_key.is_active or not db_api_key.user:
        return None

    expires_at = db_api_key.expires_at
    if expires_at is not None:
        # Only a naive value is UTC by convention. Overwriting the tzinfo of an aware
        # one — which the shared PostgreSQL returns for timestamptz columns — shifts the
        # deadline by the session's UTC offset, so a key is accepted for hours after it
        # expired or rejected hours early. The OVMS server handles it the same way in
        # dependencies.get_user_from_api_key().
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            logger.warning(f"Rejected expired API key with prefix: {api_key_value[:8]}...")
            return None

    return db_api_key.user


def get_user_from_jwt(token: str, db: Session) -> Optional[OvmsUser]:
    """Validates a JWT from a cookie using a standard JWT library."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials (JWT)",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token_value = ""
        parts = token.strip().split()

        if len(parts) == 2 and parts[0].lower() == "bearer":
            token_value = parts[1]
        elif len(parts) == 1:
            token_value = parts[0]
        else:
            logger.error("Malformed token. Expected 'Bearer <token>' or '<token>', but format is invalid.")
            raise credentials_exception

        payload_data = jwt.decode(
            token_value,
            jwt_keys.get_public_key(),
            algorithms=[jwt_keys.JWT_ALGORITHM],
            audience=jwt_keys.JWT_AUDIENCE,
            issuer=jwt_keys.JWT_ISSUER,
            options={"require": ["sub", "exp", "aud", "iss"]},
        )

        username: Optional[str] = payload_data.get("sub")
        mfa_completed: bool = payload_data.get("mfa", False)
        token_ver: int = payload_data.get("ver", 0)

        if not username:
            logger.warning("JWT is missing 'sub' (username) claim.")
            raise credentials_exception

        user = db.query(OvmsUser).filter(OvmsUser.username == username).first()
        if not user:
            logger.warning(f"User '{username}' from JWT not found in the database.")
            raise credentials_exception

        # Honour the OVMS server's JWT revocation. It bumps users.token_version on
        # password change/reset; without this check a token issued before a
        # compromise-driven password reset would keep reading the full GPS trip
        # history here until it expired on its own.
        if token_ver != (user.token_version or 0):
            logger.warning(f"Rejected JWT for user '{username}': token version is stale (revoked).")
            raise credentials_exception

        if not mfa_completed:
            logger.warning(f"Rejected JWT for user '{username}' due to missing MFA completion.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="2FA not completed in token session.",
            )

        return user

    except ExpiredSignatureError:
        logger.warning("Rejected expired JWT token.")
        raise credentials_exception
    except InvalidTokenError as e:
        logger.warning(f"Rejected invalid JWT token: {e}")
        raise credentials_exception
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"An unexpected error occurred during JWT validation: {e}", exc_info=True)
        raise credentials_exception


async def get_current_user(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
    ovms_db: Session = Depends(get_ovms_db),
    karto_db: Session = Depends(get_karto_db),
) -> OvmsUser:
    """
    FastAPI dependency that authenticates a user via either an API key
    or a JWT from a browser cookie. It also enforces IP-based rate limiting
    for failed API key attempts.
    """
    user: Optional[OvmsUser] = None
    ip_address = request.client.host if request.client else "unknown"

    if ban_manager.is_banned(karto_db, ip_address):
        raise IPBannedException(ip_address)

    if api_key:
        user = get_user_from_api_key(api_key, ovms_db)
        if not user:
            failure_count = failure_tracker.record_failure_and_get_count(karto_db, ip_address)
            logger.debug(f"Invalid API key attempt from {ip_address}. Failure count: {failure_count}.")

            if failure_count >= settings.API_FAIL_LIMIT:
                ban_manager.ban_ip(karto_db, ip_address, duration_minutes=settings.API_BAN_MINUTES)
        else:
            failure_tracker.clear_failures(karto_db, ip_address)
    else:
        # Prefer the __Host- prefixed cookie; accept the unprefixed name only over
        # plain HTTP.
        #
        # Same rule as the main server's dependencies._get_access_token(), and for the
        # same reason: a __Host- cookie can only be set by the exact host over a secure
        # connection, so a sibling subdomain cannot write one. The unprefixed name has
        # no such rule, so accepting it over HTTPS let any subdomain fix a visitor into
        # a session of its choosing — and this service serves the full GPS trip history.
        #
        # Keyed off the request scheme rather than a FORCE_SECURE_COOKIES setting of our
        # own: the two services must not be able to disagree about this via two separate
        # .env files, and behind the bundled reverse proxy (which sets X-Forwarded-Proto)
        # the scheme is already the authoritative answer. Plain HTTP means the local test
        # server, where the prefix cannot be used at all.
        token = request.cookies.get("__Host-access_token")
        if not token and request.url.scheme != "https":
            token = request.cookies.get("access_token")
        if token:
            user = get_user_from_jwt(token, ovms_db)
            if not user:
                # Count cookie failures too. Only the API-key branch used to do this, so
                # an attacker guessing at forged or stale tokens was never rate limited
                # here — and Karto's ban table is separate from the main server's, so
                # nothing else was counting either.
                failure_count = failure_tracker.record_failure_and_get_count(karto_db, ip_address)
                logger.debug(
                    f"Invalid session token from {ip_address}. Failure count: {failure_count}."
                )
                if failure_count >= settings.API_FAIL_LIMIT:
                    ban_manager.ban_ip(karto_db, ip_address, duration_minutes=settings.API_BAN_MINUTES)
            else:
                failure_tracker.clear_failures(karto_db, ip_address)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    return user
