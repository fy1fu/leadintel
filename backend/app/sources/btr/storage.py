"""Managed storage for manually imported BTR CSV files (Phase B0).

Since B0 `leadintel import-btr` copies the user's CSV into the data folder as
`btr/<county>/<safe name>.<sha12><suffix>` and records that relative path, so the import survives a moved
data folder and is included in backups. Rows written before B0 keep their absolute path to the user's own
file; they are never rewritten, and their files are never touched.

Guarantees
* The importer reads only bytes that were staged, hashed and header-checked: the user's file is copied once
  (streamed, with the size limit enforced while copying and a check that it did not change meanwhile), and
  everything afterwards uses that copy.
* A committed row only ever points at a final file that already existed and was verified before the commit.
* A file is never overwritten: placement uses an operation that fails if the destination exists
  (Windows rename, POSIX hard link, or exclusive creation where hard links are unavailable); if none can be
  guaranteed, the import is refused.
* Nothing that existed before the operation is deleted. Only this operation's own staging file (created
  with exclusive mode) may be removed, and a final file this operation created exclusively but could not
  finish writing.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.locks import WRITE_CHECK_BYTES, LockLost, UnlockedWrite, ensure_write_allowed
from app.db.models import Source, SourceFile
from app.sources.btr.csv_import import MappingError, import_btr_stream, open_csv_text, validate_mapping
from app.sources.common import (BTR_KIND, WINDOWS_RESERVED, Downloaded, ensure_source, managed_btr_relpath,
                                register_file, sha256_file, valid_county_key)

CHUNK = 1 << 20
DEFAULT_MAX_MB = 512
MAX_SETTING_MB = 4096
WINDOWS_MAX_PATH = 240


class BtrImportError(ValueError):
    """Invalid input or a refused import; nothing was registered (CLI exit code 2)."""


class FileTooLarge(BtrImportError):
    pass


class SourceChanged(BtrImportError):
    pass


class ManagedFileConflict(BtrImportError):
    pass


class NoReplaceUnsupported(BtrImportError):
    pass


# ------------------------------------------------------------------ settings and names
def btr_max_bytes(env=os.environ) -> int:
    raw = (env.get("LEADINTEL_BTR_MAX_MB") or "").strip()
    if not raw:
        return DEFAULT_MAX_MB << 20
    if not re.fullmatch(r"\d+", raw) or not 1 <= int(raw) <= MAX_SETTING_MB:
        raise BtrImportError(f"LEADINTEL_BTR_MAX_MB must be a whole number from 1 to {MAX_SETTING_MB}, not {raw!r}")
    return int(raw) << 20


def safe_county(key: str) -> str:
    if not valid_county_key(key):
        raise BtrImportError(f"invalid county key {key!r}: use the lower-case area key, e.g. duval or st_johns")
    return key


def safe_btr_filename(name: str, sha256: str) -> str:
    """A Windows- and POSIX-safe content-addressed file name for the user's CSV name."""
    base = re.split(r"[\\/]", name)[-1]
    base = unicodedata.normalize("NFC", base)
    m = re.search(r"\.([A-Za-z0-9]{1,10})$", base)
    suffix, stem = (("." + m.group(1).lower()), base[:m.start()]) if m else (".csv", base)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    stem = re.sub(r"_{2,}", "_", re.sub(r"\.{2,}", ".", stem)).strip(" ._-") or "btr"
    if stem.lower() in WINDOWS_RESERVED or stem.split(".")[0].lower() in WINDOWS_RESERVED:
        stem = "_" + stem
    return f"{stem[:100]}.{sha256[:12]}{suffix}"


def btr_folder(data_dir: Path, county: str) -> Path:
    """data/btr/<county>, refusing any link or junction that would lead outside the data folder."""
    root = Path(data_dir).resolve()
    folder = Path(data_dir) / "btr" / safe_county(county)
    if folder.resolve() != root / "btr" / county:
        raise BtrImportError(f"{folder} is redirected outside the data folder (link or junction); refusing")
    return folder


