"""`leadintel import-btr` (Phase B0, test T1): the CSV is copied into the managed data folder as
btr/<county>/<safe name>.<sha12>.csv, registered by that path, and imported from that verified copy.
Database-free: the session, the lock and the importer are fakes; the copy itself is real.
"""
import hashlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy.orm

import app.config
import app.db.locks
import app.db.session
import app.sources.btr.storage as storage
from app.cli import main

pytestmark = pytest.mark.usefixtures("unlocked_files")


def test_import_btr_copies_the_csv_into_managed_storage_and_imports_that_copy(monkeypatch, tmp_path):
    working = tmp_path / "working"
    working.mkdir()
    csv_bytes = b"Business Name,Location Address,Zip,Receipt #\nExample LLC,1 Main St,32207,R-1\n"
    csv_path = working / "btr.csv"
    csv_path.write_bytes(csv_bytes)
    sha = hashlib.sha256(csv_bytes).hexdigest()
    mapping_path = working / "mapping.yaml"
    mapping_path.write_text("business_name: Business Name\naddress: Location Address\nzip: Zip\n"
                            "receipt_number: 'Receipt #'\n")
    data = tmp_path / "data"

    monkeypatch.chdir(working)
    monkeypatch.setattr(app.config, "get_settings", lambda: SimpleNamespace(data_dir=data))
    monkeypatch.setattr(app.db.session, "make_engine", lambda settings: object())
    locks_taken = []

    @contextmanager
    def fake_lock(engine, operation, **kw):
        locks_taken.append((operation, kw.get("data_dir")))
        yield None

    monkeypatch.setattr(app.db.locks, "maintenance_lock", fake_lock)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):     # area lookup -> geoid; existing-row lookup -> none
            return SimpleNamespace(scalar=lambda: "12031", scalar_one_or_none=lambda: None)

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(storage, "Session", FakeSession)
    monkeypatch.setattr(storage, "ensure_source", lambda *args, **kwargs: SimpleNamespace(key="btr_duval"))
    captured = {}

    def fake_register_file(session, source, *, kind, remote_path, dl, data_dir=None, **kwargs):
        captured.update(path=dl.path, sha=dl.sha256, kind=kind, remote_path=remote_path, data_dir=data_dir)
        return SimpleNamespace(id=17, local_path="btr/duval/" + dl.path.name)

    monkeypatch.setattr(storage, "register_file", fake_register_file)

    def fake_import(s, src, sf, stream, mapping, fips):
        captured["imported_text"] = stream.read()
        return SimpleNamespace(rows=1)

    monkeypatch.setattr(storage, "import_btr_stream", fake_import)
    monkeypatch.setattr(sqlalchemy.orm, "Session", FakeSession)

    result = main(["import-btr", "btr.csv", "--county", "duval", "--mapping", str(mapping_path)])

    assert result == 0
    managed = data / "btr" / "duval" / f"btr.{sha[:12]}.csv"
    assert locks_taken == [("import-btr", data)]
    assert captured["kind"] == "btr_csv" and captured["remote_path"] == "manual:btr.csv"
    assert captured["path"] == managed and captured["data_dir"] == data and captured["sha"] == sha
    assert managed.read_bytes() == csv_bytes                         # identical bytes in managed storage
    assert csv_path.read_bytes() == csv_bytes                        # the user's file is untouched
    assert captured["imported_text"] == csv_bytes.decode()           # imported from the verified copy
    assert sorted(p.name for p in managed.parent.iterdir()) == [managed.name]   # no staging file left behind
