"""Shared maintenance lock and fail-closed write enforcement (Phase B0).

One PostgreSQL advisory lock (`LOCK_KEY`, the key discovery has always used) serialises every operation
that writes to the business database or to LeadIntel's managed data folder: discover, import-btr,
score, config seed, db upgrade, and (later) backup. It is tried, never waited for: a held lock refuses
the new operation (`OperationInProgress`, CLI exit code 5).

Fail-closed enforcement
* Database: listeners on *every* SQLAlchemy `Session` (before_commit) and `Engine` (commit) refuse the
  commit unless the engine is exempt (the diagnostics store, decision D9; the lock connection itself),
  has the explicit test-only bypass, or an active, not-lost guard holds the lock for the SAME database
  (`DbIdentity`) and confirms ownership right before the commit. Raising there means no COMMIT is sent;
  the caller's `with Session(...)` / pool reset rolls the transaction back.
* Managed files: `ensure_write_allowed(path)` must be called before any file is created, renamed,
  replaced or deleted inside the data folder; it applies the same rules (the path must also lie inside
  the guard's data folder).

Losing the lock (`LockLost`, CLI exit code 6)
* Only the guard's worker thread ever uses the lock connection (requests go through a queue). Callers
  wait at most `CHECK_TIMEOUT_S` for an ownership check; a timeout, an error or a false answer marks the
  guard lost PERMANENTLY. A helper thread then only *requests* cancellation with psycopg's
  `cancel_safe()`; the worker itself unlocks and closes the connection when it is free again.
* Once the guard is marked lost, no new commit or file placement can begin. An operation that had
  already passed its final check before the lock was physically lost may still complete (at most one
  commit and one file operation per operation); the application-level check cannot rule that out, and a
  database-side fencing token (milestone B0.5) is required to close that gap.
* Times are NOT promised for the database side: when PostgreSQL notices a vanished client and releases
  the advisory lock depends on the operating system's TCP keepalives (and on the server being alive).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import itertools
import os
import queue
import tempfile
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.timeouts import pg_connect_args

LOCK_KEY = 0x4C454144             # unchanged since Phase 1: old and new LeadIntel versions exclude each other
CHECK_TIMEOUT_S = 3.0             # longest the application waits for one ownership check
HEARTBEAT_S = 2.0                 # background ownership check while an operation runs
CANCEL_TIMEOUT_S = 2.0            # psycopg cancel_safe() timeout
ACQUIRE_TIMEOUT_S = 15.0          # connect (5 s default) + lock attempt + identity query
STATEMENT_TIMEOUT_MS = 2000       # server-side bound for each statement on the lock connection
WRITE_CHECK_BYTES = 8 << 20       # long writes (downloads, copies) re-check the lock every 8 MB
EXIT_LOCK_HELD = 5
EXIT_LOCK_LOST = 6

_LOCK_SQL = "SELECT pg_try_advisory_lock(:k)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(:k)"
# A bigint advisory key is shown with its high half in classid, low half in objid, and objsubid = 1.
_OWN_SQL = ("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid = :hi AND objid = :lo AND objsubid = 1)")
_HOLDER_SQL = ("SELECT a.application_name, a.pid FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
               "WHERE l.locktype = 'advisory' AND l.granted AND l.classid = :hi AND l.objid = :lo AND l.objsubid = 1 "
               "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) LIMIT 1")
_IDENTITY_SQL = ("SELECT current_database(), (SELECT oid FROM pg_database WHERE datname = current_database()), "
                 "host(inet_server_addr()), inet_server_port(), pg_postmaster_start_time()::text")
_KEY_PARAMS = {"hi": (LOCK_KEY >> 32) & 0xFFFFFFFF, "lo": LOCK_KEY & 0xFFFFFFFF}


class OperationInProgress(RuntimeError):
    """Another LeadIntel operation holds the maintenance lock (CLI exit code 5)."""


class LockLost(RuntimeError):
    """The maintenance lock can no longer be confirmed; the operation must stop writing (exit code 6)."""


class UnlockedWrite(RuntimeError):
    """A write was attempted without an authorised maintenance guard."""


@dataclass(frozen=True)
class DbIdentity:
    database: str
    oid: int
    server_addr: str | None
    server_port: int | None
    started: str


class LockBackend(Protocol):
    """What the guard needs from a lock connection. Every method except `cancel` runs on the worker."""

    def acquire(self) -> tuple[bool, DbIdentity | None, str | None]: ...
    def check(self) -> bool: ...
    def release(self) -> None: ...
    def close(self) -> None: ...
    def cancel(self) -> None: ...   # the only method called from another thread


class PgLockBackend:
    """Dedicated, unpooled AUTOCOMMIT psycopg connection holding the session-level advisory lock."""

    def __init__(self, url, operation: str):
        url_s = url if isinstance(url, str) else url.render_as_string(hide_password=False)
        args = dict(pg_connect_args(url_s).get("connect_args", {}))
        args.update(application_name=f"leadintel:{operation}:{os.getpid()}"[:63],
                    keepalives=1, keepalives_idle=5, keepalives_interval=1, keepalives_count=3,
                    options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
        self.engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT", connect_args=args)
        exempt_engine(self.engine)
        self.conn: Connection | None = None

    def acquire(self):
        self.conn = self.engine.connect()
        got = bool(self.conn.execute(text(_LOCK_SQL), {"k": LOCK_KEY}).scalar())
        if not got:
            row = self.conn.execute(text(_HOLDER_SQL), _KEY_PARAMS).first()
            holder = f"{row[0] or 'unknown operation'} (database process {row[1]})" if row else None
            return False, None, holder
        for setting in ("tcp_keepalives_idle = 10", "tcp_keepalives_interval = 5"):
            try:  # best effort: lets the server notice a vanished client sooner (OS-dependent)
                self.conn.execute(text(f"SET {setting}"))
            except Exception:  # noqa: BLE001
                pass
        ident = DbIdentity(*self.conn.execute(text(_IDENTITY_SQL)).first())
        return True, ident, None

    def check(self) -> bool:
        return bool(self.conn.execute(text(_OWN_SQL), _KEY_PARAMS).scalar())

    def release(self) -> None:
        if self.conn is not None:
            self.conn.execute(text(_UNLOCK_SQL), {"k": LOCK_KEY})

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
        finally:
            self.conn = None
            self.engine.dispose()

    def cancel(self) -> None:
        conn = self.conn
        if conn is None:
            return
        dbapi = conn.connection.dbapi_connection
        if hasattr(dbapi, "cancel_safe"):
            dbapi.cancel_safe(timeout=CANCEL_TIMEOUT_S)
        else:  # pragma: no cover - psycopg < 3.2
            dbapi.cancel()


class LockGuard:
    """Owns the lock connection through one worker thread; checks ownership on demand and on a heartbeat."""

    def __init__(self, backend: LockBackend, operation: str, data_dir: Path | None = None, *,
                 check_timeout: float = CHECK_TIMEOUT_S, heartbeat_s: float = HEARTBEAT_S,
                 acquire_timeout: float = ACQUIRE_TIMEOUT_S):
        self.operation = operation
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self.identity: DbIdentity | None = None
        self.lost_reason: str | None = None
        self.permitted = {"commit": 0, "file": 0}
        self._backend = backend
        self._check_timeout, self._heartbeat_s, self._acquire_timeout = check_timeout, heartbeat_s, acquire_timeout
        self._q: queue.Queue = queue.Queue()
        self._seq = itertools.count(1)
        self._lost = threading.Event()
        self._state = threading.Lock()
        self._stop = threading.Event()
        self._released = False
        self._worker = threading.Thread(target=self._work, name=f"leadintel-lock-{operation}", daemon=True)
        self._heartbeat = threading.Thread(target=self._beat, name=f"leadintel-heartbeat-{operation}", daemon=True)

    # ---------------------------------------------------------------- worker side
    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            _seq, op, fut = item
            if op == "check" and self._lost.is_set():
                fut.set_result(False)      # never run a further check once lost
                continue
            try:
                if op == "cleanup":
                    try:
                        self._backend.release()
                    except Exception:  # noqa: BLE001 - connection may already be gone
                        pass
                    self._backend.close()
                    fut.set_result(None)
                else:
                    fut.set_result(getattr(self._backend, op)())
            except BaseException as exc:  # noqa: BLE001 - delivered to the waiting caller
                fut.set_exception(exc)

    def _submit(self, op: str) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((next(self._seq), op, fut))
        return fut

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "LockGuard":
        self._worker.start()
        fut = self._submit("acquire")
        try:
            got, ident, holder = fut.result(timeout=self._acquire_timeout)
        except concurrent.futures.TimeoutError:
            self._mark_lost("could not confirm acquiring the maintenance lock in time")
            self._q.put(None)
            raise LockLost(self.lost_reason) from None
        except BaseException:
            self._q.put(("x", "cleanup", concurrent.futures.Future()))
            self._q.put(None)
            raise
        if not got:
            self._q.put(("x", "cleanup", concurrent.futures.Future()))
            self._q.put(None)
            raise OperationInProgress("another LeadIntel operation is in progress"
                                      + (f": {holder}" if holder else ""))
        self.identity = ident
        self._heartbeat.start()
        return self

    def release(self) -> None:
        """End of the operation: stop the heartbeat; the worker unlocks and closes (bounded wait)."""
        if self._released:
            return
        self._released = True
        self._stop.set()
        if not self._lost.is_set():
            fut = self._submit("cleanup")
            try:
                fut.result(timeout=self._check_timeout)
            except Exception:  # noqa: BLE001 - timeout or error: the session ends when the connection does
                self._mark_lost("could not release the maintenance lock cleanly")
        self._q.put(None)

    # ---------------------------------------------------------------- ownership
    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def _mark_lost(self, reason: str) -> None:
        with self._state:
            if self._lost.is_set():
                return
            self.lost_reason = reason
            self._lost.set()
        # Request cancellation of whatever the worker is blocked on (psycopg cancel_safe is documented to be
        # non-blocking and does not take the connection lock); never close the connection from here.
        threading.Thread(target=self._cancel_quietly, name="leadintel-lock-cancel", daemon=True).start()
        self._q.put(("x", "cleanup", concurrent.futures.Future()))  # the worker cleans up once it is free

    def _cancel_quietly(self) -> None:
        try:
            self._backend.cancel()
        except Exception:  # noqa: BLE001 - the guard is already lost; cancellation is best effort
            pass

    def ensure(self) -> None:
        """Raise LockLost unless the lock is confirmed to be held right now (bounded wait)."""
        if self._lost.is_set():
            raise LockLost(self.lost_reason)
        fut = self._submit("check")
        try:
            ok = fut.result(timeout=self._check_timeout)
        except concurrent.futures.TimeoutError:
            self._mark_lost(f"ownership check did not answer within {self._check_timeout:g} s")
            raise LockLost(self.lost_reason) from None
        except Exception as exc:  # noqa: BLE001
            self._mark_lost(f"ownership check failed: {type(exc).__name__}")
            raise LockLost(self.lost_reason) from exc
        if not ok:
            self._mark_lost("the maintenance lock is no longer held")
            raise LockLost(self.lost_reason)
        if self._lost.is_set():   # lost by another thread while this check was answered
            raise LockLost(self.lost_reason)

    def authorize(self, kind: str) -> None:
        self.ensure()
        self.permitted[kind] = self.permitted.get(kind, 0) + 1

    def _beat(self) -> None:
        while not self._stop.wait(self._heartbeat_s):
            try:
                self.ensure()
            except LockLost:
                return


# -------------------------------------------------------------------- the active guard
_current: contextvars.ContextVar[LockGuard | None] = contextvars.ContextVar("leadintel_maintenance_guard",
                                                                              default=None)


def current_guard() -> LockGuard | None:
    return _current.get()


@contextmanager
def maintenance_lock(engine: Engine, operation: str, *, data_dir: Path | None = None,
                     backend: LockBackend | None = None, **guard_kw) -> Iterator[LockGuard]:
    """Hold the maintenance lock for one operation. Raises OperationInProgress when it is held elsewhere."""
    if _current.get() is not None:
        raise RuntimeError("the maintenance lock is already held by this operation; it is taken once, at entry")
    guard = LockGuard(backend or PgLockBackend(engine.url, operation), operation, data_dir, **guard_kw).start()
    token = _current.set(guard)
    try:
        yield guard
    finally:
        _current.reset(token)
        guard.release()


# -------------------------------------------------------------------- exemptions and test bypasses
_exempt: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_test_bypass: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_file_bypass_roots: list[Path] = []


def exempt_engine(engine: Engine) -> Engine:
    """Only for the diagnostics store (decision D9) and the lock connection itself."""
    _exempt.add(engine)
    return engine


def _require_pytest() -> None:
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("this bypass exists for the automated tests only")


def allow_unlocked_writes_for_tests(engine: Engine) -> Engine:
    """Test-only: let a *_test PostgreSQL database commit without a guard."""
    _require_pytest()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("the database bypass applies to PostgreSQL test databases only")
    with engine.connect() as c:
        name = c.execute(text("SELECT current_database()")).scalar()
    if not str(name).endswith("_test"):
        raise RuntimeError(f"refusing to bypass the maintenance lock for database {name!r}")
    _test_bypass.add(engine)
    return engine


@contextmanager
def allow_unlocked_file_writes_for_tests(root: Path) -> Iterator[None]:
    """Test-only: allow managed-file writes below `root`, which must be inside the temporary folder."""
    _require_pytest()
    root = Path(root).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError(f"file-write bypass is limited to temporary folders, not {root}")
    _file_bypass_roots.append(root)
    try:
        yield
    finally:
        _file_bypass_roots.remove(root)


# -------------------------------------------------------------------- the gates
def _identity_of(conn: Connection) -> DbIdentity | None:
    if conn.dialect.name != "postgresql":
        return None
    cached = conn.info.get("leadintel_identity")
    if cached is None:
        cached = DbIdentity(*conn.exec_driver_sql(_IDENTITY_SQL).first())
        conn.info["leadintel_identity"] = cached
    return cached


def _gate(conn: Connection) -> None:
    engine = conn.engine
    if engine in _exempt or engine in _test_bypass:
        return
    guard = _current.get()
    if guard is None:
        raise UnlockedWrite("refusing to commit to the business database without the maintenance lock")
    if guard.lost:
        raise LockLost(guard.lost_reason)
    if _identity_of(conn) != guard.identity:
        raise UnlockedWrite("refusing to commit: this connection is not to the database the maintenance lock protects")
    guard.authorize("commit")


@event.listens_for(Session, "before_commit")
def _session_before_commit(session: Session) -> None:
    # Runs before the final flush, so pending objects are not sent once the guard is lost.
    _gate(session.connection())


@event.listens_for(Engine, "commit")
def _engine_commit(conn: Connection) -> None:
    # Runs immediately before the DBAPI COMMIT: the last check before the transaction becomes durable.
    try:
        _gate(conn)
    except BaseException:
        # SQLAlchemy deactivates a transaction whose commit raised and then skips the database-level rollback
        # when the connection returns to the pool, leaving it "idle in transaction" with its locks held.
        # Roll back at the driver level so the refused transaction is really gone.
        try:
            conn.connection.dbapi_connection.rollback()
        except Exception:  # noqa: BLE001 - the connection may already be broken; the refusal stands
            pass
        raise


def ensure_write_allowed(path: Path) -> None:
    """Call before creating, renaming, replacing or deleting a file in the managed data folder."""
    p = Path(path).resolve()
    if any(p.is_relative_to(r) for r in _file_bypass_roots):
        return
    guard = _current.get()
    if guard is None:
        raise UnlockedWrite(f"refusing to write {path}: the maintenance lock is not held")
    if guard.lost:
        raise LockLost(guard.lost_reason)
    if guard.data_dir is None or not p.is_relative_to(guard.data_dir):
        raise UnlockedWrite(f"refusing to write {path}: outside the data folder the maintenance lock protects")
    guard.authorize("file")
