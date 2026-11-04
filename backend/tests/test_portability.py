"""Phase A: downloaded source files stay usable after the data folder moves (another folder or computer).

Every reuse is still gated by size + Last-Modified + SHA-256; these tests count real GET requests so
"no download" is measured, not assumed.
"""
import json
import os
import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest
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
        rows = s.execute(__import__("sqlalchemy").text(
            "select local_path, sha256, kind from source_file order by id"
        )).all()
        assert [r.local_path for r in rows] == [f"sunbiz/cor/{v1.path.name}", f"sunbiz/cor/{v2.path.name}"]
        shutil.move(str(tmp / "old"), str(tmp / "moved"))
        for r in rows:                                                  # both versions found, content intact
            p = resolve_local_path(r.local_path, tmp / "moved", kind=r.kind)
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
    f = f.resolve()
    assert data_relative(f, data) == "dbpr/X.abcdef123456.csv"
    assert resolve_local_path("dbpr/X.abcdef123456.csv", data, kind="dbpr_licensees") == f
    assert resolve_local_path(
        "/home/old/leadintel/data/dbpr/X.abcdef123456.csv",
        data,
        kind="dbpr_licensees",
    ) == f
    assert resolve_local_path(
        "C:\\Users\\old\\leadintel\\data\\dbpr\\X.abcdef123456.csv",
        data,
        kind="dbpr_licensees",
    ) == f
    assert resolve_local_path("dbpr/missing.csv", data, kind="dbpr_licensees") is None
    assert resolve_local_path(None, data, kind="dbpr_licensees") is None
    outside = tmp_path / "manual.csv"
    outside.write_text("y")
    assert resolve_local_path(str(outside), data, kind="btr_csv") == outside.resolve()   # manual import outside


SOURCE_LAYOUT_CASES = [
    ("cor", ("sunbiz", "cor")),
    ("fic", ("sunbiz", "fic")),
    ("cor_event", ("sunbiz", "cor_event")),
    ("fic_event", ("sunbiz", "fic_event")),
    ("dbpr_licensees", ("dbpr",)),
    ("boundary_county", ("boundaries",)),
]


def _write_source_file(data_dir, layout, filename="source.csv"):
    path = data_dir.joinpath(*layout, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("source data")
    return path


@pytest.mark.parametrize("kind,layout", SOURCE_LAYOUT_CASES)
def test_all_source_kinds_resolve_relative_paths(tmp_path, kind, layout):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, layout)
    stored = "/".join((*layout, expected.name))

    assert resolve_local_path(stored, data, kind=kind) == expected.resolve()


@pytest.mark.parametrize("kind,layout", SOURCE_LAYOUT_CASES)
def test_all_source_kinds_resolve_legacy_linux_paths(tmp_path, kind, layout):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, layout)
    stored = "/old/computer/leadintel/archive/" + "/".join((*layout, expected.name))

    assert resolve_local_path(stored, data, kind=kind) == expected.resolve()


@pytest.mark.parametrize("kind,layout", SOURCE_LAYOUT_CASES)
def test_all_source_kinds_resolve_legacy_windows_paths(tmp_path, kind, layout):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, layout)
    tail = "\\".join((*layout, expected.name))
    stored = "C:\\Users\\old\\leadintel\\archive\\" + tail

    assert resolve_local_path(stored, data, kind=kind) == expected.resolve()


def test_case_a_external_manual_file_in_dbpr_folder_is_not_remapped(tmp_path):
    data = tmp_path / "custom-store"
    mapped = _write_source_file(data, ("dbpr",), "x.csv")
    mapped.write_text("LeadIntel DBPR file")

    external = tmp_path / "manual" / "dbpr" / "x.csv"
    external.parent.mkdir(parents=True)
    external.write_text("manual file")

    assert resolve_local_path(str(external), data, kind="btr_csv") == external.resolve()
    assert resolve_local_path(str(external), data, kind="btr_csv") != mapped.resolve()


def test_case_b_downloads_path_behavior_depends_on_source_kind(tmp_path):
    data = tmp_path / "custom-store"
    mapped = _write_source_file(data, ("dbpr",), "x.csv")
    stored = r"C:\Users\u\Downloads\dbpr\x.csv"

    # A DBPR source row is expected to have the DBPR layout.
    assert resolve_local_path(stored, data, kind="dbpr_licensees") == mapped.resolve()

    # A manual BTR row must never be remapped into the DBPR folder.
    assert resolve_local_path(stored, data, kind="btr_csv") is None


def test_case_c_directory_path_is_not_accepted(tmp_path):
    data = tmp_path / "custom-store"
    (data / "sunbiz").mkdir(parents=True)

    assert resolve_local_path("/old/leadintel/data/sunbiz", data, kind="cor") is None


def test_case_e_only_the_kinds_trailing_layout_counts(tmp_path):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, ("dbpr",), "x.csv")
    stored = "/old/leadintel/data/sunbiz/dbpr/x.csv"            # e.g. an old data folder named "sunbiz"

    # A DBPR row maps by its own trailing layout, dbpr/<file>; what precedes it is the old data folder.
    assert resolve_local_path(stored, data, kind="dbpr_licensees") == expected.resolve()
    # The same path never satisfies another kind's layout.
    for kind in ("cor", "fic", "cor_event", "fic_event", "boundary_county"):
        assert resolve_local_path(stored, data, kind=kind) is None, kind


