"""Phase B0: the fail-closed commit gate.

Every SQLAlchemy commit (ORM Session or Connection) is refused unless the engine is exempt (diagnostics,
the lock connection), carries the explicit test bypass, or an active guard holds the maintenance lock for
the same database and confirms it right before the commit. After a refusal, an INDEPENDENT connection
proves that nothing became visible.

SQLite cases run everywhere (the gate applies to every engine); the PostgreSQL cases use leadintel_test only
and are skipped when LEADINTEL_TEST_DATABASE_URL is not set. No database is created or dropped.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, String, create_engine, event, insert, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db import locks
from app.db.locks import DbIdentity, LockLost, UnlockedWrite, maintenance_lock
from test_maintenance_lock import IDENT, FakeBackend, guard_with


class _Base(DeclarativeBase):
    pass


class Item(_Base):
    __tablename__ = "item"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


@pytest.fixture
def sqlite_db(tmp_path):
    """A SQLite file with one table, created with the standard-library driver (outside SQLAlchemy)."""
    path = tmp_path / "gate.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name VARCHAR(40))")
    return path


def _independent_count(path) -> int:
    with sqlite3.connect(path) as raw:
        return raw.execute("SELECT count(*) FROM item").fetchone()[0]


# ------------------------------------------------------------ F1 / C1-C5 on SQLite: no guard -> nothing commits
def test_c1_orm_commit_without_flush_is_refused(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with Session(e) as s:
        s.add(Item(name="a"))
        with pytest.raises(UnlockedWrite):
            s.commit()
    assert _independent_count(sqlite_db) == 0


def test_c2_orm_commit_after_explicit_flush_is_refused_and_rolled_back(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with Session(e) as s:
        s.add(Item(name="a"))
        s.flush()                                   # the INSERT has been sent, uncommitted
        with pytest.raises(UnlockedWrite):
            s.commit()
    assert _independent_count(sqlite_db) == 0


def test_c3_orm_commit_after_autoflush_is_refused(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with Session(e) as s:
        s.add(Item(name="a"))
        assert s.execute(select(Item)).scalars().all()      # autoflush sent the INSERT
        with pytest.raises(UnlockedWrite):
            s.commit()
    assert _independent_count(sqlite_db) == 0


def test_c4_connection_commit_is_refused(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with e.connect() as c:
        c.execute(insert(Item.__table__).values(name="a"))
        with pytest.raises(UnlockedWrite):
            c.commit()
    assert _independent_count(sqlite_db) == 0


def test_c5_engine_begin_block_is_refused_and_rolled_back(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with pytest.raises(UnlockedWrite):
        with e.begin() as c:
            c.execute(insert(Item.__table__).values(name="a"))
    assert _independent_count(sqlite_db) == 0
    with e.connect() as c:                                   # the pooled connection is usable again
        assert c.execute(text("SELECT count(*) FROM item")).scalar() == 0


def test_control_the_same_writes_commit_when_the_gate_allows_them(sqlite_db):
    """Control for C1-C5: identical writes through an exempt engine do commit, so the refusals above are the
    gate's doing and not a side effect of the test setup."""
    e = locks.exempt_engine(create_engine(f"sqlite:///{sqlite_db}"))
    with Session(e) as s:
        s.add(Item(name="a"))
        s.commit()
    with e.begin() as c:
        c.execute(insert(Item.__table__).values(name="b"))
    assert _independent_count(sqlite_db) == 2


def test_f2_reading_needs_no_guard(sqlite_db):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with Session(e) as s:
        assert s.execute(select(Item)).scalars().all() == []
    with e.connect() as c:
        assert c.execute(text("SELECT 1")).scalar() == 1


def test_g1a_unrelated_sqlite_engine_cannot_commit_under_a_guard(sqlite_db, tmp_path):
    e = create_engine(f"sqlite:///{sqlite_db}")
    with guard_with(FakeBackend(), data_dir=tmp_path) as g:
        with Session(e) as s:
            s.add(Item(name="a"))
            with pytest.raises(UnlockedWrite, match="not to the database the maintenance lock protects"):
                s.commit()
        assert g.permitted["commit"] == 0
    assert _independent_count(sqlite_db) == 0


def test_f6_diagnostics_store_is_exempt_and_works_without_a_lock(tmp_path):
    from app.net.store import NetStore
    store = NetStore(f"sqlite:///{tmp_path / 'netdiag.db'}")
    store.record_event("svc", "download", True, url="https://example.invalid/x")
    with sqlite3.connect(tmp_path / "netdiag.db") as raw:
        assert raw.execute("SELECT count(*) FROM net_event").fetchone()[0] == 1


def test_f3_database_bypass_refuses_non_postgresql_engines():
    with pytest.raises(RuntimeError, match="PostgreSQL test databases only"):
        locks.allow_unlocked_writes_for_tests(create_engine("sqlite://"))


# ------------------------------------------------------------ identity comparison (no database)
class _FakeConn:
    def __init__(self, ident, engine):
        self.engine, self.dialect, self.info = engine, SimpleNamespace(name="postgresql"), {"leadintel_identity": ident}


def test_g1b_identity_mismatch_is_refused_and_match_is_authorised(tmp_path):
    engine = create_engine("sqlite://")              # any non-exempt engine object; only its identity matters
    with guard_with(FakeBackend(), data_dir=tmp_path) as g:
        for other in (DbIdentity(IDENT.database, IDENT.oid, IDENT.server_addr, 5433, IDENT.started),
                      DbIdentity(IDENT.database, IDENT.oid, IDENT.server_addr, IDENT.server_port, "restarted"),
                      DbIdentity("leadintel_portability_check", 99, IDENT.server_addr, IDENT.server_port,
                                 IDENT.started)):
            with pytest.raises(UnlockedWrite):
                locks._gate(_FakeConn(other, engine))
        assert g.permitted["commit"] == 0
        locks._gate(_FakeConn(IDENT, engine))
        assert g.permitted["commit"] == 1


