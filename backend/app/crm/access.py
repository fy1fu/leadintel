"""How CRM writes reach the database without weakening the B0 maintenance lock (M2 security review).

Two modes, chosen per process and re-checked periodically; anything uncertain falls back to the safe one.

``shared_lock`` (the default, fail-closed)
    Every CRM write takes the maintenance lock for the length of its transaction and commits through the normal,
    gated business engine - exactly like any other writer. While Load Data (or anything else) holds the lock,
    CRM changes are refused with a clear "paused" message. Nothing about B0 is relaxed.

``independent`` (only when the database itself enforces the restriction)
    CRM writes use a SEPARATE PostgreSQL login (``LEADINTEL_CRM_DATABASE_URL``, created by Setup) whose
    privileges are granted by ``db upgrade``: SELECT on ``location``, SELECT/INSERT/UPDATE/DELETE on the CRM
    tables and their sequences, nothing else. Because it is its own login (not ``SET ROLE``), no SQL sent on that
    connection can regain the application's privileges. Before the mode is used, ``self_check`` verifies on the
    live connection that the login cannot write, truncate or reference any other table or sequence, cannot
    create objects, owns nothing, belongs to no role, has no role attributes, cannot run SECURITY DEFINER
    functions or foreign servers, and that no trigger or rule on a CRM table could act on its behalf. Any
    failed check means ``shared_lock``. Only this engine is exempt from the application commit gate.

    Upgrades: every independent CRM transaction takes ``UPGRADE_KEY`` in shared mode (try, never wait);
    ``db upgrade`` takes it exclusively before migrating. A CRM write therefore never overlaps a schema upgrade.

    The SQL text check on this engine (``check_statement``) is a secondary, best-effort layer for programming
    errors. It is not relied on: the adversarial tests show it alone could be bypassed.

Residual (documented, low): the login can still create session-local temporary objects and large objects
(PUBLIC defaults in PostgreSQL) and take advisory locks; none of these touches business data.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, make_url

log = logging.getLogger(__name__)

CRM_TABLES = ("crm_organization", "crm_user", "crm_prospect", "crm_activity", "crm_list", "crm_list_item")
CRM_READ_TABLES = ("location",)                        # the only business table the CRM login may read
UPGRADE_KEY = 0x4C494355                               # 'LICU': db upgrade (exclusive) vs CRM writes (shared)
RECHECK_S = 300.0
SHARED_RETRY_S = 5.0                                   # lock mode: wait this long behind another CRM write only
_shared_writes = threading.Lock()
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class CrmUnavailable(RuntimeError):
    """Raised when a CRM write cannot run safely right now."""


# ------------------------------------------------------------------ privileges (run by db upgrade, as the owner)
def crm_role_from_url(url: str | None) -> str | None:
    if not url:
        return None
    name = make_url(url).username
    return name if name and _ROLE_NAME.match(name) else None


def grant_crm_privileges(conn: Connection, role: str) -> bool:
    """Idempotently sets the CRM login's privileges to exactly the CRM set. Returns False if the role is missing.

    Runs inside ``db upgrade`` (maintenance lock held) as the owner of the tables. Everything is revoked first,
    so a privilege added by hand or by an older version cannot survive an upgrade.
    """
    if not _ROLE_NAME.match(role or ""):
        raise ValueError(f"invalid CRM role name {role!r}")
    if not conn.execute(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}).scalar():
        return False
    q = conn.dialect.identifier_preparer.quote(role)
    conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {q}"))
    conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {q}"))
    conn.execute(text(f"REVOKE ALL ON SCHEMA public FROM {q}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {q}"))
    for t in CRM_READ_TABLES:
        conn.execute(text(f"GRANT SELECT ON public.{t} TO {q}"))
    for t in CRM_TABLES:
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{t} TO {q}"))
    seqs = conn.execute(text(
        "SELECT s.relname FROM pg_class s JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
        "JOIN pg_class t ON t.oid = d.refobjid WHERE s.relkind = 'S' AND t.relname = ANY(:t) "
        "AND s.relnamespace = 'public'::regnamespace"), {"t": list(CRM_TABLES)}).scalars().all()
    for s in seqs:
        conn.execute(text(f"GRANT USAGE, SELECT ON SEQUENCE public.{conn.dialect.identifier_preparer.quote(s)} TO {q}"))
    return True


# ------------------------------------------------------------------ self-check (run on the CRM login's connection)
_SYSTEM_SCHEMAS = "('pg_catalog', 'information_schema', 'pg_toast')"
_CHECKS: list[tuple[str, str]] = [
    ("the CRM login is the session user (no SET ROLE)", "SELECT current_user = session_user"),
    ("the CRM login has no role attributes", "SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication "
     "OR rolbypassrls) FROM pg_roles WHERE rolname = current_user"),
    ("the CRM login belongs to no other role", "SELECT NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r "
     "ON r.oid = m.member WHERE r.rolname = current_user)"),
    ("the CRM login does not own the database", "SELECT pg_get_userbyid(datdba) <> current_user FROM pg_database "
     "WHERE datname = current_database()"),
    ("the CRM login cannot create schemas", "SELECT NOT has_database_privilege(current_database(), 'CREATE')"),
    ("the CRM login cannot create objects in any schema",
     f"SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN {_SYSTEM_SCHEMAS} "
     "AND nspname NOT LIKE 'pg_temp%' AND nspname NOT LIKE 'pg_toast_temp%' AND has_schema_privilege(oid, 'CREATE'))"),
    ("the CRM login owns no objects", "SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON "
     f"n.oid = c.relnamespace WHERE n.nspname NOT IN {_SYSTEM_SCHEMAS} AND n.nspname NOT LIKE 'pg_temp%' "
     "AND n.nspname NOT LIKE 'pg_toast_temp%' AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user))"),
    ("the CRM login cannot change any non-CRM table or view",
     f"SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
     f"WHERE n.nspname NOT IN {_SYSTEM_SCHEMAS} AND n.nspname NOT LIKE 'pg_temp%' AND c.relkind IN ('r','p','v','m','f') "
     "AND NOT (n.nspname = 'public' AND c.relname = ANY(:crm)) "
     "AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') "
     "OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE') "
     "OR has_table_privilege(c.oid, 'TRIGGER') OR has_table_privilege(c.oid, 'REFERENCES')))"),
    ("the CRM login cannot truncate or add triggers to CRM tables",
     "SELECT NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
     "AND c.relname = ANY(:crm) AND (has_table_privilege(c.oid, 'TRUNCATE') OR has_table_privilege(c.oid, 'TRIGGER') "
     "OR has_table_privilege(c.oid, 'REFERENCES')))"),
    ("the CRM login cannot advance or set non-CRM sequences",
     f"SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
     f"WHERE n.nspname NOT IN {_SYSTEM_SCHEMAS} AND n.nspname NOT LIKE 'pg_temp%' AND c.relkind = 'S' "
     "AND (has_sequence_privilege(c.oid, 'UPDATE') OR has_sequence_privilege(c.oid, 'USAGE')) "
     "AND NOT EXISTS (SELECT 1 FROM pg_depend d JOIN pg_class t ON t.oid = d.refobjid WHERE d.objid = c.oid "
     "AND d.deptype IN ('a', 'i') AND t.relnamespace = 'public'::regnamespace AND t.relname = ANY(:crm)))"),
    ("no SECURITY DEFINER function is executable",
     f"SELECT NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
     f"WHERE p.prosecdef AND n.nspname NOT IN {_SYSTEM_SCHEMAS} AND has_function_privilege(p.oid, 'EXECUTE'))"),
    ("no foreign server is usable", "SELECT NOT EXISTS (SELECT 1 FROM pg_foreign_server "
     "WHERE has_server_privilege(oid, 'USAGE'))"),
    ("no trigger on a CRM table", "SELECT NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
     "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace AND c.relname = ANY(:crm))"),
    ("no rule on a CRM table", "SELECT NOT EXISTS (SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class "
     "WHERE r.rulename <> '_RETURN' AND c.relnamespace = 'public'::regnamespace AND c.relname = ANY(:crm))"),
    ("the CRM login can use the CRM tables", "SELECT bool_and(has_table_privilege(c.oid, 'SELECT') AND "
     "has_table_privilege(c.oid, 'INSERT') AND has_table_privilege(c.oid, 'UPDATE') AND has_table_privilege(c.oid, 'DELETE')) "
     "AND count(*) = cardinality(CAST(:crm AS text[])) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
     "AND c.relkind = 'r' AND c.relname = ANY(:crm)"),
]


def self_check(conn: Connection) -> list[str]:
    """Problems found on the live CRM connection; an empty list means the database enforces the restriction."""
    problems = []
    for label, sql in _CHECKS:
        try:
            ok = conn.execute(text(sql), {"crm": list(CRM_TABLES)}).scalar()
        except Exception as exc:  # noqa: BLE001 - any failure to verify is a failed check
            ok = False
            label = f"{label} (could not check: {type(exc).__name__})"
            conn.rollback()
        if ok is not True:
            problems.append(label)
    return problems


# ------------------------------------------------------------------ secondary SQL text check (not relied upon)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_LITERALS = re.compile(r"'(?:[^']|'')*'")
_WRITE_TARGET = re.compile(r"\b(?:INSERT\s*INTO|(?<!FOR\s)(?<!DO\s)UPDATE|DELETE\s*FROM|MERGE\s*INTO)(?!\w)\s*(?:ONLY\s*)?"
                           r"((?:\"[^\"]+\"|\w+)(?:\s*\.\s*(?:\"[^\"]+\"|\w+))?)", re.I)
_FORBIDDEN = re.compile(r"\$|;|\b(?:CREATE|ALTER|DROP|TRUNCATE|COPY|GRANT|REVOKE|VACUUM|CLUSTER|REINDEX|COMMENT|"
                        r"SECURITY|LOCK|CALL|PREPARE|EXECUTE|DEALLOCATE|LISTEN|NOTIFY|LOAD|IMPORT|REFRESH|RESET|"
                        r"DISCARD|AUTHORIZATION|set_config|setval|nextval|lo_\w+|dblink\w*|pg_read\w*|pg_write\w*|"
                        r"pg_terminate_backend|pg_cancel_backend|pg_advisory\w*|pg_try_advisory\w*)\b|"
                        r"\bSET\s+(?:SESSION\s+|LOCAL\s+)?(?:ROLE|SESSION)\b|\bDO\s*(?:LANGUAGE\s+\w+\s*)?''|\bSELECT\b[^()]*?\bINTO\b(?!\s*\()", re.I)
_UPGRADE_SQL = f"SELECT pg_try_advisory_xact_lock_shared({UPGRADE_KEY})"


def check_statement(statement: str) -> None:
    if statement == _UPGRADE_SQL:
        return
    s = _LITERALS.sub("''", _COMMENTS.sub(" ", statement))
    if _FORBIDDEN.search(s):
        raise PermissionError("the CRM connection refused a statement outside the CRM operations")
    for target in _WRITE_TARGET.findall(s):
        table = re.sub(r"\s", "", target).replace('"', "").split(".")[-1].lower()
        if table not in CRM_TABLES:
            raise PermissionError(f"the CRM connection may only write the CRM tables (refused: {table})")


def _check_hook(conn, cursor, statement, parameters, context, executemany):
    check_statement(statement)


# ------------------------------------------------------------------ writers
@dataclass
class CrmWriter:
    mode: str                 # "independent" or "shared_lock"
    reason: str               # why this mode (shown on the diagnostics / profile endpoint)
    engine: Engine            # the engine CRM transactions run on
    business_engine: Engine

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        if self.mode == "independent":
            with self.engine.begin() as c:
                if not c.execute(text(_UPGRADE_SQL)).scalar():
                    raise CrmUnavailable("LeadIntel is upgrading its database; try again in a minute")
                yield c
            return
        from app.db.locks import LockLost, OperationInProgress, maintenance_lock
        deadline = time.monotonic() + SHARED_RETRY_S
        with _shared_writes:                      # CRM writes of this process queue up instead of colliding
            while True:
                try:
                    with maintenance_lock(self.business_engine, "crm write"):
                        with self.business_engine.begin() as c:
                            yield c
                    return
                except OperationInProgress as exc:
                    # Another process's short CRM write: wait briefly. Anything else (Load Data, scoring,
                    # an upgrade): refuse at once - the lock is never waited for behind long operations.
                    if ":crm write:" in str(exc) and time.monotonic() < deadline:
                        time.sleep(0.05)
                        continue
                    raise CrmUnavailable("sales changes are paused while another LeadIntel task (such as Load Data) "
                                         "is running; your change was not saved - try again when it has finished") from exc
                except LockLost as exc:
                    raise CrmUnavailable("the change was not saved (the database lock was lost); try again") from exc

    def connect(self):
        return self.engine.connect()


_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, CrmWriter]] = {}


def _restricted_engine(url: str, role: str) -> Engine:
    from app.db.locks import exempt_engine
    from app.db.timeouts import pg_connect_args
    e = create_engine(url, pool_pre_ping=True, future=True, pool_size=2, max_overflow=2, **pg_connect_args(url))

    @event.listens_for(e, "connect")
    def _verify_login(dbapi_conn, record):        # every pooled connection must be the restricted login
        cur = dbapi_conn.cursor()
        cur.execute("SELECT session_user, current_user")
        got = cur.fetchone()
        cur.close()
        if tuple(got) != (role, role):
            raise PermissionError(f"the CRM connection is not the restricted login {role!r}")

    event.listen(e, "before_cursor_execute", _check_hook)
    return exempt_engine(e)


def _same_database(a: Engine, b: Engine) -> bool:
    q = "SELECT current_database(), inet_server_port(), pg_postmaster_start_time()"
    with a.connect() as ca, b.connect() as cb:
        return tuple(ca.execute(text(q)).first()) == tuple(cb.execute(text(q)).first())


def writer_for(business_engine: Engine, crm_url: str | None) -> CrmWriter:
    """The CRM writer for this process; ``independent`` only if every check passes, else ``shared_lock``."""
    key = (business_engine.url.render_as_string(hide_password=False), crm_url or "")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < RECHECK_S:
            return hit[1]
        w = _decide(business_engine, crm_url)
        if w.mode == "shared_lock" and crm_url:
            log.warning("CRM writes use the maintenance lock: %s", w.reason)
        _cache[key] = (now, w)
        return w


def forget_writers() -> None:
    with _lock:
        for _, w in _cache.values():
            if w.engine is not w.business_engine:
                w.engine.dispose()
        _cache.clear()


def _decide(business_engine: Engine, crm_url: str | None) -> CrmWriter:
    def shared(reason: str) -> CrmWriter:
        return CrmWriter("shared_lock", reason, business_engine, business_engine)
    role = crm_role_from_url(crm_url)
    if not crm_url:
        return shared("no separate CRM database login is configured")
    if not role:
        return shared("the CRM database login name is not valid")
    if role == make_url(business_engine.url.render_as_string(hide_password=False)).username:
        return shared("the CRM login must differ from the application login")
    try:
        e = _restricted_engine(crm_url, role)
        if not _same_database(e, business_engine):
            e.dispose()
            return shared("the CRM login points at a different database")
        with e.connect() as c:
            problems = self_check(c)
    except Exception as exc:  # noqa: BLE001 - cannot verify: stay fail-closed
        return shared(f"the CRM login could not be verified ({type(exc).__name__})")
    if problems:
        e.dispose()
        return shared("the database does not restrict the CRM login: " + "; ".join(problems))
    return CrmWriter("independent", "the database restricts the CRM login to the CRM tables", e, business_engine)


@contextmanager
def exclusive_upgrade(conn: Connection, wait_s: float = 30.0) -> Iterator[None]:
    """Held by ``db upgrade``: waits (bounded) for running CRM transactions, then blocks new ones."""
    conn.execute(text(f"SET statement_timeout = {int(wait_s * 1000)}"))
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": UPGRADE_KEY})
    except Exception as exc:
        raise RuntimeError("could not pause sales writes for the upgrade (a CRM transaction did not finish); "
                           "try again") from exc
    conn.execute(text("SET statement_timeout = 0"))
    try:
        yield
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": UPGRADE_KEY})
