"""Local CRM (M2) against PostgreSQL: migration 0006, identities, claims and concurrency, pipeline updates with
optimistic versions, activity history, lists, dashboard, the crm_*-only write guard, the db-upgrade refusal,
and the API (token required for every write).

Runs only with LEADINTEL_TEST_DATABASE_URL (a *_test database; see conftest). Synthetic businesses only.
"""
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.api import business
from app.api.main import create_app
from app.crm import service as crm
from app.db.models import CRM_ACTIVITY_KINDS, CRM_STATUSES, Location
from app.normalize.core import name_norm

pytestmark = pytest.mark.usefixtures("db")
TOKEN = "m2-test-token-" + "x" * 30
ORIGIN = "http://127.0.0.1:8765"
H = {"X-LeadIntel-Token": TOKEN, "Origin": ORIGIN}


@pytest.fixture
def biz(db):
    """Three synthetic businesses; the third is merged into the first."""
    with Session(db) as s:
        locs = [Location(display_name=n, name_norm=name_norm(n), county_fips="12031", operating_status="open")
                for n in ("Synthetic Lawn 1", "Synthetic Clean 2", "Synthetic Dup 3")]
        s.add_all(locs)
        s.flush()
        locs[2].merged_into_id = locs[0].id
        s.commit()
        ids = [loc.id for loc in locs]
    from app.crm import access
    access.forget_writers()        # lock mode: the default (test_crm_security.py covers the restricted login)
    return SimpleNamespace(db=db, ce=crm.crm_writer(db, ""), a=ids[0], b=ids[1], merged=ids[2])


@pytest.fixture
def me(biz):
    return crm.setup_identity(biz.ce, "Test Seller", "ts")


def _business_snapshot(db):
    with db.connect() as c:
        return c.execute(text("SELECT id, display_name, merged_into_id, operating_status FROM location ORDER BY id")).all()


def _second_user(db, me) -> crm.Identity:
    with db.begin() as c:
        uid = str(c.execute(text("INSERT INTO crm_user (organization_id, display_name, initials, role) VALUES "
                                 "(CAST(:o AS uuid), 'Other Seller', 'OS', 'member') RETURNING id"),
                            {"o": me.organization_id}).scalar())
    return crm.Identity(me.organization_id, uid, "Other Seller", "OS", "member")


# ---------------------------------------------------------------- schema
def test_migration_0006_creates_the_crm_tables_and_constraints(db):
    insp = inspect(db)
    for t in ("crm_organization", "crm_user", "crm_prospect", "crm_activity", "crm_list", "crm_list_item"):
        assert insp.has_table(t), t
    uniques = {u["name"] for u in insp.get_unique_constraints("crm_prospect")}
    assert "ux_crm_prospect_org_location" in uniques
    with db.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0007"
        checks = dict(c.execute(text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                                     "WHERE conname IN ('ck_crm_prospect_status', 'ck_crm_activity_kind')")).all())
    assert all(f"'{s}'" in checks["ck_crm_prospect_status"] for s in CRM_STATUSES)
    assert all(f"'{k}'" in checks["ck_crm_activity_kind"] for k in CRM_ACTIVITY_KINDS)


def test_single_alembic_head():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0007"]


def test_database_refuses_a_second_local_organization_and_a_duplicate_prospect(biz, me):
    with pytest.raises(Exception, match="ux_crm_organization_one_local"):
        with biz.db.begin() as c:
            c.execute(text("INSERT INTO crm_organization (name, kind) VALUES ('Second', 'local')"))
    with biz.db.begin() as c:
        c.execute(text("INSERT INTO crm_prospect (organization_id, location_id) VALUES (CAST(:o AS uuid), :l)"),
                  {"o": me.organization_id, "l": biz.a})
    with pytest.raises(Exception, match="ux_crm_prospect_org_location"):
        with biz.db.begin() as c:
            c.execute(text("INSERT INTO crm_prospect (organization_id, location_id) VALUES (CAST(:o AS uuid), :l)"),
                      {"o": me.organization_id, "l": biz.a})


