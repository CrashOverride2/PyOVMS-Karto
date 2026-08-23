import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import AuthFailureState, AuthIPBan

logger = logging.getLogger(__name__)


class PersistentAPIFailureTracker:
    """Tracks failed API authentication attempts in the database."""

    def __init__(self, window_seconds: int):
        self._window = timedelta(seconds=window_seconds)

    def record_failure_and_get_count(self, db: Session, ip_address: str) -> int:
        """Record a failed auth attempt and return count in current time window."""
        now = datetime.now(timezone.utc)
        state = db.query(AuthFailureState).filter(AuthFailureState.ip_address == ip_address).first()

        if not state:
            state = AuthFailureState(
                ip_address=ip_address,
                failure_count=1,
                window_started_at=now,
                last_failure_at=now,
            )
            db.add(state)
            db.commit()
            return 1

        if now - state.window_started_at >= self._window:
            state.failure_count = 1
            state.window_started_at = now
        else:
            state.failure_count += 1

        state.last_failure_at = now
        db.commit()
        return state.failure_count

    def clear_failures(self, db: Session, ip_address: str) -> None:
        """Clear failure state after a successful authentication."""
        db.query(AuthFailureState).filter(AuthFailureState.ip_address == ip_address).delete()
        db.commit()


class PersistentIPBanManager:
    """Stores temporary IP bans in the database."""

    def ban_ip(self, db: Session, ip_address: str, duration_minutes: int) -> None:
        banned_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        ban = db.query(AuthIPBan).filter(AuthIPBan.ip_address == ip_address).first()

        if not ban:
            ban = AuthIPBan(ip_address=ip_address, banned_until=banned_until)
            db.add(ban)
        else:
            ban.banned_until = banned_until

        db.commit()
        logger.warning(f"IP address {ip_address} has been banned until {banned_until.isoformat()}")

    def is_banned(self, db: Session, ip_address: str) -> bool:
        ban = db.query(AuthIPBan).filter(AuthIPBan.ip_address == ip_address).first()
        if not ban:
            return False

        now = datetime.now(timezone.utc)
        if now < ban.banned_until:
            return True

        db.delete(ban)
        db.commit()
        logger.debug(f"Ban has expired for IP address {ip_address}.")
        return False


failure_tracker = PersistentAPIFailureTracker(window_seconds=settings.API_FAIL_WINDOW_SECONDS)
ban_manager = PersistentIPBanManager()
