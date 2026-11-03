"""Sunbiz bulk-data access over HTTPS/WebDAV (verified 2026-11-02, docs/SUNBIZ.md §1-3).

Credentials come only from SUNBIZ_SFTP_USER / SUNBIZ_SFTP_PASSWORD and are passed to httpx as
Basic auth for sftp.floridados.gov only; they are never logged or stored.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote

from app.net.http import InfraClient
from app.net.mirrors import raise_for_retryable
from app.sources.common import Downloaded, download_checked

BASE = "https://sftp.floridados.gov"
FOLDERS = {  # actual, case-sensitive paths (SUNBIZ.md §3)
    "cor": ("/Public/doc/cor/", re.compile(r"^(\d{8})c\.txt$")),
    "cor_event": ("/Public/doc/cor/Events/", re.compile(r"^(\d{8})ce\.txt$")),
    "fic": ("/Public/doc/fic/", re.compile(r"^(\d{8})f\.txt$")),
    "fic_event": ("/Public/doc/fic/Events/", re.compile(r"^(\d{8})fe\.txt$")),
}


class MissingCredentials(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteFile:
    kind: str
    path: str
    day: date
    size: int
    last_modified: datetime | None


def credentials() -> tuple[str, str]:
    u, p = os.environ.get("SUNBIZ_SFTP_USER"), os.environ.get("SUNBIZ_SFTP_PASSWORD")
    if not u or not p:
        raise MissingCredentials("SUNBIZ_SFTP_USER / SUNBIZ_SFTP_PASSWORD are not set (public login from the "
                                 "official Data Downloads page)")
    return u, p


def parse_propfind(xml_bytes: bytes) -> list[tuple[str, int | None, datetime | None]]:
    root = ET.fromstring(xml_bytes)
    out = []
    for resp in root.iter():
        if not resp.tag.endswith("response"):
            continue
        href = size = mod = None
        for el in resp.iter():
            t = el.tag.rsplit("}", 1)[-1]
            if t == "href":
                href = unquote(el.text or "")
            elif t == "getcontentlength" and el.text:
                size = int(el.text)
            elif t == "getlastmodified" and el.text:
                try:
                    mod = parsedate_to_datetime(el.text)
                except (TypeError, ValueError):
                    mod = None
        if href:
            out.append((href, size, mod))
    return out


class SunbizClient:
    def __init__(self, client: InfraClient, data_dir: Path):
        self.client = client
        self.dir = data_dir / "sunbiz"

    def list_kind(self, kind: str) -> list[RemoteFile]:
        folder, pat = FOLDERS[kind]
        resp = self.client.request("PROPFIND", BASE + quote(folder), headers={"Depth": "1"}, auth=credentials())
        if resp.status_code == 401:
            raise PermissionError("Sunbiz rejected the configured credentials")
        if resp.status_code != 207:
            raise_for_retryable(resp, retry_404=False)
        files = []
        for href, size, mod in parse_propfind(resp.content):
            name = href.rstrip("/").rsplit("/", 1)[-1]
            m = pat.match(name)
            if m and size is not None:
                d = m.group(1)
                files.append(RemoteFile(kind, href, date(int(d[:4]), int(d[4:6]), int(d[6:])), size, mod))
        return sorted(files, key=lambda f: f.day)

    def download(self, rf: RemoteFile) -> Downloaded:
        dest = self.dir / rf.kind / rf.path.rsplit("/", 1)[-1]
        if dest.exists() and dest.stat().st_size != rf.size:  # remote file changed (regenerated) -> refetch
            dest.unlink()
        return download_checked(self.client, BASE + quote(rf.path), dest, expected_size=rf.size, auth=credentials())