# ---------------------------------------------------------------- identity
def test_identity_setup_is_idempotent_and_ids_are_stable_uuids(biz):
    with biz.db.connect() as c:
        assert crm.identity(c) is None
    first = crm.setup_identity(biz.ce, "Ana Seller", "a.s!")
    assert first.initials == "AS" and first.role == "owner" and len(first.user_id) == 36
    again = crm.setup_identity(biz.ce, "Ana B. Seller", "ab")
    assert (again.user_id, again.organization_id) == (first.user_id, first.organization_id)
    assert again.display_name == "Ana B. Seller" and again.initials == "AB"
    with biz.db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM crm_user")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM crm_organization")).scalar() == 1


@pytest.mark.parametrize("name,initials", [("", "AB"), ("x" * 121, "AB"), ("Ana", "!!"), ("Ana", "")])
def test_identity_setup_rejects_bad_input(biz, name, initials):
    with pytest.raises(crm.CrmError):
        crm.setup_identity(biz.ce, name, initials)


def test_writes_need_a_profile_first(biz):
    with pytest.raises(crm.NotConfigured):
        crm.claim(biz.ce, biz.a)
    with biz.db.connect() as c:
        assert crm.state(c, biz.a)["configured"] is False


# ---------------------------------------------------------------- claims
def test_claim_release_and_activity_history(biz, me):
    before = _business_snapshot(biz.db)
    st = crm.claim(biz.ce, biz.a)
    assert st["owned_by_me"] and st["owner"]["initials"] == "TS" and st["version"] == 2
    assert crm.claim(biz.ce, biz.a)["version"] == 2            # claiming your own business again is a no-op
    st = crm.release(biz.ce, biz.a)
    assert st["owner"] is None and not st["owned_by_me"]
    assert [a["kind"] for a in st["activities"]] == ["release", "claim"]
    with pytest.raises(crm.Conflict):
        crm.release(biz.ce, biz.a)
    assert _business_snapshot(biz.db) == before                # business facts untouched


def test_claim_of_unknown_or_merged_business_is_refused(biz, me):
    with pytest.raises(crm.NotFound):
        crm.claim(biz.ce, 999_999)
    with pytest.raises(crm.Conflict, match="merged into business"):
        crm.claim(biz.ce, biz.merged)


def test_business_claimed_by_someone_else_cannot_be_claimed(biz, me, monkeypatch):
    other = _second_user(biz.db, me)
    monkeypatch.setattr(crm, "identity", lambda c: other)
    crm.claim(biz.ce, biz.a)
    monkeypatch.undo()
    with pytest.raises(crm.Conflict, match="already claimed by OS"):
        crm.claim(biz.ce, biz.a)


def test_concurrent_claims_by_different_users_exactly_one_wins(biz, me, monkeypatch):
    users = [me] + [_second_user(biz.db, me) for _ in range(7)]
    local = threading.local()
    real_identity = crm.identity
    monkeypatch.setattr(crm, "identity", lambda c: getattr(local, "who", None) or real_identity(c))
    barrier, results = threading.Barrier(len(users)), []

    def worker(who):
        local.who = who
        barrier.wait()
        try:
            crm.claim(biz.ce, biz.b)
            results.append(("won", who.user_id))
        except crm.Conflict:
            results.append(("lost", who.user_id))

    threads = [threading.Thread(target=worker, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [u for r, u in results if r == "won"]
    assert len(results) == len(users) and len(winners) == 1
    with biz.db.connect() as c:
        rows = c.execute(text("SELECT owner_user_id::text FROM crm_prospect WHERE location_id = :l"), {"l": biz.b}).all()
        claims = c.execute(text("SELECT user_id::text FROM crm_activity WHERE location_id = :l AND kind = 'claim'"),
                           {"l": biz.b}).all()
    assert rows == [(winners[0],)] and claims == [(winners[0],)]


def test_concurrent_claims_by_the_same_user_make_one_row_and_one_entry(biz, me):
    errors = []

    def worker():
        try:
            crm.claim(biz.ce, biz.a)
        except Exception as exc:   # noqa: BLE001 - any error is a failure here
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with biz.db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM crm_prospect")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM crm_activity WHERE kind = 'claim'")).scalar() == 1


