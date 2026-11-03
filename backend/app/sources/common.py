"""Shared helpers for source adapters: source rows, checksummed downloads, source-file registration."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

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


def download_checked(client: InfraClient, url: str, dest: Path, *, expected_size: int | None = None,
                     remote_last_modified: datetime | None = None, auth: tuple[str, str] | None = None) -> Downloaded:
    """Download one remote file version safely; reuse it only when it is provably the same version.

    * Remote identity = size + Last-Modified (from the caller's listing, or a HEAD request).
    * Every version is installed at an immutable, content-addressed path `<stem>.<sha12><suffix>`;
      earlier versions (and the source_file records pointing at them) are never overwritten.
    * `<dest>.meta.json` records the current version (url, size, last_modified, sha256, path).
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
        cur = Path(meta["path"])
        if cur.exists() and cur.stat().st_size == meta["size"] and sha256_file(cur) == meta["sha256"]:
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
    _write_json_atomic(meta_path, {"url": url, "size": size, "last_modified": lm, "sha256": digest, "path": str(final)})
    part_meta.unlink(missing_ok=True)
    return Downloaded(final, digest, size, False, remote_last_modified)


def register_file(s: Session, source: Source, *, kind: str, remote_path: str, dl: Downloaded, logical_date=None,
                  remote_last_modified=None, status: str = "downloaded") -> SourceFile:
    """One row per (path, content, remote version). Earlier rows are never modified or replaced."""
    remote_last_modified = remote_last_modified or dl.remote_last_modified
    row = s.execute(select(SourceFile).where(
        SourceFile.source_id == source.id, SourceFile.remote_path == remote_path, SourceFile.sha256 == dl.sha256,
        SourceFile.remote_last_modified.is_not_distinct_from(remote_last_modified))).scalar_one_or_none()
    if row is None:
        row = SourceFile(source_id=source.id, kind=kind, logical_date=logical_date, remote_path=remote_path,
                         size=dl.size, sha256=dl.sha256, local_path=str(dl.path), status=status,
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
