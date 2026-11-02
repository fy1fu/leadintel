"""Overture Maps mirror handling (AWS S3, Azure Blob, Azure Data Lake DFS).

All three mirrors publish identical release trees. We address files by a mirror-neutral
relative path such as ``2026-10-21.0/theme=places/type=place/part-00000-....parquet``
and translate it to each mirror's URL scheme, which makes per-file failover trivial.
Only anonymous, read-only HTTPS requests are used.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal
from urllib.parse import quote, urlencode

from app.net.registry import ProbeSpec, Service

if TYPE_CHECKING:
    from app.net.probe import Prober

MirrorKind = Literal["s3", "azure_blob", "azure_dfs"]
Fetch = Callable[[str], bytes]
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


def mirror_kind(service: Service) -> MirrorKind:
    h = service.host.lower()
    if ".s3." in h or h.endswith("amazonaws.com"):
        return "s3"
    if h.endswith(".blob.core.windows.net"):
        return "azure_blob"
    if h.endswith(".dfs.core.windows.net"):
        return "azure_dfs"
    raise ValueError(f"{service.key}: not a known Overture mirror host ({service.host})")


def object_url(service: Service, rel: str) -> str:
    kind = mirror_kind(service)
    q = quote(rel, safe="/=-._")
    if kind == "s3":
        return f"{service.base_url}/release/{q}"
    return f"{service.base_url}/release/{q}"  # Azure blob and DFS share the container-path layout


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def list_releases(fetch: Fetch, service: Service) -> list[str]:
    kind = mirror_kind(service)
    names: list[str] = []
    if kind == "s3":
        root = ET.fromstring(fetch(f"{service.base_url}/?list-type=2&prefix=release/&delimiter=/"))
        _strip_ns(root)
        names = [p.text.split("/")[1] for p in root.iter("Prefix") if p.text and p.text.count("/") == 2]
    elif kind == "azure_blob":
        root = ET.fromstring(fetch(f"{service.base_url}/release?restype=container&comp=list&delimiter=/&maxresults=500"))
        names = [n.text.rstrip("/") for n in root.iter("Name") if n.text]
    else:
        data = json.loads(fetch(f"{service.base_url}/release?resource=filesystem&recursive=false&maxResults=500"))
        names = [p["name"] for p in data.get("paths", []) if str(p.get("isDirectory")).lower() == "true"]
    return sorted({n for n in names if RELEASE_RE.match(n)})


def list_objects(fetch: Fetch, service: Service, release: str, theme: str = "places", type_: str = "place",
                 limit: int | None = None) -> list[str]:
    """Return mirror-neutral relative paths of the parquet files for theme/type in a release."""
    kind = mirror_kind(service)
    prefix_rel = f"{release}/theme={theme}/type={type_}/"
    out: list[str] = []
    token: str | None = None
    while True:
        if kind == "s3":
            params = {"list-type": "2", "prefix": "release/" + prefix_rel, "max-keys": str(limit or 1000)}
            if token:
                params["continuation-token"] = token
            root = ET.fromstring(fetch(f"{service.base_url}/?{urlencode(params)}"))
            _strip_ns(root)
            out += [k.text[len("release/"):] for k in root.iter("Key") if k.text and k.text.endswith(".parquet")]
            nxt = root.find("NextContinuationToken")
            token = nxt.text if nxt is not None and nxt.text else None
        elif kind == "azure_blob":
            params = {"restype": "container", "comp": "list", "prefix": prefix_rel, "maxresults": str(limit or 1000)}
            if token:
                params["marker"] = token
            root = ET.fromstring(fetch(f"{service.base_url}/release?{urlencode(params)}"))
            out += [n.text for n in root.iter("Name") if n.text and n.text.endswith(".parquet")]
            nm = root.find("NextMarker")
            token = nm.text if nm is not None and nm.text else None
        else:
            params = {"resource": "filesystem", "directory": prefix_rel.rstrip("/"), "recursive": "false",
                      "maxResults": str(limit or 1000)}
            data = json.loads(fetch(f"{service.base_url}/release?{urlencode(params)}"))
            out += [p["name"] for p in data.get("paths", []) if p["name"].endswith(".parquet")]
            token = None  # DFS continuation is header-based; release directories are well under 1000 files
        if not token or (limit and len(out) >= limit):
            break
    return sorted(out)[: limit or None]


def resolve_sample_object_url(prober: "Prober", service: Service, spec: ProbeSpec) -> str:
    """Probe resolver: URL of the first places parquet file of the latest release on this mirror."""
    from app.net.probe import ProbeFailure, ProbeResult

    scratch = ProbeResult(service.key, spec.key + ":list", "http", service.host, service.port, None,
                          datetime.now(timezone.utc))

    def fetch(url: str) -> bytes:
        return prober.fetch(url, scratch, max_bytes=4_000_000)[2]

    releases = list_releases(fetch, service)
    if not releases:
        raise ProbeFailure("resolve", "no_releases_listed", f"{service.host} listed no Overture releases")
    objs = list_objects(fetch, service, releases[-1], limit=1)
    if not objs:
        raise ProbeFailure("resolve", "no_objects_listed", f"no places files in release {releases[-1]}")
    return object_url(service, objs[0])
