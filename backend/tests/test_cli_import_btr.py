from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy.orm

import app.config
import app.db.locks
import app.db.session
import app.sources.btr.csv_import
import app.sources.common
from app.cli import main


def test_import_btr_resolves_relative_csv_path_before_registration(monkeypatch, tmp_path):
    working = tmp_path / "working"
    working.mkdir()

    csv_path = working / "btr.csv"
    csv_path.write_text("license_number,business_name\n1,Example\n")

    mapping_path = working / "mapping.yaml"
    mapping_path.write_text("business_name: business_name\n")

    monkeypatch.chdir(working)
    monkeypatch.setattr(app.config, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path / "data"))
    monkeypatch.setattr(app.db.session, "make_engine", lambda settings: object())
    locks_taken = []

    @contextmanager
    def fake_lock(engine, operation, **kw):
        locks_taken.append(operation)
        yield None

    monkeypatch.setattr(app.db.locks, "maintenance_lock", fake_lock)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return SimpleNamespace(scalar=lambda: "12031")

        def commit(self):
            pass

    monkeypatch.setattr(sqlalchemy.orm, "Session", FakeSession)
    monkeypatch.setattr(
        app.sources.common,
        "ensure_source",
        lambda *args, **kwargs: object(),
    )

    captured = {}

    def fake_register_file(session, source, *, kind, remote_path, dl, **kwargs):
        captured["path"] = dl.path
        captured["kind"] = kind
        captured["remote_path"] = remote_path
        return SimpleNamespace(id=17)

    monkeypatch.setattr(app.sources.common, "register_file", fake_register_file)
    monkeypatch.setattr(
        app.sources.btr.csv_import,
        "import_btr_csv",
        lambda *args, **kwargs: SimpleNamespace(imported=1),
    )

    result = main([
        "import-btr",
        "btr.csv",
        "--county", "duval",
        "--mapping", str(mapping_path),
    ])

    assert result == 0
    assert locks_taken == ["import-btr"]
    assert captured["kind"] == "btr_csv"
    assert captured["path"] == csv_path.resolve()
    assert Path(captured["path"]).is_absolute()
    assert captured["remote_path"] == "manual:btr.csv"
