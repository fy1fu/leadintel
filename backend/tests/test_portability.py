"""Phase A: downloaded source files stay usable after the data folder moves (another folder or computer).

Every reuse is still gated by size + Last-Modified + SHA-256; these tests count real GET requests so
"no download" is measured, not assumed.
"""
import json
import shutil
from datetime import date

import httpx
from sqlalchemy.orm import Session

from app.costs.guard import CostGuard
from app.net.http import InfraClient
from app.sources.common import (Downloaded, _cached_candidates, data_relative, ensure_source, register_file,
                                resolve_local_path, sha256_file)
from app.sources.sunbiz.client import RemoteFile, SunbizClient
from test_download_versions import PATH, REG, T1, T2, FakeSunbiz, env  # noqa: F401  (env is a fixture)


def fetch(st, root, server, modified=None):
    rf = RemoteFile("cor", PATH, date(2026, 10, 26), len(server.content), modified or server.modified)
    with InfraClient(REG, st, CostGuard(REG, st), transport=httpx.MockTransport(server.handler)) as c:
        return SunbizClient(c, root).download(rf)


def sidecar(root):
    return next((root / "sunbiz").rglob("*.meta.json"))


def test_new_sidecar_stores_only_the_file_name(env):
    tmp, st = env
    a = fetch(st, tmp / "data", FakeSunbiz(b"A" * 1441, T1))
    meta = json.loads(sidecar(tmp / "data").read_text())
    assert meta["path"] == a.path.name and "/" not in meta["path"] and "\\" not in meta["path"]


def test_moved_data_folder_is_reused_without_any_download(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    a = fetch(st, tmp / "old", srv)
    shutil.move(str(tmp / "old"), str(tmp / "moved"))                  # like copying to another computer
    b = fetch(st, tmp / "moved", srv)
    assert (b.reused, srv.gets) == (True, 1)                            # only the original download
    assert b.path.parent.parent.parent == tmp / "moved" and b.sha256 == a.sha256


def _legacy(tmp, st, srv, stored_path):
    """A sidecar written before this change: absolute path, pointing wherever the old computer kept it."""
    fetch(st, tmp / "data", srv)
    m = sidecar(tmp / "data")
    meta = json.loads(m.read_text())
    meta["path"] = stored_path(m.parent / meta["path"])
    m.write_text(json.dumps(meta))
    return m


def test_legacy_absolute_posix_path_from_another_machine(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    m = _legacy(tmp, st, srv, lambda p: f"/home/olduser/leadintel/data/sunbiz/cor/{p.name}")
    b = fetch(st, tmp / "data", srv)
    assert (b.reused, srv.gets) == (True, 1)
    assert json.loads(m.read_text())["path"] == b.path.name             # healed to the portable form


def test_legacy_absolute_windows_path_from_another_machine(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    m = _legacy(tmp, st, srv, lambda p: f"C:\\Users\\someone\\OneDrive\\leadintel\\data\\sunbiz\\cor\\{p.name}")
    b = fetch(st, tmp / "data", srv)
    assert (b.reused, srv.gets) == (True, 1)
    assert json.loads(m.read_text())["path"] == b.path.name


def test_legacy_absolute_path_on_the_same_machine_still_works(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    m = _legacy(tmp, st, srv, lambda p: str(p.resolve()))               # exactly what older versions wrote
    b = fetch(st, tmp / "data", srv)
    assert (b.reused, srv.gets) == (True, 1)
    assert json.loads(m.read_text())["path"] == b.path.name


def test_found_file_with_wrong_content_is_never_reused(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    a = fetch(st, tmp / "data", srv)
    a.path.write_bytes(b"B" * 1441)                                     # same name and size, different content
    b = fetch(st, tmp / "data", srv)
    assert (b.reused, srv.gets) == (False, 2) and b.sha256 == a.sha256 == sha256_file(b.path)


def test_remote_change_after_move_is_still_downloaded(env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    fetch(st, tmp / "old", srv)
    shutil.move(str(tmp / "old"), str(tmp / "moved"))
    srv.content, srv.modified = b"C" * 1441, T2                         # same size, newer version upstream
    b = fetch(st, tmp / "moved", srv)
    assert (b.reused, srv.gets) == (False, 2) and b.path.read_bytes() == b"C" * 1441


def test_lookup_never_leaves_the_sidecar_folder(tmp_path):
    m = tmp_path / "sunbiz" / "cor" / "x.txt.meta.json"
    for stored in ("../../../etc/passwd", "..\\..\\secrets.txt", "C:\\Windows\\..\\..\\a.csv"):
        first = _cached_candidates(m, stored)[0]
        assert first.parent == m.parent and ".." not in first.name


def test_every_historical_version_survives_a_move_and_stays_resolvable(db, env):
    tmp, st = env
    srv = FakeSunbiz(b"A" * 1441, T1)
    with Session(db) as s:
        src = ensure_source(s, "sunbiz", {"name": "sunbiz"})
        v1 = fetch(st, tmp / "old", srv)
        register_file(s, src, kind="cor", remote_path=PATH, dl=v1, data_dir=tmp / "old")
        srv.content, srv.modified = b"C" * 1441, T2
        v2 = fetch(st, tmp / "old", srv)
        register_file(s, src, kind="cor", remote_path=PATH, dl=v2, data_dir=tmp / "old")
        rows = s.execute(__import__("sqlalchemy").text("select local_path, sha256 from source_file order by id")).all()
        assert [r.local_path for r in rows] == [f"sunbiz/cor/{v1.path.name}", f"sunbiz/cor/{v2.path.name}"]
        shutil.move(str(tmp / "old"), str(tmp / "moved"))
        for r in rows:                                                  # both versions found, content intact
            p = resolve_local_path(r.local_path, tmp / "moved")
            assert p is not None and sha256_file(p) == r.sha256
        s.commit()


def test_register_file_keeps_absolute_path_for_files_outside_the_data_folder(db, tmp_path):
    outside = tmp_path / "manual" / "btr.csv"
    outside.parent.mkdir()
    outside.write_text("a,b\n")
    with Session(db) as s:
        src = ensure_source(s, "btr", {"name": "btr"})
        sf = register_file(s, src, kind="btr_csv", remote_path="manual:btr.csv",
                           dl=Downloaded(outside, sha256_file(outside), 4, True), data_dir=tmp_path / "data")
        assert sf.local_path == str(outside)
        s.commit()


def test_resolve_local_path_handles_relative_and_legacy_absolute_entries(tmp_path):
    data = tmp_path / "data"
    f = data / "dbpr" / "X.abcdef123456.csv"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert data_relative(f, data) == "dbpr/X.abcdef123456.csv"
    assert resolve_local_path("dbpr/X.abcdef123456.csv", data) == f                                   # new form
    assert resolve_local_path("/home/old/leadintel/data/dbpr/X.abcdef123456.csv", data) == f          # old Linux
    assert resolve_local_path("C:\\Users\\old\\leadintel\\data\\dbpr\\X.abcdef123456.csv", data) == f  # old Windows
    assert resolve_local_path("dbpr/missing.csv", data) is None
    assert resolve_local_path(None, data) is None
    outside = tmp_path / "manual.csv"
    outside.write_text("y")
    assert resolve_local_path(str(outside), data) == outside            # manual import outside the data folder
