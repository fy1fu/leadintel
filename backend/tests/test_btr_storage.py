"""Phase B0: managed BTR storage - names, no-overwrite placement, staging, recovery and streaming import.

Everything here runs without PostgreSQL except the D/A cases at the end (leadintel_test only). File writes
use the explicit `unlocked_files` fixture (pytest temporary folders only).
"""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import os
import re
import time
import tracemalloc
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.sources.btr.storage as storage
from app.sources.btr.csv_import import MappingError, import_btr_csv, import_btr_stream, open_csv_text
from app.sources.btr.storage import (BtrImportError, FileTooLarge, ManagedFileConflict, NoReplaceUnsupported,
                                     SourceChanged, Staged, btr_folder, btr_max_bytes, check_path_length,
                                     import_from_verified, place_verified, prevalidate, recreate_missing_managed,
                                     safe_btr_filename, safe_county, stage_input, verify_staged)

pytestmark = pytest.mark.usefixtures("unlocked_files")
SHA = "0123456789abcdef" * 4
HEADER = "Business Name,DBA,Location Address,City,Zip,Receipt #,Issued,Expires,Occupation\n"
MAPPING = {"business_name": "Business Name", "dba_name": "DBA", "address": "Location Address", "city": "City",
           "zip": "Zip", "receipt_number": "Receipt #", "issue_date": "Issued", "expiry_date": "Expires",
           "occupation": "Occupation"}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _staged(folder: Path, blob: bytes) -> Staged:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f".t{len(list(folder.iterdir()))}.part"
    p.write_bytes(blob)
    return Staged(p, _sha(blob), len(blob))


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else "<dir>") for p in sorted(root.rglob("*"))}


# ------------------------------------------------------------------ N1-N4: names and folders
@pytest.mark.parametrize("bad", ["../x", "Duval", "con", "nul", "a/b", "", "a" * 41, "1duval", "duval ", "st-johns"])
def test_n1_invalid_county_keys_are_refused(bad):
    with pytest.raises(BtrImportError):
        safe_county(bad)


@pytest.mark.parametrize("good", ["duval", "st_johns", "clay", "a" * 40])
def test_n1_valid_county_keys(good):
    assert safe_county(good) == good


@pytest.mark.parametrize("name", ["CON.csv", "nul", "con.backup.csv", "a:b.csv", "x/../y.csv", "..\\..\\z.csv",
                                  "name. .csv", "trailing.", "trailing ", "ctl\x01\x1f.csv", "ünïcødé ✓.csv",
                                  "x" * 300 + ".csv", "noext", "LPT9.TXT", "...", "  ", "a<b>c|d?e*f\".csv"])
def test_n2_file_names_are_safe_inside_the_county_folder(name, tmp_path):
    out = safe_btr_filename(name, SHA)
    assert re.fullmatch(r"[A-Za-z0-9._-]+\.0123456789ab\.[a-z0-9]{1,10}", out), out
    stem = out.rsplit(".", 2)[0]
    assert stem.split(".")[0].lower() not in storage.WINDOWS_RESERVED
    assert not stem.endswith((".", " ")) and len(out) <= 124
    folder = tmp_path / "btr" / "duval"
    assert (folder / out).resolve().parent == folder.resolve()


def test_n2_reserved_names_are_prefixed_and_extension_kept():
    assert safe_btr_filename("CON.csv", SHA) == "_CON.0123456789ab.csv"
    assert safe_btr_filename("Duval Export (Nov).CSV", SHA) == "Duval_Export_Nov.0123456789ab.csv"
    assert safe_btr_filename("data", SHA) == "data.0123456789ab.csv"


def test_n3_link_or_junction_escaping_the_data_folder_is_refused(tmp_path):
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    try:
        (data / "btr").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")
    with pytest.raises(BtrImportError, match="redirected outside the data folder"):
        btr_folder(data, "duval")


def test_n4_long_windows_paths_are_refused(tmp_path):
    long_path = tmp_path / ("d" * 120) / ("e" * 120) / "f.csv"
    with pytest.raises(BtrImportError, match="240 characters"):
        check_path_length(long_path, windows=True)
    check_path_length(long_path, windows=False)          # POSIX has no such limit
    check_path_length(tmp_path / "short.csv", windows=True)


