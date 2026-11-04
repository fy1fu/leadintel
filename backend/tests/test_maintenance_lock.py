"""Phase B0: shared maintenance lock, lost-lock detection and the managed-file gate.

Part 1 tests the guard's logic with a fake lock backend (no database needed): bounded waits, permanent loss,
single-threaded access to the lock connection, cancellation, and that a late answer can never re-authorise
writes. Part 2 repeats the essential cases against real PostgreSQL (leadintel_test only) and is skipped when
LEADINTEL_TEST_DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import locks
from app.db.locks import (CHECK_TIMEOUT_S, EXIT_LOCK_HELD, EXIT_LOCK_LOST, HEARTBEAT_S, WRITE_CHECK_BYTES, DbIdentity,
                          LockGuard, LockLost, OperationInProgress, UnlockedWrite, current_guard, ensure_write_allowed,
                          maintenance_lock)

IDENT = DbIdentity("leadintel_test", 16384, "127.0.0.1", 5432, "2026-11-03 00:00:00+00")


class FakeBackend:
    """Records every call and the thread it ran on; `check` behaviour is configurable."""

    def __init__(self, *, acquired=True, holder=None, check=None, cancel_raises=False):
        self.acquired, self.holder = acquired, holder
        self._check = check or (lambda: True)
        self.cancel_raises = cancel_raises
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.overlap = False
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.cancel_thread = None

    def _rec(self, name):
        self.calls.append((name, threading.get_ident()))
        self.active += 1
        if self.active > 1:
            self.overlap = True

    def acquire(self):
        self._rec("acquire")
        self.active -= 1
        return (True, IDENT, None) if self.acquired else (False, None, self.holder)

    def check(self):
        self._rec("check")
        try:
            return self._check()
        finally:
            self.active -= 1

    def release(self):
        self._rec("release")
        self.active -= 1

    def close(self):
        self._rec("close")
        self.active -= 1
        self.closed.set()

    def cancel(self):
        self.cancel_thread = threading.get_ident()
        self.cancelled.set()
        if self.cancel_raises:
            raise RuntimeError("simulated CancellationTimeout")

    def threads(self, *names):
        return {t for n, t in self.calls if not names or n in names}

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


@contextmanager
def guard_with(backend, data_dir=None, **kw):
    kw.setdefault("heartbeat_s", 3600)  # heartbeat off unless a test asks for it
    with maintenance_lock(None, "test", data_dir=data_dir, backend=backend, **kw) as g:
        yield g


# ============================================================ part 1: logic (no database)
def test_documented_constants_and_exit_codes():
    assert (CHECK_TIMEOUT_S, HEARTBEAT_S, WRITE_CHECK_BYTES) == (3.0, 2.0, 8 * 1024 * 1024)
    assert (EXIT_LOCK_HELD, EXIT_LOCK_LOST, locks.LOCK_KEY) == (5, 6, 0x4C454144)


def test_lock_held_elsewhere_is_refused_with_the_holder_named_and_cleaned_up_on_the_worker():
    b = FakeBackend(acquired=False, holder="leadintel:discover:4242 (database process 77)")
    with pytest.raises(OperationInProgress, match="leadintel:discover:4242"):
        with guard_with(b):
            pass
    assert b.closed.wait(2) and len(b.threads()) == 1   # acquire and close both on the worker thread
    assert current_guard() is None


def test_normal_use_checks_on_the_worker_and_releases_on_exit():
    b = FakeBackend()
    with guard_with(b) as g:
        assert current_guard() is g and g.identity == IDENT
        g.ensure()
        g.authorize("commit")
        assert g.permitted["commit"] == 1
    assert current_guard() is None
    assert b.closed.wait(2) and b.count("release") == 1
    assert b.threads() == b.threads("acquire") and threading.get_ident() not in b.threads()


def test_nested_use_is_refused():
    with guard_with(FakeBackend()):
        with pytest.raises(RuntimeError, match="already held"):
            with guard_with(FakeBackend()):
                pass


def test_t1_unanswered_check_marks_the_guard_lost_within_the_default_timeout_and_for_good():
    hang = threading.Event()
    b = FakeBackend(check=lambda: hang.wait(30) or True)
    try:
        with guard_with(b) as g:
            t0 = time.monotonic()
            with pytest.raises(LockLost, match="did not answer within 3 s"):
                g.ensure()
            waited = time.monotonic() - t0
            assert CHECK_TIMEOUT_S - 0.1 <= waited < CHECK_TIMEOUT_S + 1.0
            assert g.lost and b.cancelled.wait(2)                  # cancellation was requested ...
            assert b.cancel_thread != threading.get_ident()        # ... from a helper thread, not the caller
            t1 = time.monotonic()
            for _ in range(100):
                with pytest.raises(LockLost):
                    g.authorize("commit")
            assert time.monotonic() - t1 < 0.5                     # refused at once: no further waiting
            assert g.permitted == {"commit": 0, "file": 0}
            assert not b.closed.is_set()                           # nobody closed the busy connection
    finally:
        hang.set()


def test_t6_a_late_ok_from_a_stuck_check_never_re_authorises_and_the_worker_cleans_up_itself():
    release = threading.Event()
    b = FakeBackend(check=lambda: release.wait(30))
    with guard_with(b, check_timeout=0.3) as g:
        with pytest.raises(LockLost):
            g.ensure()
        checks_when_lost = b.count("check")
        release.set()                                   # the stuck check now answers "True" - too late
        assert b.closed.wait(3)                         # the worker unlocked and closed, on its own thread
        assert b.threads("release", "close") == b.threads("acquire")
        with pytest.raises(LockLost):
            g.ensure()
        with pytest.raises(LockLost):
            g.authorize("file")
        assert b.count("check") == checks_when_lost     # no further check ever reached the connection
    assert g.lost


def test_t7_failed_cancellation_leaves_the_guard_lost():
    hang = threading.Event()
    b = FakeBackend(check=lambda: hang.wait(30) or True, cancel_raises=True)
    try:
        with guard_with(b, check_timeout=0.2) as g:
            with pytest.raises(LockLost):
                g.ensure()
            assert b.cancelled.wait(2)
            with pytest.raises(LockLost):
                g.ensure()
            assert g.lost
    finally:
        hang.set()


def test_check_error_or_false_answer_marks_lost():
    for check in (lambda: False, lambda: (_ for _ in ()).throw(OSError("connection reset"))):
        with guard_with(FakeBackend(check=check)) as g:
            with pytest.raises(LockLost):
                g.ensure()
            assert g.lost and g.lost_reason


def test_t3_lock_connection_is_used_by_one_thread_only_under_concurrent_checks_and_heartbeat():
    b = FakeBackend(check=lambda: (time.sleep(0.001), True)[1])
    with guard_with(b, heartbeat_s=0.01) as g:
        def hammer():
            for _ in range(25):
                g.ensure()
        ts = [threading.Thread(target=hammer) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        time.sleep(0.1)
    assert b.count("check") >= 200
    assert len(b.threads()) == 1 and not b.overlap


def test_tl4_heartbeat_detects_loss_without_any_write():
    state = {"ok": True}
    with guard_with(FakeBackend(check=lambda: state["ok"]), heartbeat_s=0.1) as g:
        state["ok"] = False
        t0 = time.monotonic()
        while not g.lost and time.monotonic() - t0 < 3:
            time.sleep(0.02)
        assert g.lost, "heartbeat did not notice the lost lock"
        assert time.monotonic() - t0 < 1.0


def test_tl3_nothing_starts_after_the_guard_is_marked_lost(tmp_path):
    state = {"ok": True}
    b = FakeBackend(check=lambda: state["ok"])
    with guard_with(b, data_dir=tmp_path) as g:
        ensure_write_allowed(tmp_path / "a.txt")
        assert g.permitted["file"] == 1
        state["ok"] = False
        with pytest.raises(LockLost):
            g.ensure()
        checks = b.count("check")
        for i in range(100):
            with pytest.raises(LockLost):
                g.authorize("commit")
            with pytest.raises(LockLost):
                ensure_write_allowed(tmp_path / f"f{i}.txt")
        assert g.permitted == {"commit": 0, "file": 1} and b.count("check") == checks
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------ managed-file gate
def test_file_gate_fails_closed_without_a_guard(tmp_path):
    with pytest.raises(UnlockedWrite, match="maintenance lock is not held"):
        ensure_write_allowed(tmp_path / "x")


def test_g3_file_gate_is_bound_to_the_guarded_data_folder(tmp_path):
    data, other = tmp_path / "data", tmp_path / "elsewhere"
    with guard_with(FakeBackend(), data_dir=data) as g:
        ensure_write_allowed(data / "dbpr" / "x.csv")
        with pytest.raises(UnlockedWrite, match="outside the data folder"):
            ensure_write_allowed(other / "x.csv")
        with pytest.raises(UnlockedWrite, match="outside the data folder"):
            ensure_write_allowed(data / ".." / "elsewhere" / "x.csv")
        assert g.permitted["file"] == 1


def test_f4_file_bypass_is_limited_to_its_root_and_to_temporary_folders(tmp_path):
    inside, outside = tmp_path / "a", tmp_path / "b"
    with locks.allow_unlocked_file_writes_for_tests(inside):
        ensure_write_allowed(inside / "x")
        with pytest.raises(UnlockedWrite):
            ensure_write_allowed(outside / "x")
    with pytest.raises(UnlockedWrite):
        ensure_write_allowed(inside / "x")            # bypass ends with the block
    outside_temp = Path(tempfile.gettempdir()).resolve().parent      # never inside the temporary folder
    with pytest.raises(RuntimeError, match="temporary folders"):
        with locks.allow_unlocked_file_writes_for_tests(outside_temp):
            pass


def test_f3_bypasses_refuse_outside_pytest(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="automated tests only"):
        with locks.allow_unlocked_file_writes_for_tests(tmp_path):
            pass
    from sqlalchemy import create_engine
    with pytest.raises(RuntimeError, match="automated tests only"):
        locks.allow_unlocked_writes_for_tests(create_engine("sqlite://"))


# ------------------------------------------------------------ LL2 / LL3: writers stop after a loss
class _Resp:
    def __init__(self, n_bytes, chunk=1 << 20):
        self.status_code, self.headers, self._n, self._chunk = 200, {}, n_bytes, chunk

    def raise_for_status(self):
        return None

    def iter_bytes(self, _size):
        sent = 0
        while sent < self._n:
            k = min(self._chunk, self._n - sent)
            sent += k
            yield b"x" * k


class _Client:
    def __init__(self, n_bytes):
        self.n = n_bytes

    @contextmanager
    def stream(self, method, url, headers=None, **kw):
        yield _Resp(self.n)


def test_ll2_writers_refuse_before_creating_anything_once_the_lock_is_lost(tmp_path):
    from app.sources.common import _write_json_atomic, download_checked
    from app.sources.overture.snapshot import SnapshotStore
    data = tmp_path / "data"
    (data / "sunbiz").mkdir(parents=True)
    (data / "overture" / "2026-01-01.0").mkdir(parents=True)
    store = SnapshotStore.__new__(SnapshotStore)
    store.root = data / "overture"
    state = {"ok": True}
    with guard_with(FakeBackend(check=lambda: state["ok"]), data_dir=data) as g:
        state["ok"] = False
        with pytest.raises(LockLost):
            g.ensure()
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        with pytest.raises(LockLost):
            _write_json_atomic(data / "sunbiz" / "x.meta.json", {"a": 1})
        with pytest.raises(LockLost):
            download_checked(_Client(10), "https://example.invalid/f.txt", data / "sunbiz" / "f.txt",
                             expected_size=10, remote_last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(LockLost):
            SnapshotStore(data / "elsewhere")          # would create data/elsewhere/overture
        with pytest.raises(LockLost):
            store.cleanup(keep=0)                      # would delete data/overture/2026-01-01.0
        assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_ll3_download_in_progress_stops_at_the_next_8_mb_check(tmp_path):
    from app.sources.common import download_checked
    data = tmp_path / "data"
    (data / "sunbiz").mkdir(parents=True)
    n = {"checks": 0}

    def check():
        n["checks"] += 1
        return n["checks"] <= 2      # ok for the .part sidecar and opening the .part file; lost at the 8 MB check
    total = 40 * 1024 * 1024
    with guard_with(FakeBackend(check=check), data_dir=data):
        with pytest.raises(LockLost):
            download_checked(_Client(total), "https://example.invalid/f.txt", data / "sunbiz" / "f.txt",
                             expected_size=total, remote_last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc))
    part = data / "sunbiz" / "f.txt.part"
    assert part.exists() and part.stat().st_size == WRITE_CHECK_BYTES      # stopped at the first 8 MB check
    assert not list((data / "sunbiz").glob("f.*.txt"))                     # never moved into place
    assert not (data / "sunbiz" / "f.txt.meta.json").exists()              # no sidecar written


# ------------------------------------------------------------ LL6: CLI exit codes
def test_ll6_cli_exit_codes(monkeypatch):
    import app.cli as cli
    for exc, code in ((OperationInProgress("held by leadintel:score:1"), 5), (LockLost("gone"), 6)):
        monkeypatch.setattr(cli, "_business", lambda args, e=exc: (_ for _ in ()).throw(e))
        assert cli.main(["score"]) == code
    assert "5  another LeadIntel operation holds the maintenance lock" in cli.EXIT_CODES_HELP
    assert "6  the maintenance lock was lost" in cli.EXIT_CODES_HELP


# ============================================================ part 2: real PostgreSQL (leadintel_test only)
TEST_DB_URL = os.environ.get("LEADINTEL_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_DB_URL, reason="LEADINTEL_TEST_DATABASE_URL not set")


def _lock_rows(pg_engine):
    from sqlalchemy import text
    with pg_engine.connect() as c:
        return c.execute(text(
            "SELECT l.pid, a.application_name FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype='advisory' AND l.granted AND l.classid=0 AND l.objid=:o AND l.objsubid=1"),
            {"o": locks.LOCK_KEY}).all()


def _gated_engine():
    from sqlalchemy import create_engine
    return create_engine(TEST_DB_URL)


@contextmanager
def _held_elsewhere(tmp_path, operation="test"):
    """Hold the maintenance lock from another thread (its own context), as another LeadIntel process would."""
    ready, done, err = threading.Event(), threading.Event(), []

    def hold():
        try:
            with maintenance_lock(_gated_engine(), operation, data_dir=tmp_path):
                ready.set()
                done.wait(120)
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
            ready.set()
    t = threading.Thread(target=hold)
    t.start()
    assert ready.wait(30) and not err, err
    try:
        yield
    finally:
        done.set()
        t.join(30)


def _acquire_in_other_thread(tmp_path, operation="second") -> str:
    """Try to take the lock from another thread: 'acquired' or 'refused'."""
    out = {}

    def run():
        try:
            with maintenance_lock(_gated_engine(), operation, data_dir=tmp_path):
                out["r"] = "acquired"
        except OperationInProgress:
            out["r"] = "refused"
        except BaseException as exc:  # noqa: BLE001
            out["r"] = f"error: {exc!r}"
    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    return out.get("r", "no answer")


def _kill_lock_holder(pg_engine, operation="test"):
    from sqlalchemy import text
    with pg_engine.connect() as c:
        n = c.execute(text("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                           "WHERE application_name LIKE :a"), {"a": f"leadintel:{operation}:%"}).scalar()
    assert n == 1


@needs_pg
def test_l1_l2_real_lock_refuses_a_second_holder_and_is_released_after_exit_and_error(pg_engine, tmp_path):
    e = _gated_engine()
    with maintenance_lock(e, "test", data_dir=tmp_path) as g:
        assert g.identity.database.endswith("_test")
        [(pid, app)] = _lock_rows(pg_engine)
        assert app.startswith("leadintel:test:")
        t = {}

        def other():
            try:
                with maintenance_lock(_gated_engine(), "other", data_dir=tmp_path):
                    t["got"] = True
            except OperationInProgress as exc:
                t["refused"] = str(exc)
        th = threading.Thread(target=other)
        th.start()
        th.join(20)
        assert "leadintel:test:" in t["refused"] and "got" not in t
    assert _lock_rows(pg_engine) == []
    with pytest.raises(ValueError):
        with maintenance_lock(e, "test", data_dir=tmp_path):
            raise ValueError("boom")
    assert _lock_rows(pg_engine) == []                  # L2 (error) and L4: nothing left holding it


@needs_pg
def test_l3_lock_is_released_when_the_holder_session_is_terminated(pg_engine, tmp_path):
    with maintenance_lock(_gated_engine(), "test", data_dir=tmp_path) as g:
        _kill_lock_holder(pg_engine)
        with pytest.raises(LockLost):
            g.ensure()
    assert _lock_rows(pg_engine) == []
    with maintenance_lock(_gated_engine(), "again", data_dir=tmp_path):
        pass


@needs_pg
def test_l5_l6_writing_commands_are_refused_while_held_reading_commands_work(pg_engine, tmp_path, monkeypatch):
    import app.cli as cli
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    mapping = tmp_path / "m.yaml"
    mapping.write_text("business_name: Name\n")
    csv_file = tmp_path / "b.csv"
    csv_file.write_text("Name\nx\n")
    with _held_elsewhere(tmp_path):
        for argv in (["score"], ["config", "seed"], ["db", "upgrade"],
                     ["import-btr", str(csv_file), "--county", "duval", "--mapping", str(mapping)], ["discover"]):
            assert cli.main(argv) == 5, argv
        assert cli.main(["config", "show"]) == 0
        assert cli.main(["db", "status"]) == 0


@needs_pg
def test_t1_real_check_query_timeout_marks_lost(pg_engine, tmp_path):
    class Sleepy(locks.PgLockBackend):
        def check(self):
            from sqlalchemy import text
            self.conn.execute(text("SELECT pg_sleep(10)"))   # statement_timeout (2 s) or the 3 s wait ends it
            return True
    e = _gated_engine()
    with maintenance_lock(e, "test", data_dir=tmp_path, backend=Sleepy(e.url, "test")) as g:
        t0 = time.monotonic()
        with pytest.raises(LockLost):
            g.ensure()
        assert time.monotonic() - t0 < CHECK_TIMEOUT_S + 1.0
        with pytest.raises(LockLost):
            ensure_write_allowed(tmp_path / "x")


@needs_pg
def test_t2_t4_t5_unresponsive_lock_connection(pg_engine, tmp_path):
    from sqlalchemy.engine import make_url

    from pg_relay import PgRelay
    url = make_url(TEST_DB_URL)
    relay = PgRelay(url.host or "127.0.0.1", url.port or 5432)
    try:
        backend = locks.PgLockBackend(url.set(host="127.0.0.1", port=relay.port), "test")
        closes = []
        orig_close = backend.close
        backend.close = lambda: (closes.append(threading.get_ident()), orig_close())[1]
        with maintenance_lock(_gated_engine(), "test", data_dir=tmp_path, backend=backend) as g:
            relay.freeze()
            t0 = time.monotonic()
            with pytest.raises(LockLost):
                g.ensure()                                                   # T2: bounded, no hang
            assert time.monotonic() - t0 < CHECK_TIMEOUT_S + 1.0
            with pytest.raises(LockLost):
                ensure_write_allowed(tmp_path / "x")
            assert closes == []                                              # never closed from another thread
            assert len(_lock_rows(pg_engine)) == 1                           # T4: the server still holds it ...
            assert _acquire_in_other_thread(tmp_path) == "refused"           # ... so a second operation is refused
            relay.close()                                                    # T5: the connection really goes away
            t1 = time.monotonic()
            while _lock_rows(pg_engine) and time.monotonic() - t1 < 60:
                time.sleep(0.2)
            released_after = time.monotonic() - t1
            assert _lock_rows(pg_engine) == [], "server did not release the lock within 60 s"
            print(f"\nT5: server released the advisory lock {released_after:.2f} s after the relay closed")
            assert _acquire_in_other_thread(tmp_path) == "acquired"          # ... and now it can be acquired
    finally:
        relay.close()


@needs_pg
def test_t6_real_late_answer_after_thaw_does_not_reauthorise(pg_engine, tmp_path):
    from sqlalchemy.engine import make_url

    from pg_relay import PgRelay
    url = make_url(TEST_DB_URL)
    relay = PgRelay(url.host or "127.0.0.1", url.port or 5432)
    try:
        backend = locks.PgLockBackend(url.set(host="127.0.0.1", port=relay.port), "test")
        with maintenance_lock(_gated_engine(), "test", data_dir=tmp_path, backend=backend) as g:
            relay.freeze()
            with pytest.raises(LockLost):
                g.ensure()
            relay.thaw()                                    # the stuck check now completes - too late
            time.sleep(1.0)
            with pytest.raises(LockLost):
                g.ensure()
            with pytest.raises(LockLost):
                ensure_write_allowed(tmp_path / "x")
        t0 = time.monotonic()
        while _lock_rows(pg_engine) and time.monotonic() - t0 < 30:
            time.sleep(0.2)
        assert _lock_rows(pg_engine) == []                  # the worker unlocked/closed on its own thread
    finally:
        relay.close()


@needs_pg
def test_ll5_discover_writes_nothing_after_losing_the_lock_and_next_run_marks_it_interrupted(pg_engine, db, tmp_path,
                                                                                             monkeypatch):
    from sqlalchemy import text

    import app.pipeline.discover as disc
    from app.db.models import DiscoveryRun

    def fake_locked(settings, engine, cfg, reg, guard, client, *a):
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            s.add(DiscoveryRun(area_keys=["duval"], status="running", params={}, config_versions={}))
            s.commit()
        _kill_lock_holder(pg_engine, "discover")
        raise LockLost("simulated")
    monkeypatch.setattr(disc, "_run_locked", fake_locked)
    from app.config import Settings
    settings = Settings(data_dir=tmp_path / "data", database_url=TEST_DB_URL)
    with pytest.raises(LockLost):
        disc.run_discovery(settings, engine=_gated_engine(), client=object())
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT status FROM discovery_run")).scalars().all() == ["running"]   # not 'failed'
    monkeypatch.setattr(disc, "_run_locked", lambda *a: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        disc.run_discovery(settings, engine=_gated_engine(), client=object())
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT status FROM discovery_run")).scalars().all() == ["interrupted"]
