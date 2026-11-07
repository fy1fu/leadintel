"""Phase B0: every writing entry point is covered by the maintenance lock, and nothing can bypass the gates.

W1 - every CLI command is classified as reading or writing; writing commands take the lock, reading ones never do.
W3 - a source scan: engines, raw connections, autocommit, commits and managed-file writes appear only in
     reviewed places; the test-only bypasses are never referenced by application code (F5).
W4 - network diagnostics work with the gates active and no lock (decision D9).
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest

import app.cli as cli
from app.db import locks

ROOT = Path(__file__).parents[1]
REPO_ROOT = ROOT.parent


def _commands():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")
    out = set()
    for name, p in sub.choices.items():
        action = next((a for a in p._actions if a.dest == "action"), None)
        out |= {(name, c) for c in action.choices} if action else {(name, None)}
    return out


def test_w1_every_command_is_classified_exactly_once():
    assert cli.WRITING_COMMANDS.isdisjoint(cli.READING_COMMANDS)
    assert _commands() == cli.WRITING_COMMANDS | cli.READING_COMMANDS


def _argv(cmd, tmp_path):
    name, action = cmd
    if name == "import-btr":
        csv_file, mapping = tmp_path / "b.csv", tmp_path / "m.yaml"
        csv_file.write_text("Name\nx\n")
        mapping.write_text("business_name: Name\n")
        return [name, str(csv_file), "--county", "duval", "--mapping", str(mapping)]
    if name == "export":
        return [name, str(tmp_path / "out.csv")]
    if name == "net-check":
        return [name, "--no-run", "--no-store"]
    if name == "crawl":
        return [name, "1"]
    if name == "wqs-calibration":
        if action == "report":
            labels = tmp_path / "labels.csv"
            labels.write_text("audit_id,wqs,human_overall_1_5\n")
            return [name, action, "--labels", str(labels)]
        return [name, action, "--out", str(tmp_path / "sheet.csv")]
    return [name] + ([action] if action else [])


@pytest.fixture
def lock_probe(monkeypatch, tmp_path):
    """Replace the lock with a recorder that refuses (exit 5), and make the database unreachable on purpose."""
    from app.config import REPO_ROOT as CFG_ROOT
    from app.config import Settings
    taken = []

    @contextmanager
    def refusing_lock(engine, operation, **kw):
        taken.append(operation)
        raise locks.OperationInProgress(f"probe refused {operation}")
        yield  # pragma: no cover

    monkeypatch.setattr(locks, "maintenance_lock", refusing_lock)
    import app.pipeline.discover as disc
    monkeypatch.setattr(disc, "maintenance_lock", refusing_lock)
    monkeypatch.setattr(disc, "check_database", lambda engine: None)
    settings = Settings(config_dir=CFG_ROOT / "config", data_dir=tmp_path / "data",
                        database_url="postgresql+psycopg://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return taken


@pytest.mark.parametrize("cmd", sorted(cli.WRITING_COMMANDS, key=str))
def test_w1_writing_commands_take_the_maintenance_lock_first(cmd, lock_probe, tmp_path):
    assert cli.main(_argv(cmd, tmp_path)) == locks.EXIT_LOCK_HELD
    assert len(lock_probe) == 1


@pytest.mark.parametrize("cmd", sorted(cli.READING_COMMANDS, key=str))
def test_w1_reading_commands_never_take_the_lock(cmd, lock_probe, tmp_path):
    try:
        cli.main(_argv(cmd, tmp_path))
    except Exception:  # noqa: BLE001 - the database is unreachable on purpose; only the lock matters here
        pass
    assert lock_probe == []


# ------------------------------------------------------------ W3 / F5: source scan
def _sources():
    for base in ("app", "migrations"):
        for p in sorted((ROOT / base).rglob("*.py")):
            yield p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")


ENGINE_FACTORIES = {"app/db/session.py", "app/db/locks.py", "app/net/store.py", "migrations/env.py",
                    "app/crm/access.py"}   # M2: restricted CRM login, verified by the database (test_crm_security.py)
# Engines exempt from the commit gate, each with a reason; a new one needs review.
EXEMPT_ENGINES = {"app/db/locks.py": "the lock connection itself", "app/net/store.py": "diagnostics store (D9)",
                  "app/crm/access.py": "CRM login that the database restricts to the CRM tables (self_check)"}
COMMIT_SITES = {"app/cli.py", "app/pipeline/discover.py", "app/db/session.py",
                "app/sources/btr/storage.py",   # import-btr (Phase B0), runs under the lock and the gate
                "app/audit/service.py"}         # M3 website audit: under the maintenance lock and the gate
# Modules that write inside the managed data folder; each must call ensure_write_allowed.
MANAGED_FILE_WRITERS = {"app/sources/common.py", "app/sources/overture/snapshot.py", "app/sources/osm/extracts.py",
                        "app/sources/btr/storage.py",
                        "app/geo/places.py",   # M3: reduced Census Gazetteer JSON, written during Load Data
                        "app/crawl/runner.py",   # M3: crawl screenshots, under the maintenance lock
                        "app/audit/service.py"}  # M3: screenshot retention (deletes expired ones), under the lock
# Writes that are NOT in the managed data folder, each for a documented reason.
OTHER_FILE_WRITERS = {
    "app/cli.py": "net-check --markdown writes a report to a path the user chose",
    "app/reports/prospects.py": "export writes a CSV to a path the user chose",
    "app/net/service.py": "creates the diagnostics store's own folder (decision D9)",
    "app/scoring/calibration.py": "wqs-calibration export writes a labelling sheet to a path the user chose",
}
FILE_WRITE = re.compile(r"os\.replace\(|os\.rename\(|os\.link\(|\.write_text\(|\.write_bytes\(|"
                        r"open\([^)]*['\"][wax]b?['\"]|\.unlink\(|shutil\.rmtree|\.mkdir\(|pq\.write_table")


def test_w3_engines_are_created_only_in_reviewed_modules():
    found = {name for name, src in _sources() if re.search(r"\bcreate_engine\(", src)}
    assert found <= ENGINE_FACTORIES, found - ENGINE_FACTORIES


def test_w3_gate_exempt_engines_are_reviewed():
    found = {name for name, src in _sources() if re.search(r"\bexempt_engine\(", src)}
    assert found <= set(EXEMPT_ENGINES), found - set(EXEMPT_ENGINES)


def test_w3_no_raw_connections_or_autocommit_outside_the_lock_module():
    for name, src in _sources():
        assert "raw_connection" not in src, name
        if name != "app/db/locks.py":
            assert "AUTOCOMMIT" not in src and "isolation_level" not in src, name


def test_w3_business_commits_only_in_reviewed_modules():
    found = {name for name, src in _sources() if re.search(r"\.commit\(\)", src)}
    assert found <= COMMIT_SITES, found - COMMIT_SITES


def test_w3_file_writes_only_in_reviewed_modules_and_managed_writers_use_the_gate():
    found = {name for name, src in _sources() if FILE_WRITE.search(src)}
    unexpected = found - MANAGED_FILE_WRITERS - set(OTHER_FILE_WRITERS)
    assert not unexpected, f"new file-writing module(s) need review: {sorted(unexpected)}"
    for name, src in _sources():
        if name in MANAGED_FILE_WRITERS:
            assert "ensure_write_allowed" in src, name


def test_f5_test_bypasses_are_never_referenced_by_application_code():
    for name, src in _sources():
        if name != "app/db/locks.py":
            assert "allow_unlocked_" not in src, name


# ------------------------------------------------------------ W4: diagnostics stay independent
def test_w4_net_check_works_with_the_gates_and_no_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LEADINTEL_DATABASE_URL", raising=False)
    assert locks.current_guard() is None
    rc = cli.main(["net-check", "--no-run", "--json"])          # persistent store in the data folder
    assert rc in (0, 2)                                         # 2 = critical capability without results yet
    assert (tmp_path / "data" / "netdiag.db").exists()