# ------------------------------------------------------------------ P1-P5, A2: placement never overwrites
def _race_creating(target: Path, content: bytes, real):
    def wrapper(*args, **kw):
        target.write_bytes(content)        # "another program" creates the destination right now
        return real(*args, **kw)
    return wrapper


@pytest.mark.skipif(os.name == "nt", reason="hard-link strategy is POSIX")
def test_p1_link_race_never_overwrites_the_other_programs_file(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"OURS")
    target = folder / "x.csv"
    monkeypatch.setattr(storage, "_link", _race_creating(target, b"THEIRS", os.link))
    with pytest.raises(ManagedFileConflict, match="not modified"):
        place_verified(st, target)
    assert target.read_bytes() == b"THEIRS" and st.path.exists()      # theirs intact; staging left for the caller


@pytest.mark.skipif(os.name == "nt", reason="exclusive-creation fallback is POSIX")
def test_p1_exclusive_race_never_overwrites_the_other_programs_file(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"OURS")
    target = folder / "x.csv"
    monkeypatch.setattr(storage, "_link", lambda *a: (_ for _ in ()).throw(OSError(errno.EPERM, "no hard links")))
    monkeypatch.setattr(storage, "_open_exclusive", _race_creating(target, b"THEIRS", storage._open_exclusive))
    with pytest.raises(ManagedFileConflict):
        place_verified(st, target)
    assert target.read_bytes() == b"THEIRS"


@pytest.mark.skipif(os.name != "nt", reason="rename strategy is Windows")
def test_p1_windows_rename_race_never_overwrites_the_other_programs_file(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"OURS")
    target = folder / "x.csv"
    monkeypatch.setattr(storage, "_rename", _race_creating(target, b"THEIRS", os.rename))
    with pytest.raises(ManagedFileConflict):
        place_verified(st, target)
    assert target.read_bytes() == b"THEIRS"


def test_p2_identical_file_appearing_during_placement_is_reused_not_rewritten(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"SAME")
    target = folder / "x.csv"
    name = "_rename" if os.name == "nt" else "_link"
    monkeypatch.setattr(storage, name, _race_creating(target, b"SAME", getattr(storage, name)))
    assert place_verified(st, target) == "reused"
    mtime = target.stat().st_mtime_ns
    time.sleep(0.01)
    assert target.read_bytes() == b"SAME" and target.stat().st_mtime_ns == mtime