def test_gate_refuses_once_the_guard_is_lost(tmp_path):
    engine = create_engine("sqlite://")
    state = {"ok": True}
    with guard_with(FakeBackend(check=lambda: state["ok"]), data_dir=tmp_path) as g:
        state["ok"] = False
        with pytest.raises(LockLost):
            g.ensure()
        with pytest.raises(LockLost):
            locks._gate(_FakeConn(IDENT, engine))


# ============================================================ PostgreSQL (leadintel_test only)
TEST_DB_URL = os.environ.get("LEADINTEL_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_DB_URL, reason="LEADINTEL_TEST_DATABASE_URL not set")


def _sources(pg_engine):
    with pg_engine.connect() as c:
        return c.execute(text("SELECT key FROM source ORDER BY key")).scalars().all()


def _add_source(conn_or_session, key):
    conn_or_session.execute(text("INSERT INTO source (key, name, reliability, cost_class, enabled) "
                                 "VALUES (:k, :k, '{}', 'free', true)"), {"k": key})


def _lose_lock(pg_engine, g):
    with pg_engine.connect() as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name LIKE 'leadintel:test:%'"))
    with pytest.raises(LockLost):
        g.ensure()


@needs_pg
@pytest.mark.parametrize("mode", ["orm_noflush", "orm_flush", "orm_autoflush", "connection", "begin_block"])
def test_c1_to_c5_postgresql_nothing_commits_after_the_lock_is_lost(db, tmp_path, mode):
    e = create_engine(TEST_DB_URL)
    with maintenance_lock(e, "test", data_dir=tmp_path) as g:
        with Session(e) as s:                                   # the guard works while the lock is held
            _add_source(s, "kept")
            s.commit()
        _lose_lock(db, g)
        with pytest.raises(LockLost):
            if mode.startswith("orm"):
                with Session(e) as s:
                    from app.db.models import Source
                    s.add(Source(key="lost", name="lost", reliability={}))
                    if mode == "orm_flush":
                        s.flush()
                    elif mode == "orm_autoflush":
                        s.execute(text("SELECT count(*) FROM source")).scalar()
                    s.commit()
            elif mode == "connection":
                with e.connect() as c:
                    _add_source(c, "lost")
                    c.commit()
            else:
                with e.begin() as c:
                    _add_source(c, "lost")
    assert _sources(db) == ["kept"]                              # independent connection: nothing else visible


@needs_pg
def test_c6_no_connection_is_left_inside_a_transaction(db, tmp_path):
    e = create_engine(TEST_DB_URL, pool_size=2)
    with maintenance_lock(e, "test", data_dir=tmp_path) as g:
        _lose_lock(db, g)
        for _ in range(2):                       # every commit style: ORM, direct connection, engine.begin()
            with pytest.raises(LockLost):
                with Session(e) as s:
                    _add_source(s, "x")
                    s.commit()
            with pytest.raises(LockLost):
                with e.connect() as c:
                    _add_source(c, "y")
                    c.commit()
            with pytest.raises(LockLost):
                with e.begin() as c:
                    _add_source(c, "z")
    with db.connect() as c:
        states = c.execute(text("SELECT state FROM pg_stat_activity WHERE datname = current_database() "
                                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'")).all()
    assert states == []
    with maintenance_lock(e, "test", data_dir=tmp_path):            # a later locked operation works
        with Session(e) as s:
            _add_source(s, "later")
            s.commit()
    assert _sources(db) == ["later"]


@needs_pg
def test_f1_new_postgresql_engine_fails_closed_without_a_guard(db):
    e = create_engine(TEST_DB_URL)
    with pytest.raises(UnlockedWrite):
        with Session(e) as s:
            _add_source(s, "x")
            s.commit()
    with pytest.raises(UnlockedWrite):
        with e.begin() as c:
            _add_source(c, "y")
    assert _sources(db) == []


@needs_pg
def test_g1c_g2_guarded_database_commits_and_alembic_engine_is_allowed(db, tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    e = create_engine(TEST_DB_URL)
    with maintenance_lock(e, "test", data_dir=tmp_path):
        with Session(e) as s:
            _add_source(s, "ok")
            s.commit()
        root = Path(__file__).parents[1]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
        command.upgrade(cfg, "head")                   # already at head: still passes the gate (same identity)
    assert _sources(db) == ["ok"]


@needs_pg
def test_tl1_a_commit_already_authorised_when_the_lock_is_lost_completes_and_the_next_is_refused(db, tmp_path):
    e = create_engine(TEST_DB_URL)
    killed = threading.Event()

    def kill_after_gate(conn):                         # runs after the gate's successful check, before COMMIT
        if not killed.is_set():
            killed.set()
            with db.connect() as c:
                c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                               "WHERE application_name LIKE 'leadintel:test:%'"))
    with maintenance_lock(e, "test", data_dir=tmp_path) as g:
        event.listen(e, "commit", kill_after_gate)
        with Session(e) as s:
            _add_source(s, "in_flight")
            s.commit()                                 # authorised before the loss: completes (known gap, B0.5)
        with pytest.raises(LockLost):
            with Session(e) as s:
                _add_source(s, "after")
                s.commit()
        assert g.lost
    assert _sources(db) == ["in_flight"]
