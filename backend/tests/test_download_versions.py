"""Fix 1 regression tests: a changed remote version is always re-downloaded, history is kept."""
from datetime import date, datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.costs.guard import CostGuard
from app.db.models import SourceFile
from app.net.http import InfraClient
from app.net.mirrors import RetryableServiceError
from app.net.registry import load_registry
from app.sources.common import download_checked, ensure_source, register_file, sha256_file
from app.sources.sunbiz.client import BASE, RemoteFile, SunbizClient, needs_refresh
from helpers import make_settings

# Calls managed-file writers directly: explicit, temp-folder-only opt-out of the fail-closed file gate (B0).
pytestmark = pytest.mark.usefixtures("unlocked_files")

T1 = datetime(2026, 10, 27, 2, 30, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 27, 14, 35, tzinfo=timezone.utc)
PATH = "/Public/doc/cor/20261026c.txt"
REG = load_registry(REPO_ROOT / "config" / "network.yaml")


class FakeSunbiz:
    """Serves one file; content/timestamp can change; can truncate or fail; honours Range."""

    def __init__(self, content: bytes, modified: datetime):
        self.content, self.modified = content, modified
        self.gets, self.ranges = 0, []
        self.truncate_next = False
        self.fail = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500, content=b"server error")
        hdr = {"Last-Modified": format_datetime(self.modified, usegmt=True)}
        if req.method == "HEAD":
            return httpx.Response(200, headers=hdr | {"Content-Length": str(len(self.content))})
        self.gets += 1
        rng = req.headers.get("range")
        self.ranges.append(rng)
        body, status = self.content, 200
        if rng:
            start = int(rng[6:].split("-")[0])
            body, status = self.content[start:], 206
            hdr["Content-Range"] = f"bytes {start}-{len(self.content) - 1}/{len(self.content)}"
        if self.truncate_next:
            self.truncate_next = False
            body = body[: len(body) // 2]
        return httpx.Response(status, content=body, headers=hdr)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUNBIZ_SFTP_USER", "Public")
    monkeypatch.setenv("SUNBIZ_SFTP_PASSWORD", "test-only")
    st = make_settings(tmp_path)
    return tmp_path, st


def run(env, server, modified=None):
    tmp_path, st = env
    rf = RemoteFile("cor", PATH, date(2026, 10, 26), len(server.content), modified or server.modified)
    with InfraClient(REG, st, CostGuard(REG, st), transport=httpx.MockTransport(server.handler)) as c:
        return SunbizClient(c, tmp_path).download(rf)


def test_1_same_size_same_timestamp_is_reused_without_download(env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    a = run(env, srv)
    b = run(env, srv)
    assert (a.reused, b.reused, srv.gets) == (False, True, 1)
    assert a.path == b.path and a.sha256 == b.sha256


def test_2_different_content_same_size_newer_timestamp_is_downloaded(env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    old = run(env, srv)
    srv.content, srv.modified = b"B" * 1441, T2
    new = run(env, srv)
    assert not new.reused and srv.gets == 2
    assert new.sha256 != old.sha256 and new.path.read_bytes() == b"B" * 1441
    assert old.path.read_bytes() == b"A" * 1441  # earlier version kept, never overwritten


def test_3_different_size_newer_timestamp_is_downloaded(env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    old = run(env, srv)
    srv.content, srv.modified = b"C" * 2882, T2
    new = run(env, srv)
    assert not new.reused and new.size == 2882 and old.path.exists()


def test_4_interrupted_download_is_never_installed_and_resumes(env):
    srv = FakeSunbiz(bytes(range(256)) * 20, T1)
    srv.truncate_next = True
    with pytest.raises(IOError, match="incomplete download"):
        run(env, srv)
    d = env[0] / "sunbiz" / "cor"
    assert not list(d.glob("20261026c.*.txt")) and not (d / "20261026c.txt.meta.json").exists()
    good = run(env, srv)  # resumes from the kept .part (Range request), then verifies the full size
    assert srv.ranges[-1] and srv.ranges[-1].startswith("bytes=")
    assert good.path.read_bytes() == srv.content and good.sha256 == sha256_file(good.path)


def test_5_failed_refresh_keeps_previous_file_and_metadata(env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    old = run(env, srv)
    meta = (env[0] / "sunbiz" / "cor" / "20261026c.txt.meta.json").read_text()
    srv.content, srv.modified, srv.fail = b"B" * 1441, T2, True
    with pytest.raises(RetryableServiceError):  # same error class the existing mirror/retry logic handles
        run(env, srv)
    assert old.path.read_bytes() == b"A" * 1441 and sha256_file(old.path) == old.sha256
    assert (env[0] / "sunbiz" / "cor" / "20261026c.txt.meta.json").read_text() == meta


def test_missing_last_modified_forces_refresh_and_locally_corrupted_cache_is_redownloaded(env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    first = run(env, srv)
    first.path.write_bytes(b"X" * 1441)  # same size, corrupted locally
    again = run(env, srv)
    assert not again.reused and again.path.read_bytes() == b"A" * 1441


@pytest.mark.parametrize("size,mod,remote_size,remote_mod,expected", [
    (1441, T1, 1441, T1, False),   # unchanged
    (1441, T1, 1441, T2, True),    # same size, newer timestamp
    (1441, T1, 2882, T2, True),    # size and timestamp changed
    (1441, T2, 1441, T1, True),    # timestamp differs (even older): different version
    (None, None, 1441, T1, True),  # never imported
    (1441, T1, 1441, None, True),  # no remote timestamp: cannot prove sameness
])
def test_needs_refresh_rules(size, mod, remote_size, remote_mod, expected):
    rf = RemoteFile("cor", PATH, date(2026, 10, 26), remote_size, remote_mod)
    assert needs_refresh(size, mod, rf) is expected


def test_source_file_history_keeps_every_version_and_checksum(db, env):
    srv = FakeSunbiz(b"A" * 1441, T1)
    with Session(db) as s:
        src = ensure_source(s, "sunbiz", {"name": "Sunbiz"})
        v1 = run(env, srv)
        r1 = register_file(s, src, kind="cor", remote_path=PATH, dl=v1, remote_last_modified=T1, status="imported")
        srv.content, srv.modified = b"B" * 1441, T2
        v2 = run(env, srv)
        r2 = register_file(s, src, kind="cor", remote_path=PATH, dl=v2, remote_last_modified=T2, status="imported")
        srv.content, srv.modified = b"B" * 1441, datetime(2026, 10, 28, tzinfo=timezone.utc)  # same content, republished
        v3 = run(env, srv)
        r3 = register_file(s, src, kind="cor", remote_path=PATH, dl=v3, status="imported")
        rows = s.execute(select(SourceFile).order_by(SourceFile.id)).scalars().all()
        assert [r.id for r in rows] == [r1.id, r2.id, r3.id]
        assert rows[0].sha256 == v1.sha256 and rows[0].local_path == str(v1.path) and v1.path.exists()
        assert rows[1].sha256 == rows[2].sha256 and rows[1].remote_last_modified != rows[2].remote_last_modified
        s.commit()