@pytest.mark.skipif(os.name == "nt", reason="POSIX strategies")
def test_normal_placement_strategies_remove_the_staging_copy(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"DATA")
    assert place_verified(st, folder / "a.csv") == "placed:link"
    assert not st.path.exists() and (folder / "a.csv").read_bytes() == b"DATA"
    st2 = _staged(folder, b"DATA2")
    monkeypatch.setattr(storage, "_link", lambda *a: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    assert place_verified(st2, folder / "b.csv") == "placed:exclusive"
    assert not st2.path.exists() and (folder / "b.csv").read_bytes() == b"DATA2"


class _FailingWriter(io.BytesIO):
    def write(self, b):
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.skipif(os.name == "nt", reason="exclusive-creation fallback is POSIX")
def test_p3_exclusive_copy_failing_midway_removes_only_its_own_file(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    (folder).mkdir(parents=True)
    (folder / "other.csv").write_bytes(b"OTHER")
    st = _staged(folder, b"DATA")
    target = folder / "x.csv"

    def open_excl(p):
        open(p, "xb").close()              # really created exclusively ...
        return _FailingWriter()            # ... then the disk fills up
    monkeypatch.setattr(storage, "_link", lambda *a: (_ for _ in ()).throw(OSError(errno.EPERM, "no links")))
    monkeypatch.setattr(storage, "_open_exclusive", open_excl)
    with pytest.raises(OSError, match="No space"):
        place_verified(st, target)
    assert not target.exists() and (folder / "other.csv").read_bytes() == b"OTHER" and st.path.exists()


def test_p4_incomplete_file_left_by_a_crash_blocks_and_is_never_overwritten(tmp_path):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"COMPLETE CONTENT")
    target = folder / "x.csv"
    target.write_bytes(b"COMPL")           # what a crash during exclusive creation would leave
    with pytest.raises(ManagedFileConflict, match=re.escape(str(target))):
        place_verified(st, target)
    assert target.read_bytes() == b"COMPL"


@pytest.mark.skipif(os.name == "nt", reason="POSIX strategies")
def test_p5_refused_when_no_no_replace_strategy_is_available(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"DATA")
    target = folder / "x.csv"
    monkeypatch.setattr(storage, "_link", lambda *a: (_ for _ in ()).throw(OSError(errno.EPERM, "no links")))
    monkeypatch.setattr(storage, "_open_exclusive", lambda p: (_ for _ in ()).throw(OSError(errno.EINVAL, "no O_EXCL")))
    with pytest.raises(NoReplaceUnsupported):
        place_verified(st, target)
    assert not target.exists() and st.path.exists()


def test_a2_placement_error_leaves_nothing_at_the_target(tmp_path, monkeypatch):
    folder = tmp_path / "btr" / "duval"
    st = _staged(folder, b"DATA")
    target = folder / "x.csv"
    name = "_rename" if os.name == "nt" else "_link"
    monkeypatch.setattr(storage, name, lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")))
    with pytest.raises(OSError, match="I/O error"):
        place_verified(st, target)
    assert not target.exists() and st.path.exists()


# ------------------------------------------------------------------ B1-B3, A3: staging the exact bytes
def test_b1_staged_copy_hash_and_header_are_checked_on_the_copy_not_the_original(tmp_path):
    src = tmp_path / "in.csv"
    blob = (HEADER + "A LLC,,1 X St,Jax,32207,R-1,10/01/2026,09/30/2027,ROOF\n").encode()
    src.write_bytes(blob)
    st = stage_input(src, tmp_path / "data" / "btr" / "duval", 1 << 20)
    assert (st.sha256, st.size) == (_sha(blob), len(blob)) and st.path.read_bytes() == blob
    src.unlink()                                          # the original is gone: validation still works ...
    assert verify_staged(st, MAPPING)[0] == "Business Name"     # ... because it reads the staged copy
    st.path.write_bytes(blob.replace(b"R-1", b"R-2"))     # the staged copy is altered
    with pytest.raises(SourceChanged):
        verify_staged(st, MAPPING)


def test_b1_header_of_the_staged_copy_must_satisfy_the_mapping(tmp_path):
    src = tmp_path / "in.csv"
    src.write_bytes(b"Wrong,Header\n1,2\n")
    st = stage_input(src, tmp_path / "data" / "btr" / "duval", 1 << 20)
    with pytest.raises(MappingError):
        verify_staged(st, MAPPING)


def test_b1_source_changed_during_copy_is_detected_and_staging_removed(tmp_path, monkeypatch):
    src = tmp_path / "in.csv"
    src.write_bytes(b"A" * 5000)
    folder = tmp_path / "data" / "btr" / "duval"
    real = storage._open_source

    def swapped(p):
        p.write_bytes(b"B" * 5000)                         # replaced after the first stat, same size
        os.utime(p, ns=(1, 1))
        return real(p)
    monkeypatch.setattr(storage, "_open_source", swapped)
    with pytest.raises(SourceChanged):
        stage_input(src, folder, 1 << 20)
    assert list(folder.iterdir()) == []


def test_b2_size_limit_is_enforced_while_streaming(tmp_path, monkeypatch):
    src = tmp_path / "in.csv"
    src.write_bytes(b"x" * 1000)                           # under the limit at the stat check ...
    folder = tmp_path / "data" / "btr" / "duval"
    monkeypatch.setattr(storage, "_open_source", lambda p: io.BytesIO(b"x" * 5000))   # ... but grows while copied
    with pytest.raises(FileTooLarge):
        stage_input(src, folder, 2000)
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("value,ok", [("", True), ("512", True), ("1", True), ("4096", True), ("0", False),
                                      ("-1", False), ("abc", False), ("5000", False), ("1.5", False)])
def test_b3_limit_setting_is_validated(value, ok):
    env = {"LEADINTEL_BTR_MAX_MB": value}
    if ok:
        assert btr_max_bytes(env) == (int(value) if value else 512) << 20
    else:
        with pytest.raises(BtrImportError):
            btr_max_bytes(env)


def test_a3_disk_full_while_staging_removes_the_staging_file(tmp_path, monkeypatch):
    src = tmp_path / "in.csv"
    src.write_bytes(b"x" * 3000)
    folder = tmp_path / "data" / "btr" / "duval"
    folder.mkdir(parents=True)
    (folder / "existing.csv").write_bytes(b"KEEP")
    real_open = open

    def fake_open(p, mode="r", *a, **kw):
        if mode == "xb":
            real_open(p, "xb").close()
            return _FailingWriter()
        return real_open(p, mode, *a, **kw)
    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(OSError, match="No space"):
        stage_input(src, folder, 1 << 20)
    monkeypatch.undo()
    assert sorted(p.name for p in folder.iterdir()) == ["existing.csv"]
    assert (folder / "existing.csv").read_bytes() == b"KEEP"


def test_e3_prevalidate_refuses_oversized_files_before_anything_is_written(tmp_path, monkeypatch):
    src = tmp_path / "in.csv"
    src.write_bytes(b"x" * (2 << 20))
    monkeypatch.setenv("LEADINTEL_BTR_MAX_MB", "1")
    before = _tree(tmp_path)
    with pytest.raises(FileTooLarge):
        prevalidate(src, "duval", MAPPING)
    assert _tree(tmp_path) == before


# ------------------------------------------------------------------ M1-M5: missing managed files
def _row(local_path, sha):
    return SimpleNamespace(local_path=local_path, sha256=sha, id=7)


def test_m1_missing_managed_file_is_recreated_from_identical_bytes(tmp_path):
    data = tmp_path / "data"
    blob = b"ORIGINAL BYTES"
    rel = f"btr/duval/x.{_sha(blob)[:12]}.csv"
    row = _row(rel, _sha(blob))
    before = dict(vars(row))
    st = _staged(data / "btr" / "duval", blob)
    target = recreate_missing_managed(row, st, data)
    assert target == data / rel and target.read_bytes() == blob
    assert vars(row) == before                              # the row is not changed


def test_m2_different_bytes_never_recreate(tmp_path):
    data = tmp_path / "data"
    row = _row("btr/duval/x.aaaaaaaaaaaa.csv", "a" * 64)
    st = _staged(data / "btr" / "duval", b"OTHER BYTES")
    with pytest.raises(BtrImportError, match="does not match"):
        recreate_missing_managed(row, st, data)
    assert not (data / row.local_path).exists()


def test_m3_row_without_hash_is_unverifiable(tmp_path):
    data = tmp_path / "data"
    st = _staged(data / "btr" / "duval", b"X")
    with pytest.raises(BtrImportError, match="unverifiable"):
        recreate_missing_managed(_row("btr/duval/x.csv", None), st, data)


@pytest.mark.parametrize("occupant", ["file", "dir", "link"])
def test_m4_occupied_path_is_never_overwritten(tmp_path, occupant):
    data = tmp_path / "data"
    blob = b"BYTES"
    rel = "btr/duval/x.csv"
    target = data / rel
    target.parent.mkdir(parents=True)
    if occupant == "file":
        target.write_bytes(b"SOMETHING ELSE")
    elif occupant == "dir":
        target.mkdir()
    else:
        try:
            target.symlink_to(tmp_path / "nowhere")
        except OSError:
            pytest.skip("symbolic links are not available")
    before = _tree(data)
    st = _staged(tmp_path / "staging", blob)
    with pytest.raises(ManagedFileConflict):
        recreate_missing_managed(_row(rel, _sha(blob)), st, data)
    assert _tree(data) == before


def test_m5_external_rows_are_never_repaired_and_their_paths_untouched(tmp_path):
    data = tmp_path / "data"
    external = tmp_path / "users_files" / "btr.csv"          # missing historical external file
    external.parent.mkdir()
    (external.parent / "neighbour.csv").write_bytes(b"N")
    before = _tree(tmp_path / "users_files")
    st = _staged(tmp_path / "staging", b"BYTES")
    with pytest.raises(BtrImportError, match="never repaired"):
        recreate_missing_managed(_row(str(external), _sha(b"BYTES")), st, data)
    assert _tree(tmp_path / "users_files") == before and not external.exists()


# ------------------------------------------------------------------ E1, E4: streaming equals bytes
class StubSession:
    """Answers the importer's queries without a database; optionally records every call."""

    def __init__(self, record=False):
        self.record, self.calls = record, []

    def execute(self, stmt, params=None):
        if self.record:
            self.calls.append((type(stmt).__name__, str(getattr(stmt, "text", ""))[:60], repr(params)))
        return SimpleNamespace(scalar=lambda: 1, first=lambda: None)

    def add(self, obj):
        if self.record:
            self.calls.append(("add", type(obj).__name__, repr(sorted((k, v) for k, v in vars(obj).items()
                                                                      if not k.startswith("_")))))

    def flush(self):
        pass


SRC = SimpleNamespace(id=1, key="btr_duval")


def _run_bytes(blob):
    s, sf = StubSession(record=True), SimpleNamespace(id=1)
    st = import_btr_csv(s, SRC, sf, blob, MAPPING, "12031", today=date(2026, 11, 3))
    return st.__dict__, s.calls, vars(sf)


def _run_stream(path, sha):
    s, sf = StubSession(record=True), SimpleNamespace(id=1)
    st = import_from_verified(path, sha, lambda t: import_btr_stream(s, SRC, sf, t, MAPPING, "12031",
                                                                     today=date(2026, 11, 3)))
    return st.__dict__, s.calls, vars(sf)


ROW = "A{i} LLC,Dba {i},{i} Main St,Jacksonville,32207,R-{i},10/01/2026,09/30/2027,ROOFING\n"
E1_CASES = {
    "bom_crlf": "﻿".encode() + (HEADER + ROW.format(i=1) + ROW.format(i=2)).replace("\n", "\r\n").encode(),
    "quoted_newlines": (HEADER + 'B LLC,"multi\r\nline\rdba",1 X St,Jax,32207,R-9,10/01/2026,09/30/2027,"x\ny"\n').encode(),
    "invalid_utf8": HEADER.encode() + b"Caf\xe9 \xff\xfe LLC,,1 X St,Jax,32207,R-7,10/01/2026,09/30/2027,X\n",
    "blank_lines_and_rejects": (HEADER + "\n" + ROW.format(i=3) + "\n,,,,,,,,\nNo Receipt,,1 X,Jax,32207,,,,\n").encode(),
    "header_only": HEADER.encode(),
    "lone_cr_rows": (HEADER + ROW.format(i=4) + ROW.format(i=5)).replace("\n", "\r").encode(),
    # a multi-byte character and an invalid byte straddling the 8 KB / 1 MB read boundaries
    "boundaries": HEADER.encode() + b"".join(
        ("Z" * (8190 - len(HEADER)) if i == 0 else "").encode() + "é€ LLC,,1 X,Jax,32207,R-b{},10/01/2026,09/30/2027,X\n"
        .format(i).encode() for i in range(20000)) + b"\xc3",
}


@pytest.mark.parametrize("case", sorted(E1_CASES))
def test_e1_streaming_import_is_identical_to_the_bytes_import(case, tmp_path):
    blob = E1_CASES[case]
    p = tmp_path / "f.csv"
    p.write_bytes(blob)

    def outcome(fn):
        try:
            return ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - the same failure must occur on both paths
            return ("error", type(exc).__name__, str(exc))

    def rows_from_stream():
        with open(p, "rb") as raw:
            return list(csv.DictReader(open_csv_text(io.BufferedReader(storage.HashingReader(raw)))))
    rows_bytes = outcome(lambda: list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace")))))
    assert outcome(rows_from_stream) == rows_bytes
    assert outcome(lambda: _run_stream(p, _sha(blob))) == outcome(lambda: _run_bytes(blob))
    if case == "lone_cr_rows":   # existing behaviour, unchanged by B0: such files are refused on both paths
        assert rows_bytes[0] == "error"


def test_e4_file_changed_after_verification_is_refused(tmp_path):
    p = tmp_path / "f.csv"
    blob = (HEADER + ROW.format(i=1)).encode()
    p.write_bytes(blob)
    sha = _sha(blob)
    p.write_bytes(blob.replace(b"R-1", b"R-X"))           # changed between verification and import
    with pytest.raises(SourceChanged):
        import_from_verified(p, sha, lambda t: t.read(10))


def test_hashing_covers_bytes_the_importer_did_not_read(tmp_path):
    p = tmp_path / "f.csv"
    blob = (HEADER + ROW.format(i=1)).encode() * 1000
    p.write_bytes(blob)
    assert import_from_verified(p, _sha(blob), lambda t: t.readline()) == HEADER
    with pytest.raises(SourceChanged):
        import_from_verified(p, _sha(blob + b"x"), lambda t: t.readline())


# ------------------------------------------------------------------ E2: memory (measured and reported)
# Wide rows (~16 KB) keep the row count, and so the run time, manageable while the file sizes reach 256 MB.
WIDE = "A{i} LLC,,{i} Main St,Jacksonville,32207,R-{i},10/01/2026,09/30/2030," + "O" * 16000 + "\n"
REJECT = "No Receipt {i},,1 X,Jax,32207,,,,\n"


def _make_csv(path: Path, mb: int, rejected: int) -> None:
    target = mb << 20
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER)
        size = len(HEADER)
        for i in range(rejected):
            line = REJECT.format(i=i)
            fh.write(line)
            size += len(line)
        i = 0
        while size < target:
            line = WIDE.format(i=i)
            fh.write(line)
            size += len(line)
            i += 1


def _peak(fn) -> int:
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _report(label, value_bytes):
    print(f"\nE2 {label}: {value_bytes / (1 << 20):.2f} MB")


@pytest.fixture(scope="module")
def e2_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("e2")
    files = {}
    for mb in (16, 64, 256):
        p = d / f"f{mb}.csv"
        _make_csv(p, mb, rejected=1000)
        files[mb] = p
    for n in (0, 10_000, 100_000):
        p = d / f"rej{n}.csv"
        _make_csv(p, 16, rejected=n)
        files[f"rej{n}"] = p
    return d, files


@pytest.mark.timeout(900)
def test_e2a_hashing_and_copying_stay_within_16_mb(e2_files):
    d, files = e2_files
    for mb in (16, 64, 256):
        folder = d / f"stage{mb}"
        peak = _peak(lambda: stage_input(files[mb], folder, 1 << 30))
        _report(f"(a) hash+copy {mb} MB file: peak", peak)
        assert peak <= 16 << 20, f"{mb} MB: peak {peak / (1 << 20):.1f} MB exceeds the 16 MB target"


def _import_peak(path: Path) -> int:
    from app.sources.common import sha256_file
    sha = sha256_file(path)
    return _peak(lambda: import_from_verified(path, sha, lambda t: import_btr_stream(
        StubSession(), SRC, SimpleNamespace(id=1), t, MAPPING, "12031", today=date(2026, 11, 3))))


@pytest.mark.timeout(900)
def test_e2b_import_memory_does_not_grow_with_file_size_for_the_same_rejected_rows(e2_files):
    _, files = e2_files
    peaks = {mb: _import_peak(files[mb]) for mb in (16, 64, 256)}
    for mb, p in peaks.items():
        _report(f"(b) import {mb} MB file, 1,000 rejected rows: peak", p)
    growth = peaks[256] - peaks[16]
    _report("(b) growth 16 MB -> 256 MB", growth)
    assert growth < 0.05 * (240 << 20), "import memory grows with file size"


@pytest.mark.timeout(900)
def test_e2d_memory_from_rejected_rows_is_measured_not_bounded(e2_files):
    _, files = e2_files
    peaks = {n: _import_peak(files[f"rej{n}"]) for n in (0, 10_000, 100_000)}
    for n, p in peaks.items():
        _report(f"(d) import 16 MB file, {n:,} rejected rows: peak", p)
    per_row = (peaks[100_000] - peaks[0]) / 100_000
    print(f"\nE2 (d) about {per_row:.0f} bytes per rejected row (kept for the import report; no limit is enforced)")
    assert peaks[100_000] >= peaks[0]


# ================================================================== PostgreSQL (leadintel_test only)
TEST_DB_URL = os.environ.get("LEADINTEL_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_DB_URL, reason="LEADINTEL_TEST_DATABASE_URL not set")
CSV1 = (HEADER + "Ace Roofing LLC,,1 X St,Jacksonville,32207,R-1,10/01/2026,09/30/2030,ROOFING\n").encode()


def _setup_areas(db):
    from sqlalchemy import text
    with db.begin() as c:
        for key, geoid in (("duval", "12031"), ("clay", "12019")):
            c.execute(text("INSERT INTO area (key, name, kind, spec, is_target, enabled) "
                           "VALUES (:k, :k, 'county', CAST(:s AS jsonb), true, true)"),
                      {"k": key, "s": f'{{"geoid": "{geoid}"}}'})


def _import(tmp_path, county, blob, name="btr.csv"):
    from sqlalchemy import create_engine

    from app.db.locks import maintenance_lock
    src = tmp_path / "in" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(blob)
    e = create_engine(TEST_DB_URL)
    data = tmp_path / "data"
    with maintenance_lock(e, "test", data_dir=data):
        return storage.run_btr_import(e, data, src, county, MAPPING, 1 << 30)


def _rows(db):
    from sqlalchemy import text
    with db.connect() as c:
        return c.execute(text("SELECT s.key, f.local_path, f.sha256 FROM source_file f JOIN source s ON s.id = f.source_id "
                              "ORDER BY f.id")).all()


def _counts(db):
    from sqlalchemy import text
    with db.connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("source_record", "location", "fact")}


@needs_pg
def test_d1_same_file_same_county_reuses_the_row_with_no_new_file_or_records(db, tmp_path):
    _setup_areas(db)
    a = _import(tmp_path, "duval", CSV1)
    counts = _counts(db)
    b = _import(tmp_path, "duval", CSV1)
    assert (a.action, b.action) == ("new", "reused_managed") and len(_rows(db)) == 1
    assert _counts(db) == counts, "re-importing the same file changed records or locations"
    assert len(list((tmp_path / "data" / "btr" / "duval").iterdir())) == 1


@needs_pg
def test_d2_same_file_different_county_is_imported_separately(db, tmp_path):
    _setup_areas(db)
    _import(tmp_path, "duval", CSV1)
    _import(tmp_path, "clay", CSV1)
    rows = _rows(db)
    assert {r.key for r in rows} == {"btr_duval", "btr_clay"}
    assert {r.local_path.split("/")[1] for r in rows} == {"duval", "clay"}


@needs_pg
def test_d3_same_name_new_content_keeps_both_versions(db, tmp_path):
    _setup_areas(db)
    _import(tmp_path, "duval", CSV1)
    _import(tmp_path, "duval", CSV1.replace(b"R-1", b"R-2"))
    assert len(_rows(db)) == 2 and len(list((tmp_path / "data" / "btr" / "duval").iterdir())) == 2


@needs_pg
def test_d4_m5_existing_external_row_is_reused_unchanged(db, tmp_path):
    from sqlalchemy import text
    _setup_areas(db)
    external = tmp_path / "users" / "btr.csv"
    with db.begin() as c:
        sid = c.execute(text("INSERT INTO source (key, name, reliability, cost_class, enabled) "
                             "VALUES ('btr_duval', 'b', '{}', 'free', true) RETURNING id")).scalar()
        c.execute(text("INSERT INTO source_file (source_id, kind, remote_path, size, sha256, local_path, status, "
                       "records, quarantined) VALUES (:s, 'btr_csv', 'manual:btr.csv', :n, :h, :p, 'imported', 1, 0)"),
                  {"s": sid, "n": len(CSV1), "h": _sha(CSV1), "p": str(external)})
    before = _rows(db)
    out = _import(tmp_path, "duval", CSV1)
    assert out.action == "reused_external" and any("missing" in w for w in out.warnings)
    assert [(r.local_path, r.sha256) for r in _rows(db)] == [(r.local_path, r.sha256) for r in before]
    assert not external.exists() and not (tmp_path / "data" / "btr" / "duval").exists() or \
        list((tmp_path / "data" / "btr" / "duval").iterdir()) == []


@needs_pg
def test_m1_cli_level_missing_managed_file_recreated(db, tmp_path):
    _setup_areas(db)
    a = _import(tmp_path, "duval", CSV1)
    managed = tmp_path / "data" / a.stored
    managed.unlink()
    before = _rows(db)
    b = _import(tmp_path, "duval", CSV1)
    assert b.action == "recreated" and managed.read_bytes() == CSV1 and _rows(db) == before


@needs_pg
def test_a1_a7_commit_failure_keeps_the_verified_file_and_rerun_registers_it(db, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    _setup_areas(db)
    real_commit = Session.commit
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("simulated commit failure")))
    with pytest.raises(RuntimeError) as ei:
        _import(tmp_path, "duval", CSV1)
    assert any("nothing was committed" in n for n in getattr(ei.value, "__notes__", []))
    assert _rows(db) == []
    files = list((tmp_path / "data" / "btr" / "duval").iterdir())
    assert len(files) == 1 and files[0].read_bytes() == CSV1        # verified, unregistered, kept
    monkeypatch.setattr(Session, "commit", real_commit)
    out = _import(tmp_path, "duval", CSV1)
    assert out.action == "new" and len(_rows(db)) == 1
    assert list((tmp_path / "data" / "btr" / "duval").iterdir()) == files    # reused, no second copy


# ------------------------------------------------------------------ E2(c): database-backed import memory (measured)
E2C_REJECTED = 1_000
# Rows spread over 100 ZIP codes, like a real county file. Sizes stay small: the existing importer's location-matching
# query (a trigram similarity search per row) takes roughly quadratic time as locations accumulate - measured at
# about 21-42 s for 1,000 rows and 400-1,000 s for 3,000 rows here - which B0 does not change.
E2C_ROW = "E2C Business {n}-{i} LLC,,{i} Test Ave,Jacksonville,{zip},E2C-{n}-{i},10/01/2026,09/30/2030,TESTING\n"


@needs_pg
@pytest.mark.timeout(1800)
@pytest.mark.parametrize("valid_rows", [250, 500, 1_000])
def test_e2c_database_backed_import_memory_is_measured(db, tmp_path, valid_rows):
    """Real import into leadintel_test (staging, verification, placement, import, commit) with the same absolute
    number of rejected rows; peak Python memory is reported per valid-row count. No limit is asserted: the
    importer legitimately keeps rejected rows for its report, and the measurement shows any session growth."""
    from sqlalchemy import create_engine

    from app.db.locks import maintenance_lock
    _setup_areas(db)
    src = tmp_path / "in" / "e2c.csv"
    src.parent.mkdir()
    with open(src, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER)
        for i in range(E2C_REJECTED):
            fh.write(REJECT.format(i=i))
        for i in range(valid_rows):
            fh.write(E2C_ROW.format(n=valid_rows, i=i, zip=32100 + i % 100))
    e = create_engine(TEST_DB_URL)
    data = tmp_path / "data"
    with maintenance_lock(e, "test", data_dir=data):
        tracemalloc.start()
        tracemalloc.reset_peak()
        t0 = time.monotonic()
        try:
            out = storage.run_btr_import(e, data, src, "duval", MAPPING, 1 << 30)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        elapsed = time.monotonic() - t0
    assert out.action == "new"
    assert out.stats["rows"] == valid_rows + E2C_REJECTED and len(out.stats["rejected"]) == E2C_REJECTED
    assert out.stats["created"] + out.stats["linked"] == valid_rows
    print(f"\nE2 (c) database import, {valid_rows:,} valid + {E2C_REJECTED:,} rejected rows "
          f"({src.stat().st_size / (1 << 20):.2f} MB file): peak {peak / (1 << 20):.2f} MB, {elapsed:.1f} s")
