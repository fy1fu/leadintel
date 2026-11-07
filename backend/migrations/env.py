from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from app.db import locks  # noqa: F401 - migrations commit through the same fail-closed gate
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


def _migrate(conn) -> None:
    context.configure(connection=conn, target_metadata=target_metadata, include_object=include_object,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller may pass its own connection (the test fixture does, on its explicitly bypassed test engine).
    # Otherwise a dedicated engine is used; its commits pass the gate only under the maintenance lock
    # for this same database (`leadintel db upgrade` takes it).
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        _migrate(supplied)
        return
    url = _url()
    engine = create_engine(url, **pg_connect_args(url))
    with engine.connect() as conn:
        _migrate(conn)


run_migrations_online()
