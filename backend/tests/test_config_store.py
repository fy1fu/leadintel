import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.appconfig.store import KINDS, ConfigError, ConfigService
from app.config import REPO_ROOT, Settings


def svc():
    return ConfigService(Settings(config_dir=REPO_ROOT / "config"))


def test_packaged_defaults_load_and_validate():
    s = svc()
    assert set(s.defaults) == set(KINDS)
    assert [t["key"] for t in s.defaults["areas"]["targets"]] == ["duval", "st_johns", "clay"]


def test_database_unavailable_falls_back_to_defaults():
    eff = svc().effective(create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none"))
    assert eff.mode.startswith("defaults (database unavailable")
    assert eff["scoring"]["evidence_caps"]["0"] == 35 and all(v is None for v in eff.versions.values())


def test_seed_is_idempotent_and_versions_are_append_only(db):
    s = svc()
    with Session(db) as ses:
        assert set(s.seed(ses)) == set(KINDS)
        assert s.seed(ses) == []
        areas = copy.deepcopy(s.defaults["areas"])
        areas["targets"].append({"key": "nassau", "kind": "county", "geoid": "12089", "name": "Nassau County, FL", "enabled": True})
        v2 = s.propose(ses, "areas", areas, created_by="levi")
        assert v2.status == "active" and v2.version == 2          # geography: versioned, no approval needed
        assert [r.status for r in s.history(ses, "areas")] == ["superseded", "active"]
        ses.commit()


def test_scoring_change_requires_approval_and_is_reversible(db):
    s = svc()
    with Session(db) as ses:
        s.seed(ses)
        new = copy.deepcopy(s.defaults["scoring"])
        new["evidence_caps"]["0"] = 30
        prop = s.propose(ses, "scoring", new, created_by="levi")
        assert prop.status == "proposed"
        assert s.active(ses, "scoring").content["evidence_caps"]["0"] == 35     # not active until approved
        s.approve(ses, prop.id, approver="levi")
        assert s.active(ses, "scoring").content["evidence_caps"]["0"] == 30
        v1 = s.history(ses, "scoring")[0]
        restored = s.restore(ses, "scoring", v1.id, actor="levi")
        assert restored.version == 3 and s.active(ses, "scoring").content["evidence_caps"]["0"] == 35
        assert len(s.history(ses, "scoring")) == 3                              # nothing deleted
        ses.commit()
    assert svc().effective(db)["scoring"]["evidence_caps"]["0"] == 35


def test_invalid_scoring_rejected(db):
    s = svc()
    with Session(db) as ses:
        s.seed(ses)
        bad = copy.deepcopy(s.defaults["scoring"])
        bad["bos_weights"]["website_need"] = 0.9
        with pytest.raises(ConfigError):
            s.propose(ses, "scoring", bad, created_by="x")