def test_release_and_reassign_permissions(biz, me, monkeypatch):
    other = _second_user(biz.db, me)
    crm.claim(biz.ce, biz.a)
    monkeypatch.setattr(crm, "identity", lambda c: other)            # a member cannot release someone else's claim
    with pytest.raises(crm.Forbidden):
        crm.release(biz.ce, biz.a)
    with pytest.raises(crm.Forbidden):
        crm.reassign(biz.ce, biz.a, other.user_id)
    with pytest.raises(crm.Forbidden):
        crm.update(biz.ce, biz.a, 2, status="contacted")
    monkeypatch.undo()
    st = crm.reassign(biz.ce, biz.a, other.user_id)                  # the owner (admin) can
    assert st["owner"]["initials"] == "OS" and not st["owned_by_me"]
    assert st["activities"][0]["kind"] == "reassign"
    with pytest.raises(crm.NotFound):
        crm.reassign(biz.ce, biz.a, "00000000-0000-0000-0000-000000000000")
    assert crm.release(biz.ce, biz.a)["owner"] is None


# ---------------------------------------------------------------- pipeline updates
def test_update_status_next_action_and_follow_up_with_versions(biz, me):
    st = crm.update(biz.ce, biz.a, 0, status="researching")          # no row yet: version 0 is accepted
    assert st["status"] == "researching" and st["version"] == 2
    with pytest.raises(crm.Conflict, match="changed elsewhere"):
        crm.update(biz.ce, biz.a, 1, status="contacted")             # stale version
    fu = datetime(2026, 12, 1, 15, tzinfo=timezone.utc)
    st = crm.update(biz.ce, biz.a, 2, status="contacted", next_action="Call the owner", follow_up_at=fu)
    assert (st["status"], st["next_action"], st["follow_up_at"], st["version"]) == ("contacted", "Call the owner", fu, 3)
    kinds = [a["kind"] for a in st["activities"]]
    assert kinds.count("status") == 2 and "next_action" in kinds and "follow_up" in kinds
    assert crm.update(biz.ce, biz.a, 3, status="contacted")["version"] == 3   # no change, no new version
    st = crm.update(biz.ce, biz.a, 3, next_action="", follow_up_at=None)
    assert st["next_action"] is None and st["follow_up_at"] is None


def test_update_rejects_unknown_status_and_long_next_action(biz, me):
    with pytest.raises(crm.CrmError):
        crm.update(biz.ce, biz.a, 0, status="hot")
    with pytest.raises(crm.CrmError):
        crm.update(biz.ce, biz.a, 0, next_action="x" * 501)


