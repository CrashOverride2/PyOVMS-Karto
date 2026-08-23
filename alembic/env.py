import sys
from pathlib import Path
import logging

import sqlalchemy
from sqlalchemy import pool
from sqlalchemy.engine.url import make_url

from alembic import context

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

config = context.config

logger = logging.getLogger("alembic.env")

from app.models import Base
from app.config import settings

target_metadata = Base.metadata

def get_url():
    """Returns the database URL from Pydantic settings and logs it."""
    url_str = settings.DATABASE_URL
    url_obj = make_url(url_str)
    
    logger.debug(f"Alembic connecting to: {url_obj.render_as_string(hide_password=True)}")
    
    return url_str


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    logger.debug("Running migrations in 'offline' mode.")
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    logger.debug("Running migrations in 'online' mode.")
    
    connectable = sqlalchemy.create_engine(
        get_url(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 10},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, 
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()