"""Business database engine/session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.timeouts import pg_connect_args


class DatabaseUnavailable(RuntimeError):
    pass


def make_engine(settings: Settings) -> Engine:
    url = settings.business_db_url
    return create_engine(url, pool_pre_ping=True, future=True, **pg_connect_args(url))


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(f"business database unavailable: {type(exc).__name__}: {exc}") from exc


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    s = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