def test_failed_update_rolls_back_completely(biz, me, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("simulated failure while logging")
    monkeypatch.setattr(crm, "_log", boom)
    with pytest.raises(RuntimeError):
        crm.update(biz.ce, biz.a, 0, status="meeting")
    with biz.db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM crm_prospect")).scalar() == 0


# ---------------------------------------------------------------- activities
def test_notes_and_contact_history(biz, me):
    when = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    crm.add_activity(biz.ce, biz.a, "call", "Left a voicemail", when, outcome="no answer")
    st = crm.add_activity(biz.ce, biz.a, "note", "  Owner prefers email  ")
    assert [a["kind"] for a in st["activities"]] == ["note", "call"]
    assert st["activities"][0]["body"] == "Owner prefers email"
    assert st["activities"][1]["data"] == {"outcome": "no answer"} and st["activities"][1]["occurred_at"] == when


@pytest.mark.parametrize("kind,body", [("note", ""), ("note", "   "), ("claim", "x"), ("status", "x"), ("sms", "x"),
                                       ("note", "x" * 10_001)])
def test_bad_activities_are_refused(biz, me, kind, body):
    with pytest.raises(crm.CrmError):
        crm.add_activity(biz.ce, biz.a, kind, body)


def test_hostile_text_is_stored_verbatim(biz, me):
    evil = "<img src=x onerror=alert(1)> '; DROP TABLE location; --"
    st = crm.add_activity(biz.ce, biz.a, "note", evil)
    assert st["activities"][0]["body"] == evil
    assert len(_business_snapshot(biz.db)) == 3


# ---------------------------------------------------------------- lists and campaigns
def test_lists_and_campaigns(biz, me):
    lst = crm.create_list(biz.ce, "Spring push", "campaign", "Lawn care in Duval")
    with pytest.raises(crm.Conflict):
        crm.create_list(biz.ce, "Spring push")
    with pytest.raises(crm.CrmError):
        crm.create_list(biz.ce, "x", "folder")
    st = crm.add_to_list(biz.ce, lst["id"], biz.a)
    assert st["lists"] == [{"id": lst["id"], "name": "Spring push", "kind": "campaign"}]
    crm.add_to_list(biz.ce, lst["id"], biz.a)                     # twice: one item, one history entry
    crm.add_to_list(biz.ce, lst["id"], biz.b)
    with biz.db.connect() as c:
        got = crm.lists(c)
        assert [(x["name"], x["size"], x["archived"]) for x in got] == [("Spring push", 2, False)]
        assert c.execute(text("SELECT count(*) FROM crm_activity WHERE kind = 'list_add'")).scalar() == 2
    st = crm.remove_from_list(biz.ce, lst["id"], biz.a)
    assert st["lists"] == [] and st["activities"][0]["kind"] == "list_remove"
    crm.set_list_archived(biz.ce, lst["id"], True)
    with biz.db.connect() as c:
        assert crm.lists(c)[0]["archived"] is True
    with pytest.raises(crm.NotFound):
        crm.add_to_list(biz.ce, 999_999, biz.a)
    with pytest.raises(crm.Conflict):
        crm.add_to_list(biz.ce, lst["id"], biz.merged)


# ---------------------------------------------------------------- dashboard
def test_dashboard_pipeline_follow_ups_and_recent(biz, me):
    now = datetime.now(timezone.utc)
    crm.claim(biz.ce, biz.a)
    crm.update(biz.ce, biz.a, 2, status="contacted", follow_up_at=now - timedelta(days=2))
    crm.claim(biz.ce, biz.b)
    crm.update(biz.ce, biz.b, 2, status="won", follow_up_at=now - timedelta(days=2))     # closed: not a follow-up
    with biz.db.connect() as c:
        d = crm.dashboard(c)
    assert d["claimed"] == 2 and d["pipeline"]["contacted"] == 1 and d["pipeline"]["won"] == 1
    assert set(d["pipeline"]) == set(CRM_STATUSES)
    assert [f["location_id"] for f in d["follow_ups"]] == [biz.a] and d["overdue"] == 1
    assert d["recent"] and d["recent"][0]["name"] in ("Synthetic Lawn 1", "Synthetic Clean 2")
    status_changes = [r["data"] for r in d["recent"] if r["kind"] == "status"]   # the interface describes changes from data
    assert {"from": "new", "to": "won"} in status_changes


def test_dashboard_before_setup(biz):
    with biz.db.connect() as c:
        assert crm.dashboard(c) == {"configured": False}


# ---------------------------------------------------------------- API
@pytest.fixture
def api(biz, monkeypatch):
    monkeypatch.setenv("LEADINTEL_DATABASE_URL", biz.db.url.render_as_string(hide_password=False))
    business._engine.cache_clear()
    yield TestClient(create_app(SimpleNamespace(diagnostics=None), token=TOKEN), base_url=ORIGIN), biz
    business._engine.cache_clear()


def test_api_every_write_needs_the_token(api):
    c, biz = api
    for method, path, body in [("post", "/api/crm/me", {"display_name": "A", "initials": "A"}),
                               ("post", f"/api/crm/prospects/{biz.a}/claim", None),
                               ("patch", f"/api/crm/prospects/{biz.a}", {"version": 0, "status": "won"}),
                               ("post", f"/api/crm/prospects/{biz.a}/activities", {"kind": "note", "body": "x"}),
                               ("post", "/api/crm/lists", {"name": "L"}),
                               ("delete", f"/api/crm/lists/1/items/{biz.a}", None)]:
        r = c.request(method, path, json=body, headers={"Origin": ORIGIN})
        assert r.status_code == 403, (method, path)
        r = c.request(method, path, json=body, headers={"X-LeadIntel-Token": TOKEN, "Origin": "https://evil.example"})
        assert r.status_code == 403, (method, path)
    with biz.db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM crm_user")).scalar() == 0


def test_api_full_flow(api):
    c, biz = api
    first = c.get("/api/crm/me").json()
    assert {k: first[k] for k in ("available", "configured", "me")} == {"available": True, "configured": False, "me": None}
    assert first["write_mode"] == "shared_lock"            # no separate CRM login configured: maintenance lock
    assert c.post(f"/api/crm/prospects/{biz.a}/claim", headers=H).status_code == 409    # profile first
    me = c.post("/api/crm/me", json={"display_name": "Test Seller", "initials": "TS"}, headers=H).json()["me"]
    assert me["initials"] == "TS"
    assert [u["initials"] for u in c.get("/api/crm/users").json()] == ["TS"]
    st = c.post(f"/api/crm/prospects/{biz.a}/claim", headers=H).json()
    assert st["owned_by_me"] and st["version"] == 2
    r = c.patch(f"/api/crm/prospects/{biz.a}", json={"version": 1, "status": "contacted"}, headers=H)
    assert r.status_code == 409
    r = c.patch(f"/api/crm/prospects/{biz.a}", json={"version": 2, "status": None}, headers=H)
    assert r.status_code == 422
    r = c.patch(f"/api/crm/prospects/{biz.a}", json={"version": 2, "status": "meeting",
                                                       "follow_up_at": "2026-12-01T09:00:00Z"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "meeting" and r.json()["next_action"] is None
    r = c.post(f"/api/crm/prospects/{biz.a}/activities", json={"kind": "call", "body": "Spoke", "outcome": "positive"},
               headers=H)
    assert r.status_code == 200 and r.json()["activities"][0]["kind"] == "call"
    assert c.post(f"/api/crm/prospects/{biz.a}/activities", json={"kind": "claim"}, headers=H).status_code == 400
    lst = c.post("/api/crm/lists", json={"name": "Hot", "kind": "list"}, headers=H).json()
    assert c.post(f"/api/crm/lists/{lst['id']}/items", json={"location_id": biz.a}, headers=H).json()["lists"][0]["name"] == "Hot"
    assert c.get("/api/crm/lists").json()[0]["size"] == 1
    assert c.delete(f"/api/crm/lists/{lst['id']}/items/{biz.a}", headers=H).json()["lists"] == []
    assert c.patch(f"/api/crm/lists/{lst['id']}", json={"archived": True}, headers=H).status_code == 200
    d = c.get("/api/crm/dashboard").json()
    assert d["available"] and d["claimed"] == 1 and d["pipeline"]["meeting"] == 1
    got = c.get(f"/api/crm/prospects/{biz.a}").json()
    assert got["available"] and got["status"] == "meeting"
    assert c.post(f"/api/crm/prospects/{biz.a}/release", headers=H).json()["owner"] is None
    assert c.post(f"/api/crm/prospects/{biz.merged}/claim", headers=H).status_code == 409
    assert c.post("/api/crm/prospects/999999/claim", headers=H).status_code == 404


def test_api_reads_never_write(api):
    c, biz = api
    for path in ("/api/crm/me", "/api/crm/users", f"/api/crm/prospects/{biz.a}", "/api/crm/lists", "/api/crm/dashboard"):
        assert c.get(path).status_code == 200
    with biz.db.connect() as conn:
        n = conn.execute(text("SELECT (SELECT count(*) FROM crm_organization) + (SELECT count(*) FROM crm_user) "
                              "+ (SELECT count(*) FROM crm_prospect)")).scalar()
    assert n == 0


def test_migration_0006_downgrade_removes_only_crm_tables_and_upgrade_restores_them(biz, me):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    crm.claim(biz.ce, biz.a)
    before = _business_snapshot(biz.db)
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    try:
        with biz.db.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0005")
        insp = inspect(biz.db)
        assert not any(insp.has_table(t) for t in ("crm_organization", "crm_prospect", "crm_activity", "crm_list"))
        assert _business_snapshot(biz.db) == before
        with biz.db.connect() as c:
            assert crm.crm_available(c) is False and crm.identity(c) is None
    finally:
        with biz.db.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
    assert inspect(biz.db).has_table("crm_prospect") and _business_snapshot(biz.db) == before


# ---------------------------------------------------------------- sales filters (list, count, summary, map, export)
def test_sales_status_values_match_the_schema():
    from app.api.filters import SALES_STATUSES
    assert SALES_STATUSES == CRM_STATUSES


def test_sales_filters_need_the_crm():
    from app.api.filters import FilterError, ProspectFilter
    for kw in ({"sales": ["won"]}, {"claim": "mine"}, {"follow_up": "today"}, {"list_id": 1}, {"sort": "follow_up"}):
        with pytest.raises(FilterError):
            ProspectFilter(**kw)
    for kw in ({"sales": ["hot"], "crm": True}, {"claim": "everyone", "crm": True}, {"follow_up": "soon", "crm": True}):
        with pytest.raises(FilterError):
            ProspectFilter(**kw)


@pytest.fixture
def pipeline(api, monkeypatch):
    """Eight businesses in a target county: mine in several states, one claimed by someone else, untouched ones."""
    from app.db.models import Area
    c, biz = api
    with Session(biz.db) as s:
        s.add(Area(key="duval", name="Duval", kind="county", spec={"geoid": "12031"}, is_target=True, enabled=True,
                   geom="SRID=4326;MULTIPOLYGON(((-82 30,-82 31,-81 31,-81 30,-82 30)))"))
        extra = [Location(display_name=f"Synthetic Extra {i}", name_norm=name_norm(f"Synthetic Extra {i}"),
                          county_fips="12031", operating_status="open",
                          geom="SRID=4326;POINT(-81.6 30.3)" if i % 2 else None) for i in range(6)]
        s.add_all(extra)
        s.commit()
        x = [e.id for e in extra]
    me = c.post("/api/crm/me", json={"display_name": "Test Seller", "initials": "TS"}, headers=H).json()["me"]
    me = crm.Identity(me["organization_id"], me["user_id"], me["display_name"], me["initials"], me["role"])
    with biz.db.connect() as conn:   # periods follow the database clock and time zone, as the filters do
        day = conn.execute(text("SELECT date_trunc('day', now()) + interval '12 hours'")).scalar()
    crm.claim(biz.ce, x[0])
    crm.update(biz.ce, x[0], 2, status="contacted", follow_up_at=day - timedelta(days=3))     # overdue
    crm.claim(biz.ce, x[1])
    crm.update(biz.ce, x[1], 2, status="meeting", follow_up_at=day + timedelta(hours=11))     # today
    crm.claim(biz.ce, x[2])
    crm.update(biz.ce, x[2], 2, follow_up_at=day + timedelta(days=4))                         # this week, still 'new'
    crm.add_activity(biz.ce, x[3], "note", "researched")                                       # row, unclaimed, new
    crm.update(biz.ce, x[4], 0, status="not_a_fit")                                            # unclaimed, closed out
    other = _second_user(biz.db, me)
    monkeypatch.setattr(crm, "identity", lambda conn: other)
    crm.claim(biz.ce, x[5])
    monkeypatch.undo()
    lst = crm.create_list(biz.ce, "Week 1", "campaign")
    for i in (0, 3, 5):
        crm.add_to_list(biz.ce, lst["id"], x[i])
    a, b = biz.a, biz.b                                                       # untouched: no CRM row at all
    return c, biz, x, a, b, lst["id"]


def _ids(c, **params):
    d = c.get("/api/prospects", params={"page_size": 200, "qualification": "", **params}).json()
    assert d["total"] == len(d["items"]), d
    return {r["id"] for r in d["items"]}


def test_sales_filters_select_the_right_businesses(pipeline):
    c, biz, x, a, b, list_id = pipeline
    everyone = set(x) | {a, b}
    assert _ids(c) == everyone                                               # merged business never listed
    assert _ids(c, claim="mine") == {x[0], x[1], x[2]}
    assert _ids(c, claim="others") == {x[5]}
    assert _ids(c, claim="claimed") == {x[0], x[1], x[2], x[5]}
    assert _ids(c, claim="unclaimed") == {x[3], x[4], a, b}
    assert _ids(c, sales="new") == {x[2], x[3], x[5], a, b}                  # includes untouched businesses
    assert _ids(c, sales="contacted,meeting") == {x[0], x[1]}
    assert _ids(c, sales="new,not_a_fit", claim="unclaimed") == {x[3], x[4], a, b}
    assert _ids(c, follow_up="overdue") == {x[0]}
    assert _ids(c, follow_up="today") == {x[1]}
    assert _ids(c, follow_up="week") == {x[1], x[2]}
    assert _ids(c, follow_up="any") == {x[0], x[1], x[2]}
    assert _ids(c, follow_up="none") == everyone - {x[0], x[1], x[2]}
    assert _ids(c, list_id=list_id) == {x[0], x[3], x[5]}
    assert _ids(c, list_id=list_id, claim="mine") == {x[0]}
    assert _ids(c, list_id=999_999) == set()


def test_sales_columns_summary_map_and_export_agree(pipeline):
    import csv
    import io
    c, biz, x, a, b, list_id = pipeline
    items = c.get("/api/prospects", params={"page_size": 200, "qualification": "", "sort": "follow_up"}).json()["items"]
    assert [r["id"] for r in items][:3] == [x[0], x[1], x[2]]                # soonest follow-up first
    row = {r["id"]: r for r in items}
    assert row[x[0]]["sales_status"] == "contacted" and row[x[0]]["owned_by_me"] and row[x[0]]["owner_initials"] == "TS"
    assert row[x[5]]["owner_initials"] == "OS" and not row[x[5]]["owned_by_me"]
    assert row[a]["sales_status"] == "new" and row[a]["owner_initials"] is None and row[a]["owned_by_me"] is False
    params = {"qualification": "", "claim": "mine"}
    summary = c.get("/api/prospects/summary", params=params).json()
    points = c.get("/api/prospects/points", params=params).json()
    assert summary["total"] == 3 and points["total"] == 3
    assert {p[0] for p in points["points"]} == {x[1]}                       # of mine, only x[1] has coordinates
    assert points["without_coordinates"] == 2
    r = c.get("/api/prospects/export.csv", params=params)
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert r.headers["X-LeadIntel-Total"] == "3" and len(rows) == 3
    assert {"sales_status", "sales_owner", "follow_up", "next_action"} <= set(rows[0])
    assert {rr["sales_owner"] for rr in rows} == {"Test Seller"}


def test_sales_filters_are_validated_by_the_api(pipeline):
    c = pipeline[0]
    for params in ({"sales": "hot"}, {"claim": "all"}, {"follow_up": "later"}, {"list_id": "abc"}, {"sort": "owner"}):
        assert c.get("/api/prospects", params=params).status_code == 422, params
    opts = c.get("/api/filters/options").json()
    assert opts["sales"] == list(CRM_STATUSES) and "mine" in opts["claim"] and "overdue" in opts["follow_up"]
