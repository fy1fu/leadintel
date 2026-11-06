"""Adversarial review of the CRM write path (M2 security review).

The CRM may write while Load Data holds the maintenance lock only through a separate PostgreSQL login that the
DATABASE restricts to the CRM tables. These tests attack that login directly (no application text check), attack
it through the application engine, tamper with the database around it (grants, memberships, triggers, rules,
SECURITY DEFINER functions) and check that every doubt falls back to the maintenance lock.

Runs only with LEADINTEL_TEST_DATABASE_URL (a *_test database). Creates the cluster login ``leadintel_crm_test``
in that test cluster; synthetic rows only.
"""
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.crm import access
from app.crm import service as crm
from app.db.locks import LOCK_KEY, current_guard, exempt_engine
from app.db.models import Location, Score
from app.normalize.core import name_norm

pytestmark = pytest.mark.usefixtures("db")
ROLE = "leadintel_crm_test"


def _url_for(db, user):
    return make_url(db.url.render_as_string(hide_password=False)).set(username=user, password=None).render_as_string(
        hide_password=False)


@pytest.fixture
def setup(db):
    with db.begin() as c:
        c.execute(text(f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN "
                       f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT; END IF; END$$"))
        for r in c.execute(text("SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid "
                                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = :r"), {"r": ROLE}).scalars():
            c.execute(text(f'REVOKE "{r}" FROM {ROLE}'))
        assert access.grant_crm_privileges(c, ROLE)
    with Session(db) as s:
        locs = [Location(display_name=f"Synthetic Sec {i}", name_norm=name_norm(f"Synthetic Sec {i}"), county_fips="12031")
                for i in range(2)]
        s.add_all(locs)
        s.flush()
        s.add(Score(location_id=locs[0].id, kind="bos", stage="provisional", value=50.0))
        s.commit()
        ids = [x.id for x in locs]
    access.forget_writers()
    url = _url_for(db, ROLE)
    w = crm.crm_writer(db, url)
    assert w.mode == "independent", w.reason
    crm.setup_identity(w, "Sec Seller", "SS")
    yield SimpleNamespace(db=db, url=url, w=w, a=ids[0], b=ids[1])
    access.forget_writers()


def _snapshot(db):
    with db.connect() as c:
        return (c.execute(text("SELECT id, display_name, merged_into_id FROM location ORDER BY id")).all(),
                c.execute(text("SELECT id, value FROM score ORDER BY id")).all(),
                c.execute(text("SELECT last_value FROM location_id_seq")).scalar(),
                c.execute(text("SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace")).scalar())


ATTACKS = {
    "comment between keyword and table": "UPDATE/**/location SET display_name = 'x'",
    "quoted table without space": "UPDATE\"location\" SET display_name = 'x'",
    "line comment": "UPDATE --c\n location SET display_name = 'x'",
    "multiple statements": "SELECT 1; UPDATE location SET display_name = 'x'",
    "nested CTE": "WITH a AS (WITH b AS (UPDATE location SET display_name = 'x' RETURNING 1) SELECT * FROM b) SELECT * FROM a",
    "quoted schema and table": 'DELETE FROM "public"."score"',
    "insert business row": "INSERT INTO location (display_name, name_norm) VALUES ('x', 'x')",
    "merge": "MERGE INTO score s USING (SELECT 1 AS id) v ON s.id = v.id WHEN MATCHED THEN UPDATE SET value = 0",
    "truncate business": "TRUNCATE location CASCADE",
    "truncate CRM": "TRUNCATE crm_activity",
    "copy from program": "COPY crm_activity (body) FROM PROGRAM 'echo x'",
    "copy to server file": "COPY (SELECT 1) TO '/tmp/leadintel_crm_probe'",
    "create table": "CREATE TABLE public.crm_evil (a int)",
    "select into": "SELECT 1 AS a INTO public.crm_evil2",
    "alter CRM table": "ALTER TABLE crm_activity ADD COLUMN evil int",
    "drop CRM table": "DROP TABLE crm_list_item",
    "create index": "CREATE INDEX evil ON crm_activity (body)",
    "trigger on CRM table": "CREATE TRIGGER evil BEFORE INSERT ON crm_activity FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    "rule on CRM table": "CREATE RULE evil AS ON INSERT TO crm_activity DO ALSO UPDATE location SET display_name = 'x'",
    "grant to self": f"GRANT UPDATE ON location TO {ROLE}",
    "set role": "SET ROLE postgres",
    "set session authorization": "SET SESSION AUTHORIZATION postgres",
    "set_config role": "SELECT set_config('role', 'postgres', false)",
    "setval business sequence": "SELECT setval('location_id_seq', 1)",
    "nextval business sequence": "SELECT nextval('location_id_seq')",
    "DO block": "DO $$BEGIN UPDATE location SET display_name = 'x'; END$$",
    "temp SECURITY DEFINER function": ("CREATE FUNCTION pg_temp.evil() RETURNS void SECURITY DEFINER LANGUAGE sql AS "
                                       "$$UPDATE location SET display_name = 'x'$$; SELECT pg_temp.evil()"),
    "terminate other sessions": "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                                "AND usename <> current_user",
    "read server file": "SELECT pg_read_file('postgresql.conf')",
    "create schema": "CREATE SCHEMA evil",
    "vacuum business": "VACUUM location",
}


def _attempt(engine, sql) -> bool:
    """True if the statement ran without error (the transaction is always rolled back)."""
    with engine.connect() as c:
        try:
            c.exec_driver_sql(sql)
            ok = True
        except Exception:  # noqa: BLE001 - any refusal counts
            ok = False
        c.rollback()
    return ok


@pytest.mark.parametrize("name", list(ATTACKS))
def test_database_refuses_attacks_on_the_crm_login_without_any_text_check(setup, name):
    raw = create_engine(setup.url, poolclass=NullPool)        # the login itself, no application guard at all
    before = _snapshot(setup.db)
    try:
        ran = _attempt(raw, ATTACKS[name])
    finally:
        raw.dispose()
    if name == "grant to self":     # PostgreSQL only warns ("no privileges were granted"): check the effect instead
        with setup.db.connect() as c:
            assert not c.execute(text(f"SELECT has_table_privilege('{ROLE}', 'location', 'UPDATE')")).scalar()
    else:
        assert not ran, f"the database allowed: {name}"
    assert _snapshot(setup.db) == before


@pytest.mark.parametrize("name", list(ATTACKS))
def test_crm_engine_refuses_the_same_attacks(setup, name):
    before = _snapshot(setup.db)
    with pytest.raises(Exception):
        with setup.w.begin() as c:
            c.exec_driver_sql(ATTACKS[name])
    assert _snapshot(setup.db) == before
    with setup.w.connect() as c:                               # the pooled connection is still the restricted login
        assert c.execute(text("SELECT session_user, current_user")).first() == (ROLE, ROLE)
    assert crm.claim(setup.w, setup.a)["owned_by_me"]            # and ordinary CRM work still succeeds


def test_documented_residuals_do_not_touch_business_data(setup):
    """Temporary tables and large objects are PUBLIC defaults in PostgreSQL; they are session/user objects only."""
    raw = create_engine(setup.url, poolclass=NullPool)
    before = _snapshot(setup.db)
    try:
        assert _attempt(raw, "CREATE TEMP TABLE scratch AS SELECT 1")
        assert _attempt(raw, "SELECT lo_unlink(lo_create(0))")
    finally:
        raw.dispose()
    assert _snapshot(setup.db) == before


def test_text_check_is_only_a_secondary_layer():
    for sql in ATTACKS.values():
        with pytest.raises(PermissionError):
            access.check_statement(sql)
    for sql in ("INSERT INTO crm_organization (name, kind) VALUES (%(n)s, 'local') ON CONFLICT (kind) WHERE kind = 'local' DO NOTHING",
                "SELECT owner_user_id FROM crm_prospect WHERE location_id = %(l)s FOR UPDATE",
                "UPDATE crm_prospect SET status = %(s)s WHERE id = 1",
                "DELETE FROM crm_list_item WHERE list_id = 1 RETURNING list_id",
                "SELECT u.role FROM crm_user u"):
        access.check_statement(sql)


# ---------------------------------------------------------------- tampering: every doubt means the maintenance lock
def _mode(setup):
    access.forget_writers()
    return crm.crm_writer(setup.db, setup.url)


@pytest.mark.parametrize("tamper,undo", [
    (f"GRANT UPDATE ON location TO {ROLE}", f"REVOKE UPDATE ON location FROM {ROLE}"),
    (f"GRANT TRUNCATE ON crm_activity TO {ROLE}", f"REVOKE TRUNCATE ON crm_activity FROM {ROLE}"),
    (f"GRANT USAGE ON SEQUENCE location_id_seq TO {ROLE}", f"REVOKE USAGE ON SEQUENCE location_id_seq FROM {ROLE}"),
    (f"GRANT CREATE ON SCHEMA public TO {ROLE}", f"REVOKE CREATE ON SCHEMA public FROM {ROLE}"),
    (f"GRANT pg_write_all_data TO {ROLE}", f"REVOKE pg_write_all_data FROM {ROLE}"),
    (f"ALTER ROLE {ROLE} CREATEDB", f"ALTER ROLE {ROLE} NOCREATEDB"),
    ("CREATE FUNCTION public.li_evil() RETURNS void SECURITY DEFINER LANGUAGE sql AS $$SELECT$$",
     "DROP FUNCTION public.li_evil()"),
    ("CREATE FUNCTION public.li_trg() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$; "
     "CREATE TRIGGER li_evil AFTER INSERT ON crm_activity FOR EACH ROW EXECUTE FUNCTION public.li_trg()",
     "DROP TRIGGER li_evil ON crm_activity; DROP FUNCTION public.li_trg()"),
    ("CREATE RULE li_evil AS ON INSERT TO crm_activity DO ALSO UPDATE location SET display_name = display_name",
     "DROP RULE li_evil ON crm_activity"),
    (f"CREATE TABLE public.li_owned (a int); ALTER TABLE public.li_owned OWNER TO {ROLE}", "DROP TABLE public.li_owned"),
])
def test_any_weakened_restriction_falls_back_to_the_maintenance_lock(setup, tamper, undo):
    with setup.db.begin() as c:
        c.exec_driver_sql(tamper)
    try:
        w = _mode(setup)
        assert w.mode == "shared_lock" and "does not restrict" in w.reason, w.reason
    finally:
        with setup.db.begin() as c:
            c.exec_driver_sql(undo)
    assert _mode(setup).mode == "independent"


def test_a_rule_would_write_with_owner_rights_which_is_why_rules_disable_the_exemption(setup):
    with setup.db.begin() as c:
        c.exec_driver_sql("CREATE RULE li_evil AS ON INSERT TO crm_activity DO ALSO "
                          "UPDATE location SET display_name = 'rule' WHERE false")
    try:
        assert _mode(setup).mode == "shared_lock"
    finally:
        with setup.db.begin() as c:
            c.exec_driver_sql("DROP RULE li_evil ON crm_activity")


def test_upgrade_regrants_exactly_the_crm_privileges(setup):
    with setup.db.begin() as c:
        c.exec_driver_sql(f"GRANT UPDATE ON location TO {ROLE}; GRANT SELECT ON score TO {ROLE}")
        assert access.grant_crm_privileges(c, ROLE)
        assert not c.execute(text(f"SELECT has_table_privilege('{ROLE}', 'location', 'UPDATE') "
                                  f"OR has_table_privilege('{ROLE}', 'score', 'SELECT')")).scalar()
    assert _mode(setup).mode == "independent"


@pytest.mark.parametrize("url_user,expect", [(None, "no separate CRM database login"),
                                             ("postgres", "does not restrict")])
def test_no_login_or_an_unrestricted_login_means_the_maintenance_lock(setup, url_user, expect):
    access.forget_writers()
    url = _url_for(setup.db, url_user) if url_user else None
    w = crm.crm_writer(setup.db, url or "")
    assert w.mode == "shared_lock" and (expect in w.reason or "must differ" in w.reason), w.reason


def test_pool_refuses_a_connection_that_is_not_the_restricted_login(setup):
    e = access._restricted_engine(_url_for(setup.db, "postgres"), ROLE)
    try:
        with pytest.raises(Exception, match="not the restricted login"):
            with e.connect():
                pass
    finally:
        e.dispose()


# ---------------------------------------------------------------- upgrade and Load Data
def _holder(db, key, operation, shared=False):
    e = exempt_engine(create_engine(db.url, poolclass=NullPool, isolation_level="AUTOCOMMIT",
                                    connect_args={"application_name": f"leadintel:{operation}:4242"}))
    conn = e.connect()
    fn = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
    assert conn.execute(text(f"SELECT {fn}(:k)"), {"k": key}).scalar()
    return e, conn


def test_independent_writes_continue_during_load_data_but_stop_during_an_upgrade(setup):
    e, conn = _holder(setup.db, LOCK_KEY, "load-data")
    try:
        assert crm.claim(setup.w, setup.a)["owned_by_me"]
    finally:
        conn.close()
        e.dispose()
    e, conn = _holder(setup.db, access.UPGRADE_KEY, "db upgrade")
    try:
        with pytest.raises(crm.Unavailable, match="upgrading"):
            crm.add_activity(setup.w, setup.a, "note", "x")
    finally:
        conn.close()
        e.dispose()


def test_upgrade_waits_for_a_running_crm_transaction_and_gives_up_safely(setup):
    with setup.w.begin():                                       # a CRM transaction in progress (shared key)
        with setup.db.connect() as hold:
            with pytest.raises(RuntimeError, match="could not pause sales writes"):
                with access.exclusive_upgrade(hold, wait_s=0.3):
                    pass
    with setup.db.connect() as hold:
        with access.exclusive_upgrade(hold, wait_s=1):
            with pytest.raises(crm.Unavailable):
                crm.add_activity(setup.w, setup.a, "note", "during upgrade")
    assert crm.add_activity(setup.w, setup.a, "note", "after upgrade")["activities"][0]["body"] == "after upgrade"


def test_lock_mode_pauses_during_load_data_and_leaves_no_lock_behind(setup):
    access.forget_writers()
    w = crm.crm_writer(setup.db, "")
    assert w.mode == "shared_lock"
    assert crm.claim(w, setup.a)["owned_by_me"] and current_guard() is None
    e, conn = _holder(setup.db, LOCK_KEY, "load-data")
    try:
        with pytest.raises(crm.Unavailable, match="paused"):
            crm.add_activity(w, setup.a, "note", "x")
    finally:
        conn.close()
        e.dispose()
    with setup.db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM crm_activity WHERE body = 'x'")).scalar() == 0


def test_lock_mode_waits_briefly_behind_another_crm_write(setup):
    access.forget_writers()
    w = crm.crm_writer(setup.db, "")
    e, conn = _holder(setup.db, LOCK_KEY, "crm write")
    threading.Timer(0.4, lambda: (conn.close(), e.dispose())).start()
    t0 = time.monotonic()
    assert crm.add_activity(w, setup.a, "note", "queued")["activities"][0]["body"] == "queued"
    assert time.monotonic() - t0 >= 0.3


def test_api_reports_the_write_mode(setup, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import business
    from app.api.main import create_app
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", setup.db.url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEADINTEL_CRM_DATABASE_URL", setup.url)
    business._engine.cache_clear()
    try:
        c = TestClient(create_app(SimpleNamespace(diagnostics=None), token="t" * 40), base_url="http://127.0.0.1:8765")
        me = c.get("/api/crm/me").json()
        assert me["write_mode"] == "independent"
        monkeypatch.delenv("LEADINTEL_CRM_DATABASE_URL")
        assert c.get("/api/crm/me").json()["write_mode"] == "shared_lock"
    finally:
        business._engine.cache_clear()


def test_db_upgrade_command_sets_the_crm_privileges(setup, monkeypatch):
    from app.cli import main
    with setup.db.begin() as c:
        c.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    assert _mode(setup).mode == "shared_lock"
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", setup.db.url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEADINTEL_CRM_DATABASE_URL", setup.url)
    assert main(["db", "upgrade"]) == 0
    assert _mode(setup).mode == "independent"
