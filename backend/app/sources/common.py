"""Shared helpers for source adapters: source rows, checksummed downloads, source-file registration."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
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


def download_checked(client: InfraClient, url: str, dest: Path, *, expected_size: int | None = None,
                     auth: tuple[str, str] | None = None) -> Downloaded:
    """Download to `dest` via a .part file with Range resume; verify size; install atomically.

    Reuses an existing file when its size matches `expected_size` (or the server's Content-Length).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    kw = {"auth": auth} if auth else {}
    if expected_size is None:
        head = client.head(url, **kw)
        if head.status_code < 400 and head.headers.get("content-length"):
            expected_size = int(head.headers["content-length"])
    if dest.exists() and expected_size is not None and dest.stat().st_size == expected_size:
        return Downloaded(dest, sha256_file(dest), expected_size, True)
    part = dest.with_name(dest.name + ".part")
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
    os.replace(part, dest)
    return Downloaded(dest, sha256_file(dest), size, False)


def register_file(s: Session, source: Source, *, kind: str, remote_path: str, dl: Downloaded, logical_date=None,
                  remote_last_modified=None, status: str = "downloaded") -> SourceFile:
    row = s.execute(select(SourceFile).where(SourceFile.source_id == source.id, SourceFile.remote_path == remote_path,
                                             SourceFile.sha256 == dl.sha256)).scalar_one_or_none()
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
