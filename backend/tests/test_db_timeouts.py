"""Default PostgreSQL connect timeout (5 s) on every engine; user-configured timeouts are never overridden.

Found during the Windows validation (V12): with no timeout, a refused connection waited psycopg's
130 s default on Windows instead of failing at once.
"""
import time

import psycopg
import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.db.session import DatabaseUnavailable, check_database, make_engine
from app.db.timeouts import PG_CONNECT_TIMEOUT_S, pg_connect_args
from app.net.store import _make_engine as make_store_engine

PG = "postgresql+psycopg://nobody@127.0.0.1:1/none"


class _Captured(Exception):
    pass


@pytest.fixture
def captured(monkeypatch):
    """Record the keyword arguments every engine passes to psycopg.connect, without connecting."""
    calls: list[dict] = []

    def fake_connect(*args, **kwargs):
        calls.append(kwargs)
        raise _Captured()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    return calls


def _connect(engine):
    with pytest.raises(Exception):
        with engine.connect():
            pass


def test_helper_defaults_only_for_postgresql(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    assert PG_CONNECT_TIMEOUT_S == 5
    assert pg_connect_args(PG) == {"connect_args": {"connect_timeout": 5}}
    assert pg_connect_args("postgresql://u@h/db") == {"connect_args": {"connect_timeout": 5}}
    assert pg_connect_args("sqlite:///x.db") == {} and pg_connect_args("sqlite://") == {}


def test_helper_respects_url_timeout_and_pgconnect_timeout(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    assert pg_connect_args(PG + "?connect_timeout=12") == {}
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "20")
    assert pg_connect_args(PG) == {}


def test_business_engine_uses_default_timeout(captured):
    _connect(make_engine(Settings(database_url=PG)))
    assert captured and captured[0]["connect_timeout"] == 5


def test_business_engine_keeps_url_timeout(captured):
    _connect(make_engine(Settings(database_url=PG + "?connect_timeout=12")))
    assert str(captured[0]["connect_timeout"]) == "12"


def test_business_engine_keeps_pgconnect_timeout_env(captured, monkeypatch):
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "20")
    _connect(make_engine(Settings(database_url=PG)))
    assert "connect_timeout" not in captured[0]          # libpq reads PGCONNECT_TIMEOUT itself


def test_diagnostics_store_postgres_default_and_sqlite_untouched(captured, tmp_path):
    _connect(make_store_engine(PG))
    assert captured[0]["connect_timeout"] == 5
    _connect(make_store_engine(PG + "?connect_timeout=12"))
    assert str(captured[1]["connect_timeout"]) == "12"
    eng = make_store_engine(f"sqlite:///{tmp_path / 'netdiag.db'}")  # SQLite: no connect_timeout, still works
    with eng.connect() as c:
        assert c.execute(text("select 1")).scalar() == 1
    assert len(captured) == 2


@pytest.mark.parametrize("suffix,expected", [("", "5"), ("?connect_timeout=12", "12")])
def test_migrations_engine_default_and_url_timeout(captured, suffix, expected):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    root = Path(__file__).parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", PG + suffix)
    with pytest.raises(Exception):
        command.current(cfg)
    assert str(captured[0]["connect_timeout"]) == expected


def test_unreachable_database_fails_fast_through_the_real_driver(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    t = time.monotonic()
    with pytest.raises(DatabaseUnavailable):
        check_database(make_engine(Settings(database_url=PG)))
    assert time.monotonic() - t < PG_CONNECT_TIMEOUT_S + 5      # was 130 s on Windows before the fix
