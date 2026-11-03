"""Shared helpers for source adapters: source rows, checksummed downloads, source-file registration."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source, SourceFile, SourceRecord
from app.net.http import InfraClient
from app.net.mirrors import raise_for_retryable

CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def payload_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def ensure_source(s: Session, key: str, meta: dict) -> Source:
    row = s.execute(select(Source).where(Source.key == key)).scalar_one_or_none()
    if row is None:
        row = Source(key=key, name=meta.get("name", key), licence=meta.get("licence"),
                     attribution=meta.get("attribution"), reliability=meta.get("reliability", {}))
        s.add(row)
        s.flush()
    else:
        row.reliability = meta.get("reliability", row.reliability)
    return row


@dataclass
class Downloaded:
    path: Path
    sha256: str
    size: int
    reused: bool
    remote_last_modified: datetime | None = None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _file_name(stored: str) -> str:
    """Last path component of a stored path written on Windows or POSIX (never a directory part)."""
    return (PureWindowsPath if "\\" in stored or (len(stored) > 1 and stored[1] == ":") else PurePosixPath)(stored).name


def _cached_candidates(meta_path: Path, stored: str) -> list[Path]:
    """Where a cached version may be, most portable first.

    1. The file of that name next to its sidecar (works after the data folder is moved to another
       folder or computer; the lookup never leaves the sidecar's folder).
    2. The stored absolute path (sidecars written before relative paths were introduced).
    """
    out = []
    name = _file_name(stored)
    if name and name not in (".", ".."):
        out.append(meta_path.parent / name)
    if (PureWindowsPath(stored).is_absolute() or PurePosixPath(stored).is_absolute()) and Path(stored) not in out:
        out.append(Path(stored))
    return out


def data_relative(path: Path, data_dir: Path | None) -> str:
    """Path to record in source_file.local_path: relative (POSIX style) inside the data folder, else absolute."""
    if data_dir is not None:
        try:
            return Path(path).resolve().relative_to(Path(data_dir).resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


DATA_SUBDIRS = ("boundaries", "dbpr", "overture", "sunbiz")


def resolve_local_path(stored: str | None, data_dir: Path) -> Path | None:
    """Find the file a source_file.local_path refers to in the current data folder.

    Relative entries resolve against `data_dir`. Older absolute entries (possibly from another computer
    or operating system) are mapped by their position under a known data subfolder (dbpr/..., sunbiz/...),
    falling back to the stored absolute path. Returns None when no candidate exists.
    """
    if not stored:
        return None
    win = "\\" in stored or (len(stored) > 1 and stored[1] == ":")
    pure = PureWindowsPath(stored) if win else PurePosixPath(stored)
    candidates = []
    if not pure.is_absolute():
        candidates.append(Path(data_dir).joinpath(*pure.parts))
    else:
        parts = pure.parts
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] in DATA_SUBDIRS:
                candidates.append(Path(data_dir).joinpath(*parts[i:]))
                break
        candidates.append(Path(stored))
    root = Path(data_dir).resolve()
    for c in candidates:
        if c.exists() and (c.resolve().is_relative_to(root) or c == Path(stored)):
            return c
    return None


def download_checked(client: InfraClient, url: str, dest: Path, *, expected_size: int | None = None,
                     remote_last_modified: datetime | None = None, auth: tuple[str, str] | None = None) -> Downloaded:
    """Download one remote file version safely; reuse it only when it is provably the same version.

    * Remote identity = size + Last-Modified (from the caller's listing, or a HEAD request).
    * Every version is installed at an immutable, content-addressed path `<stem>.<sha12><suffix>`;
      earlier versions (and the source_file records pointing at them) are never overwritten.
    * `<dest>.meta.json` records the current version (url, size, last_modified, sha256, path). `path` is
      the file NAME (the file sits next to its sidecar), so a moved data folder keeps working; older
      sidecars with an absolute path are still honoured and are rewritten to the portable form once the
      file has been verified.
      Reuse requires: same size AND same Last-Modified AND the local file's SHA-256 still matching.
      If the server gives no Last-Modified, the file is fetched again (a same-size change could not
      be detected otherwise).
    * Downloads go to `<dest>.part` with Range resume. A partial file is resumed only for the same
      remote identity, is never installed or imported unless its size is complete, and a failed
      refresh leaves the previous version and its metadata untouched.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    kw = {"auth": auth} if auth else {}
    if expected_size is None or remote_last_modified is None:
        head = client.head(url, **kw)
        if head.status_code < 400:
            if expected_size is None and head.headers.get("content-length"):
                expected_size = int(head.headers["content-length"])
            if remote_last_modified is None and head.headers.get("last-modified"):
                try:
                    remote_last_modified = parsedate_to_datetime(head.headers["last-modified"])
                except (TypeError, ValueError):
                    remote_last_modified = None
    lm = _iso(remote_last_modified)
    meta_path = dest.with_name(dest.name + ".meta.json")
    meta = _read_json(meta_path)
    if (meta and lm is not None and meta.get("url") == url and meta.get("size") == expected_size
            and meta.get("last_modified") == lm):
        for cur in _cached_candidates(meta_path, str(meta.get("path", ""))):
            if cur.is_file() and cur.stat().st_size == meta["size"] and sha256_file(cur) == meta["sha256"]:
                if meta.get("path") != cur.name and cur.parent == meta_path.parent:
                    _write_json_atomic(meta_path, {**meta, "path": cur.name})  # heal to the portable form
                return Downloaded(cur, meta["sha256"], meta["size"], True, remote_last_modified)
    identity = {"url": url, "size": expected_size, "last_modified": lm}
    part = dest.with_name(dest.name + ".part")
    part_meta = part.with_name(part.name + ".meta.json")
    if part.exists() and _read_json(part_meta) != identity:  # leftover from a different remote version
        part.unlink()
    _write_json_atomic(part_meta, identity)
    offset = part.stat().st_size if part.exists() else 0
    if expected_size is not None and offset > expected_size:
        part.unlink()
        offset = 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with client.stream("GET", url, headers=headers, **kw) as resp:
        if resp.status_code != 416:
            raise_for_retryable(resp, retry_404=False)
            if offset and resp.status_code != 206:
                offset = 0
            with open(part, "ab" if offset else "wb") as fh:
                for block in resp.iter_bytes(CHUNK):
                    fh.write(block)
    size = part.stat().st_size
    if expected_size is not None and size != expected_size:
        raise IOError(f"incomplete download of {url}: {size} of {expected_size} bytes (kept .part for resume)")
    digest = sha256_file(part)
    final = dest.with_name(f"{dest.stem}.{digest[:12]}{dest.suffix}")
    if final.exists() and sha256_file(final) == digest:
        part.unlink()
    else:
        os.replace(part, final)
    _write_json_atomic(meta_path, {"url": url, "size": size, "last_modified": lm, "sha256": digest, "path": final.name})
    part_meta.unlink(missing_ok=True)
    return Downloaded(final, digest, size, False, remote_last_modified)


def register_file(s: Session, source: Source, *, kind: str, remote_path: str, dl: Downloaded, logical_date=None,
                  remote_last_modified=None, status: str = "downloaded", data_dir: Path | None = None) -> SourceFile:
    """One row per (path, content, remote version). Earlier rows are never modified or replaced.

    `local_path` is stored relative to `data_dir` when the file is inside it (portable); otherwise absolute.
    """
    remote_last_modified = remote_last_modified or dl.remote_last_modified
    row = s.execute(select(SourceFile).where(
        SourceFile.source_id == source.id, SourceFile.remote_path == remote_path, SourceFile.sha256 == dl.sha256,
        SourceFile.remote_last_modified.is_not_distinct_from(remote_last_modified))).scalar_one_or_none()
    if row is None:
        row = SourceFile(source_id=source.id, kind=kind, logical_date=logical_date, remote_path=remote_path,
                         size=dl.size, sha256=dl.sha256, local_path=data_relative(dl.path, data_dir), status=status,
                         remote_last_modified=remote_last_modified)
        s.add(row)
        s.flush()
    return row


def upsert_record(s: Session, source: Source, *, kind: str, external_id: str, payload: dict,
                  source_file_id: int | None = None, line_no: int | None = None, raw_line: bytes | None = None,
                  licence_tag: str | None = None) -> tuple[SourceRecord, bool]:
    """Append-only: returns the existing row when the identical payload was already stored."""
    h = payload_hash(payload)
    row = s.execute(select(SourceRecord).where(SourceRecord.source_id == source.id, SourceRecord.record_kind == kind,
                                               SourceRecord.external_id == external_id,
                                               SourceRecord.payload_hash == h)).scalar_one_or_none()
    if row is not None:
        return row, False
    row = SourceRecord(source_id=source.id, record_kind=kind, external_id=external_id, payload=payload,
                       payload_hash=h, source_file_id=source_file_id, line_no=line_no, raw_line=raw_line,
                       licence_tag=licence_tag)
    s.add(row)
    s.flush()
    return row, True
