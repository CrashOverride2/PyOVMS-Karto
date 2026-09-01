from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.engine import Engine
from typing import Optional

from .config import settings

engine: Optional[Engine] = None
SessionLocal: Optional[sessionmaker] = None
Base = declarative_base()

ovms_engine: Optional[Engine] = None
OvmsSessionLocal: Optional[sessionmaker] = None


def connect_args_for(database_url: str) -> dict:
    """DBAPI connect() arguments for a database URL.

    PostgreSQL is pinned to UTC. `timestamptz` is returned *in the session's TimeZone*,
    which defaults to whatever the server, the database or the role happens to be set to.
    Karto reads instants and writes them back out as text — `_to_iso_z()` in the GPX
    export and the "UTC" suffix in the KML description both format the value directly —
    so a host configured for Europe/Berlin produced a Berlin wall clock labelled Z or
    UTC. date_trunc() and extract('isodow') have the same exposure: they cut the
    statistics on the session's day boundaries, not on UTC ones.

    Both engines get it. The OVMS database is read-only here but supplies the timestamps
    the ownership cutoffs compare against, and it need not be PostgreSQL at all — the
    OVMS server defaults to SQLite — so the argument follows the URL rather than being
    assumed.
    """
    if database_url.startswith("postgresql"):
        return {"options": "-c timezone=UTC"}
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def init_db_engines():
    """
    Creates both database engines and configures SessionLocals.
    """
    global engine, SessionLocal, ovms_engine, OvmsSessionLocal

    # Bound both pools. Without pool_timeout a caller that cannot get a connection waits
    # forever, so a burst of MQTT work (each message opened its own session) turned into
    # an unbounded pile of blocked tasks instead of a visible, recoverable error. The
    # limits also stop Karto from exhausting the shared PostgreSQL server's connection
    # slots, which would take the OVMS main server down with it.
    _POOL_KWARGS = {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT_SECONDS,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }

    if engine is None:
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"connect_timeout": 10, **connect_args_for(settings.DATABASE_URL)},
            **_POOL_KWARGS,
        )
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    if ovms_engine is None:
        ovms_engine = create_engine(
            settings.OVMS_DATABASE_URL,
            connect_args=connect_args_for(settings.OVMS_DATABASE_URL),
            **_POOL_KWARGS,
        )
        OvmsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=ovms_engine)

def get_db():
    """FastAPI dependency for Karto's primary database."""
    if SessionLocal is None:
        raise RuntimeError("Karto database is not initialized.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_ovms_db():
    """FastAPI dependency for the read-only OVMS database."""
    if OvmsSessionLocal is None:
        raise RuntimeError("OVMS database connection is not initialized.")
    db = OvmsSessionLocal()
    try:
        yield db
    finally:
        db.close()