def test_case_g_missing_moved_copy_does_not_fall_back_to_original(tmp_path):
    data = tmp_path / "moved-data"
    original = tmp_path / "old-data" / "dbpr" / "x.csv"
    original.parent.mkdir(parents=True)
    original.write_text("original file")

    assert resolve_local_path(
        str(original),
        data,
        kind="dbpr_licensees",
    ) is None


@pytest.mark.parametrize("kind", ["btr_csv", "unknown_source_kind"])
def test_case_h_relative_external_path_is_never_guessed(tmp_path, monkeypatch, kind):
    data = tmp_path / "custom-store"
    _write_source_file(data, ("dbpr",), "x.csv")

    working = tmp_path / "working"                        # the same relative path exists in the working directory
    _write_source_file(working, ("dbpr",), "x.csv")
    monkeypatch.chdir(working)

    assert resolve_local_path("dbpr/x.csv", data, kind=kind) is None
    assert resolve_local_path("x.csv", data, kind=kind) is None


def test_resolver_rejects_directory_where_file_is_expected(tmp_path):
    data = tmp_path / "custom-store"
    directory = data / "dbpr" / "x.csv"
    directory.mkdir(parents=True)

    assert resolve_local_path(
        "dbpr/x.csv",
        data,
        kind="dbpr_licensees",
    ) is None


def test_resolver_rejects_missing_file_and_none_input(tmp_path):
    data = tmp_path / "custom-store"

    assert resolve_local_path("dbpr/missing.csv", data, kind="dbpr_licensees") is None
    assert resolve_local_path(None, data, kind="dbpr_licensees") is None


def test_unknown_kind_is_treated_as_external_not_data_relative(tmp_path):
    data = tmp_path / "custom-store"
    _write_source_file(data, ("dbpr",), "x.csv")

    assert resolve_local_path(
        "dbpr/x.csv",
        data,
        kind="unknown_source_kind",
    ) is None
    assert resolve_local_path(
        str(data / "dbpr" / "x.csv"),
        data,
        kind="dbpr",                                      # a kind that is not a managed source_file kind
    ) == (data / "dbpr" / "x.csv").resolve()             # treated as an external absolute path


def test_kind_is_required():
    with pytest.raises(TypeError):
        resolve_local_path("dbpr/x.csv", Path("."))      # noqa


@pytest.mark.parametrize("stored", [
    r"C:\Users\me\dbpr\leadintel\data\dbpr\x.csv",           # a layout name somewhere above the data folder
    r"C:\Users\me\leadintel\dbpr\dbpr\x.csv",                 # the old data folder itself named "dbpr"
    "/srv/sunbiz/dbpr/x.csv",                                   # ... named "sunbiz"
    "/srv/boundaries/dbpr/x.csv",                               # ... named "boundaries"
    r"\\server\share\leadintel\data\dbpr\x.csv",               # Windows network (UNC) path
])
def test_legacy_data_folder_may_have_any_name(tmp_path, stored):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, ("dbpr",), "x.csv")

    assert resolve_local_path(stored, data, kind="dbpr_licensees") == expected.resolve()


def test_sunbiz_data_folder_named_sunbiz(tmp_path):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, ("sunbiz", "cor"), "20261004c.txt")

    assert resolve_local_path("/home/me/sunbiz/sunbiz/cor/20261004c.txt", data, kind="cor") == expected.resolve()


@pytest.mark.skipif(os.name == "nt", reason="a backslash cannot be part of a file name on Windows")
def test_posix_absolute_path_with_a_literal_backslash_is_parsed_as_posix(tmp_path):
    external = tmp_path / "manual\\export.csv"                  # one file whose name contains a backslash
    external.write_text("manual file")
    assert resolve_local_path(str(external), tmp_path / "data", kind="btr_csv") == external.resolve()

    data = tmp_path / "custom-store"
    expected = _write_source_file(data, ("dbpr",), "x.csv")
    stored = str(tmp_path / "old\\data" / "dbpr" / "x.csv")    # a folder name with a backslash in it
    assert resolve_local_path(stored, data, kind="dbpr_licensees") == expected.resolve()


def test_relative_windows_separators_and_exact_case(tmp_path):
    data = tmp_path / "custom-store"
    expected = _write_source_file(data, ("sunbiz", "cor"), "20261004c.txt")

    assert resolve_local_path(r"sunbiz\cor\20261004c.txt", data, kind="cor") == expected.resolve()
    assert resolve_local_path("Sunbiz/COR/20261004c.txt", data, kind="cor") is None
    assert resolve_local_path("sunbiz/fic/20261004c.txt", data, kind="cor") is None   # another kind's folder


def test_link_inside_the_data_folder_pointing_outside_is_rejected(tmp_path):
    data = tmp_path / "custom-store"
    outside = tmp_path / "outside.csv"
    outside.write_text("outside")
    link = data / "dbpr" / "link.csv"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are not available")

    assert resolve_local_path("dbpr/link.csv", data, kind="dbpr_licensees") is None
    assert resolve_local_path("/old/data/dbpr/link.csv", data, kind="dbpr_licensees") is None


@pytest.mark.parametrize("stored", [
    "../dbpr/x.csv",
    "dbpr/../x.csv",
    "sunbiz/cor/../../outside.csv",
    r"..\dbpr\x.csv",
    r"dbpr\..\x.csv",
])
def test_resolver_rejects_relative_traversal(tmp_path, stored):
    data = tmp_path / "custom-store"
    _write_source_file(data, ("dbpr",), "x.csv")

    assert resolve_local_path(stored, data, kind="dbpr_licensees") is None
