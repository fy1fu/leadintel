from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from app.db.models import Base
from app.db.timeouts import pg_connect_args

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("LEADINTEL_DATABASE_URL")
    if not url:
        raise RuntimeError("LEADINTEL_DATABASE_URL is not set")
    return url


def include_object(obj, name, type_, reflected, compare_to):
    # PostGIS/tiger system tables are not ours
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def run_migrations_online() -> None:
    url = _url()
    engine = create_engine(url, **pg_connect_args(url))
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, include_object=include_object,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