def check_path_length(p: Path, *, windows: bool | None = None) -> None:
    windows = os.name == "nt" if windows is None else windows
    if windows and len(str(Path(p).resolve())) > WINDOWS_MAX_PATH:
        raise BtrImportError(f"the stored path would exceed {WINDOWS_MAX_PATH} characters on Windows: {p}")


# ------------------------------------------------------------------ staging
@dataclass
class Staged:
    path: Path
    sha256: str
    size: int


def _open_source(path: Path):            # indirection for tests
    return open(path, "rb")


def _stat_key(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def discard_own(path: Path) -> None:
    """Remove a file THIS operation created exclusively (staging file). Never used on anything else."""
    try:
        ensure_write_allowed(path)
        path.unlink(missing_ok=True)
    except (LockLost, UnlockedWrite, OSError):
        pass  # a leftover staging file is harmless: never registered, excluded from backups


def stage_input(src: Path, folder: Path, limit: int) -> Staged:
    """Copy the user's file once into the btr folder, hashing it; stop at `limit` bytes; detect changes."""
    st0 = os.stat(src)
    if not os.path.isfile(src):
        raise BtrImportError(f"{src} is not a regular file")
    if not folder.is_dir():
        ensure_write_allowed(folder)
        folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{secrets.token_hex(8)}.{os.getpid()}.part"
    ensure_write_allowed(tmp)
    h, n, since = hashlib.sha256(), 0, 0
    try:
        with _open_source(src) as fin, open(tmp, "xb") as fout:
            while chunk := fin.read(CHUNK):
                n += len(chunk)
                if n > limit:
                    raise FileTooLarge(f"{src} exceeds the import limit of {limit >> 20} MB")
                if since >= WRITE_CHECK_BYTES:
                    ensure_write_allowed(tmp)
                    since = 0
                fout.write(chunk)
                h.update(chunk)
                since += len(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if _stat_key(os.stat(src)) != _stat_key(st0) or n != st0.st_size:
            raise SourceChanged(f"{src} changed while it was being copied; nothing was imported - try again")
    except BaseException:
        discard_own(tmp)
        raise
    return Staged(tmp, h.hexdigest(), n)


def verify_staged(staged: Staged, mapping: dict[str, str]) -> list[str]:
    """Re-read the staged copy: its hash must match, and ITS header must satisfy the mapping."""
    if sha256_file(staged.path) != staged.sha256:
        raise SourceChanged("the staged copy does not match what was read; nothing was imported")
    with open(staged.path, "rb") as raw:
        header = next(csv.reader(open_csv_text(raw)), [])
    validate_mapping(header, mapping)
    return header


# ------------------------------------------------------------------ placement that never overwrites
_link = os.link            # indirections for tests
_rename = os.rename


def _open_exclusive(path: Path):
    return open(path, "xb")


_NO_LINK = {errno.EPERM, errno.EXDEV, errno.EMLINK, errno.ENOSYS, getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -2)}


def _fsync_dir(folder: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def no_replace_place(staged: Path, target: Path) -> str:
    """Place `staged` at `target` only if `target` does not exist; FileExistsError otherwise.

    Returns the strategy used: 'rename' (Windows), 'link' (POSIX hard link) or 'exclusive' (POSIX exclusive
    creation where hard links are unavailable). Raises NoReplaceUnsupported if no strategy can guarantee it.
    """
    ensure_write_allowed(target)
    if os.name == "nt":
        _rename(staged, target)                 # MoveFileExW without REPLACE_EXISTING: fails if target exists
        return "rename"
    try:
        _link(staged, target)                   # link(2): fails with EEXIST if target exists, atomically
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_LINK:
            raise
        _exclusive_copy(staged, target)
        discard_own(staged)
        _fsync_dir(target.parent)
        return "exclusive"
    discard_own(staged)
    _fsync_dir(target.parent)
    return "link"


def _exclusive_copy(staged: Path, target: Path) -> None:
    try:
        fout = _open_exclusive(target)          # O_CREAT|O_EXCL: fails if target exists
    except FileExistsError:
        raise
    except OSError as exc:
        raise NoReplaceUnsupported(f"this file system cannot guarantee no-overwrite placement ({exc}); "
                                   "nothing was imported") from exc
    try:
        with fout, open(staged, "rb") as fin:
            since = 0
            while chunk := fin.read(CHUNK):
                if since >= WRITE_CHECK_BYTES:
                    ensure_write_allowed(target)
                    since = 0
                fout.write(chunk)
                since += len(chunk)
            fout.flush()
            os.fsync(fout.fileno())
    except BaseException:
        discard_own(target)                     # O_EXCL proves this operation created it
        raise


def place_verified(staged: Staged, target: Path) -> str:
    """Place the staged bytes at `target` (never overwriting) and verify them there.

    Returns 'placed:<strategy>' or 'reused' (an identical file was already there; the staging copy is then
    the caller's to discard). Raises ManagedFileConflict if a different file is there.
    """
    check_path_length(target)
    try:
        if os.path.lexists(target):
            raise FileExistsError(target)
        strategy = no_replace_place(staged.path, target)
    except FileExistsError:
        if target.is_file() and not target.is_symlink() and sha256_file(target) == staged.sha256:
            return "reused"
        raise ManagedFileConflict(f"{target} already exists with different content (or is not a regular file); "
                                  "it was not modified - check it manually") from None
    if sha256_file(target) != staged.sha256:
        raise ManagedFileConflict(f"{target} does not match the verified copy after placement; it was left in "
                                  "place, unregistered - check it manually")
    return f"placed:{strategy}"


def recreate_missing_managed(row, staged: Staged, data_dir: Path) -> Path:
    """Recreate a MISSING managed file at the row's own stored path, from identical, verified bytes only."""
    parts = managed_btr_relpath(row.local_path)
    if parts is None:
        raise BtrImportError("only managed btr/<county>/<file> rows can be recreated; external rows are never repaired")
    if not row.sha256:
        raise BtrImportError("the existing row has no SHA-256, so its file cannot be verified (unverifiable)")
    if row.sha256 != staged.sha256:
        raise BtrImportError("the supplied file does not match the row's SHA-256; nothing was recreated")
    target = Path(data_dir).joinpath(*parts)
    if os.path.lexists(target):
        raise ManagedFileConflict(f"{target} is occupied; it is never overwritten")
    if not target.parent.is_dir():
        btr_folder(data_dir, parts[1])
        ensure_write_allowed(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
    result = place_verified(staged, target)
    if result == "reused":   # appeared meanwhile with identical content
        discard_own(staged.path)
    return target


# ------------------------------------------------------------------ hashing while importing
class HashingReader(io.RawIOBase):
    def __init__(self, raw):
        self._raw, self.h = raw, hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = self._raw.readinto(b)
        if n:
            self.h.update(memoryview(b)[:n])
        return n or 0


def import_from_verified(path: Path, expected_sha256: str, consume) -> object:
    """Run `consume(text_stream)` over `path`, hashing every byte read; refuse if the bytes differ."""
    with open(path, "rb") as raw:
        hr = HashingReader(raw)
        buffered = io.BufferedReader(hr, buffer_size=CHUNK)
        text_stream = open_csv_text(buffered)
        result = consume(text_stream)
        text_stream.detach()            # keep the byte stream open (a discarded wrapper would close it)
        while buffered.read(CHUNK):     # hash whatever the importer did not need to read
            pass
    if hr.h.hexdigest() != expected_sha256:
        raise SourceChanged(f"{path} changed after it was verified; the import was not committed")
    return result


# ------------------------------------------------------------------ the import command
@dataclass
class ImportOutcome:
    action: str                 # new | reused_managed | reused_external | recreated
    stored: str
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def prevalidate(csv_path: Path, county: str, mapping) -> int:
    """Fast checks before the lock (not authoritative for the file: it is re-checked while copying)."""
    limit = btr_max_bytes()
    safe_county(county)
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise BtrImportError("the mapping file must map field names to CSV column names")
    if not csv_path.is_file():
        raise BtrImportError(f"{csv_path} is not a file")
    if csv_path.stat().st_size > limit:
        raise FileTooLarge(f"{csv_path} exceeds the import limit of {limit >> 20} MB")
    return limit


def _existing_row(s: Session, county: str, name: str, sha: str) -> SourceFile | None:
    return s.execute(select(SourceFile).join(Source, Source.id == SourceFile.source_id).where(
        Source.key == f"btr_{county}", SourceFile.remote_path == f"manual:{name}", SourceFile.sha256 == sha,
        SourceFile.remote_last_modified.is_(None))).scalar_one_or_none()


def run_btr_import(engine, data_dir: Path, csv_path: Path, county: str, mapping: dict, limit: int) -> ImportOutcome:
    """Steps 2-10 of the B0 sequence. Must be called with the maintenance lock held (the gates enforce it)."""
    name = csv_path.name
    warnings: list[str] = []
    with Session(engine) as s:
        fips = s.execute(text("SELECT spec->>'geoid' FROM area WHERE key=:k"), {"k": county}).scalar()
        if not fips:
            raise BtrImportError(f"unknown area {county}")
        folder = btr_folder(data_dir, county)
        staged = stage_input(csv_path, folder, limit)
        staging_only = True
        try:
            verify_staged(staged, mapping)
            row = _existing_row(s, county, name, staged.sha256)
            if row is not None and managed_btr_relpath(row.local_path) is None:
                action, read_path, stored = "reused_external", staged.path, row.local_path
                if not Path(row.local_path).is_file():
                    warnings.append(f"the original external file of this import is missing: {row.local_path} "
                                    "(left as recorded; it is not repaired)")
            elif row is not None:
                target = Path(data_dir).joinpath(*managed_btr_relpath(row.local_path))
                stored = row.local_path
                if os.path.lexists(target):
                    if not target.is_file() or sha256_file(target) != staged.sha256:
                        raise ManagedFileConflict(f"the managed file {target} was altered; nothing was changed")
                    action, read_path = "reused_managed", staged.path
                else:
                    recreate_missing_managed(row, staged, data_dir)
                    staging_only = False
                    action, read_path = "recreated", target
                    warnings.append(f"the missing managed file {stored} was recreated from identical bytes")
            else:
                target = folder / safe_btr_filename(name, staged.sha256)
                result = place_verified(staged, target)
                staging_only = result == "reused"
                action, read_path, stored = "new", target, None
            src = ensure_source(s, f"btr_{county}", {"name": f"{county} business tax receipts (manual CSV)",
                                                      "licence": "Public record", "reliability": {"btr": 0.85}})
            if row is None:
                row = register_file(s, src, kind=BTR_KIND, remote_path=f"manual:{name}",
                                    dl=Downloaded(read_path, staged.sha256, staged.size, True), data_dir=data_dir)
                stored = row.local_path
            st = import_from_verified(read_path, staged.sha256,
                                      lambda stream: import_btr_stream(s, src, row, stream, mapping, fips))
            try:
                s.commit()
            except Exception as exc:
                s.rollback()
                exc.add_note(_commit_outcome(engine, county, name, staged.sha256))
                raise
        finally:
            if staging_only:
                discard_own(staged.path)
    return ImportOutcome(action, stored, {k: v for k, v in st.__dict__.items()}, warnings)


def _commit_outcome(engine, county: str, name: str, sha: str) -> str:
    """After a failed commit, report from a fresh read whether the row exists (outcome can be unclear)."""
    try:
        with Session(engine) as s:
            found = _existing_row(s, county, name, sha) is not None
        return "commit failed; a fresh read shows the import WAS committed" if found else \
            "commit failed; a fresh read shows nothing was committed - re-running is safe"
    except Exception as exc:  # noqa: BLE001
        return f"commit failed and its outcome is unclear ({type(exc).__name__}); re-running is safe"


__all__ = ["BtrImportError", "FileTooLarge", "SourceChanged", "ManagedFileConflict", "NoReplaceUnsupported",
           "MappingError", "btr_max_bytes", "safe_county", "safe_btr_filename", "btr_folder", "stage_input",
           "verify_staged", "no_replace_place", "place_verified", "recreate_missing_managed", "HashingReader",
           "import_from_verified", "prevalidate", "run_btr_import", "ImportOutcome"]
