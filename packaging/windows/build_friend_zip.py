"""Builds the LeadIntel Friend Edition ZIP.

    python packaging/windows/build_friend_zip.py --version 1.0.0 [--commit HEAD] [--allow-dirty] [--allow-unpinned] [--out DIR]

Contents (allow-list only): backend/app, backend/migrations, backend/alembic.ini, backend/pyproject.toml, config/,
the Friend Edition scripts, README.txt and a manifest with every file's SHA-256. Never included: data/, databases,
backups, tests, docs, virtual environments, environment files, credentials. The build refuses files that look
like secrets. Application files come from --commit; with --allow-dirty, uncommitted working-tree changes to
tracked files are used instead and listed in the manifest (for test builds only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "packaging" / "windows"
APP_INCLUDE = ("backend/app/", "backend/migrations/", "config/")
APP_FILES = ("backend/alembic.ini", "backend/pyproject.toml")
EXCLUDE = re.compile(r"(^|/)(__pycache__|\.venv|venv|tests?|docs|data|backups?)(/|$)|\.(pyc|db|sqlite3?|dump|bak|env|pem|key|bundle|patch)$",
                     re.IGNORECASE)
SCRIPTS = ("common.ps1", "setup.ps1", "launch.ps1", "stop.ps1", "load-data.ps1", "export.ps1", "run_cli.py",
           "downloads.json", "requirements-win-py313.lock")
DEV_SCRIPTS = ("test-dataload.ps1",)
ROOT_FILES = ("Setup.cmd", "LeadIntel.cmd", "Stop LeadIntel.cmd", "Load Data.cmd", "Export Prospects.cmd")
CRLF = (".ps1", ".cmd", ".txt")
SECRET_PATTERNS = [
    re.compile(rb"postgres(?:ql)?(?:\+\w+)?://[^/\s:@]+:[^@\s/]+@"),          # URL with an embedded password
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)\b(PGPASSWORD|SUNBIZ_SFTP_PASSWORD)\s*=\s*['\"]?[A-Za-z0-9!@#$%^&*]{6,}"),
    re.compile(rb"(?i)password\s*[:=]\s*['\"][^'\"$\s{}]{6,}['\"]"),
]
SECRET_ALLOW = {  # reviewed matches that are not secrets (placeholders/test strings in code)
}


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--allow-unpinned", action="store_true", help="test builds only: downloads.json may lack fingerprints")
    ap.add_argument("--include-dev-tests", action="store_true", help="test builds only: add test-dataload.ps1 (simulated load outcomes)")
    ap.add_argument("--out", type=Path, default=REPO / "dist")
    a = ap.parse_args()

    commit = git("rev-parse", a.commit).decode().strip()
    tracked = [p for p in git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
               if (p.startswith(APP_INCLUDE) or p in APP_FILES) and not EXCLUDE.search(p)]
    dirty = set(git("diff", "--name-only", commit, "--", *APP_INCLUDE, *APP_FILES).decode().split())
    if dirty and not a.allow_dirty:
        print("uncommitted changes in application files (use --allow-dirty for a test build):\n  " + "\n  ".join(sorted(dirty)))
        return 2
    downloads = json.loads((PKG / "downloads.json").read_text())
    unpinned = [k for k, v in downloads.items() if isinstance(v, dict) and "url" in v and not (v.get("url") and v.get("sha256"))]
    if unpinned and not a.allow_unpinned:
        print(f"downloads.json is missing fingerprints for: {', '.join(unpinned)}")
        return 2

    top = f"LeadIntel-{a.version}"
    entries: dict[str, bytes] = {}
    for p in tracked:
        data = (REPO / p).read_bytes() if p in dirty else git("show", f"{commit}:{p}")
        entries[f"{top}/app/{p}"] = data
    for s in SCRIPTS + (DEV_SCRIPTS if a.include_dev_tests else ()):
        entries[f"{top}/app/packaging/windows/{s}"] = (PKG / s).read_bytes()
    readme = (PKG / "README.txt").read_bytes()
    entries[f"{top}/README.txt"] = readme
    entries[f"{top}/app/README.txt"] = readme
    for r in ROOT_FILES:
        entries[f"{top}/{r}"] = (PKG / "root" / r).read_bytes()
    for name in list(entries):
        if name.endswith(CRLF):
            entries[name] = entries[name].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    problems = []
    for name, data in entries.items():
        if EXCLUDE.search(name.split("/app/", 1)[-1]) and "/packaging/" not in name:
            problems.append(f"excluded path slipped in: {name}")
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(data):
                if (name.split("/", 1)[1], m.group(0)) not in SECRET_ALLOW:
                    problems.append(f"possible secret in {name}: {m.group(0)[:60]!r}")
    if problems:
        print("refusing to build:\n  " + "\n  ".join(problems))
        return 3

    manifest = {
        "product": "LeadIntel Friend Edition", "version": a.version,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_commit": commit, "uncommitted_files_included": sorted(dirty), "unpinned_downloads": unpinned,
        "dev_tests_included": bool(a.include_dev_tests),
        "files": {n.split("/", 1)[1]: hashlib.sha256(d).hexdigest() for n, d in sorted(entries.items())},
    }
    entries[f"{top}/app/manifest.json"] = json.dumps(manifest, indent=1).encode()

    a.out.mkdir(parents=True, exist_ok=True)
    test_build = bool(dirty or unpinned or a.include_dev_tests)
    zpath = a.out / f"{top}{'-TEST' if test_build else ''}.zip"
    if zpath.exists():
        print(f"{zpath} already exists; not overwriting")
        return 2
    with zipfile.ZipFile(zpath, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(entries):
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, entries[name])
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    print(f"built {zpath}  ({zpath.stat().st_size:,} bytes, {len(entries)} files)\nsha256 {digest}")
    if dirty or unpinned:
        print(f"TEST BUILD: uncommitted={sorted(dirty)} unpinned={unpinned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
