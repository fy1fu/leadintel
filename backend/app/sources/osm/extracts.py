"""OpenStreetMap access strategy: Geofabrik extracts first, Overpass only as a small-area fallback.

* Extracts are downloaded once per region and refreshed only when the published MD5 changes
  (incremental by design - no repeated queries against volunteer-run Overpass servers).
* Downloads resume from a .part file, are verified against the published MD5, and are
  installed atomically. A failed or mismatched download never replaces a good file.
* Mirrors are tried via MirrorSet (capability `osm_extract`), healthiest first.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.db.locks import WRITE_CHECK_BYTES, ensure_write_allowed
from app.net.http import InfraClient
from app.net.mirrors import AllMirrorsFailed, MirrorSet, RetryableServiceError, raise_for_retryable
from app.net.registry import Registry, Service
from app.net.store import NetStore

CHUNK = 1 << 20


@dataclass
class ExtractResult:
    region: str
    path: Path
    md5: str
    downloaded: bool          # False when the local copy was already current
    bytes: int
    service_key: str


@dataclass
class OsmPlan:
    strategy: Literal["extract", "overpass", "unavailable"]
    reason: str
    services: list[str]


def _md5_of(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - matching Geofabrik's published checksum, not for security
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


class GeofabrikExtracts:
    def __init__(self, registry: Registry, client: InfraClient, store: NetStore | None, data_dir: Path,
                 enabled_extra: list[str] | None = None):
        self.registry = registry
        self.client = client
        self.store = store
        self.dir = data_dir / "osm"
        if not self.dir.is_dir():
            ensure_write_allowed(self.dir)
            self.dir.mkdir(parents=True, exist_ok=True)
        self.enabled_extra = enabled_extra

    def _published_md5(self, svc: Service, region: str) -> str:
        resp = raise_for_retryable(self.client.get(f"{svc.base_url}/{region}-latest.osm.pbf.md5"))
        token = resp.text.strip().split()[0] if resp.text.strip() else ""
        if len(token) != 32:
            raise RetryableServiceError(f"malformed md5 file from {svc.host}")
        return token.lower()

    def ensure(self, region: str = "north-america/us/florida") -> ExtractResult:
        """Make sure an up-to-date, verified extract for `region` exists locally."""
        target = self.dir / (region.replace("/", "__") + "-latest.osm.pbf")
        md5_file = target.with_suffix(target.suffix + ".md5")
        mirrors = MirrorSet(self.registry, self.store, "osm_extract", self.enabled_extra)

        def op(svc: Service) -> ExtractResult:
            want = self._published_md5(svc, region)
            if target.exists() and md5_file.exists() and md5_file.read_text().strip() == want:
                return ExtractResult(region, target, want, False, target.stat().st_size, svc.key)
            nbytes = self._download(svc, region, target, want)
            if self.store:
                self.store.record_event(svc.key, "download", True, url=f"{svc.base_url}/{region}-latest.osm.pbf",
                                        nbytes=nbytes)
            return ExtractResult(region, target, want, True, nbytes, svc.key)

        result, _svc, _attempts = mirrors.run(op, op_name="download")
        return result

    def _download(self, svc: Service, region: str, target: Path, want_md5: str) -> int:
        url = f"{svc.base_url}/{region}-latest.osm.pbf"
        part = target.with_suffix(target.suffix + ".part")
        part_md5 = part.with_suffix(".expect")
        # A partial file is only resumable if it belongs to the same published version.
        ensure_write_allowed(part)
        if part.exists() and (not part_md5.exists() or part_md5.read_text().strip() != want_md5):
            part.unlink()
        part_md5.write_text(want_md5)
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self.client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:  # already complete
                pass
            else:
                raise_for_retryable(resp)
                if offset and resp.status_code != 206:
                    offset = 0  # server ignored Range - restart
                since_check = 0
                with open(part, "ab" if offset else "wb") as fh:
                    for block in resp.iter_bytes(CHUNK):
                        if since_check >= WRITE_CHECK_BYTES:
                            ensure_write_allowed(part)
                            since_check = 0
                        fh.write(block)
                        since_check += len(block)
        got = _md5_of(part)
        ensure_write_allowed(target)
        if got != want_md5:
            part.unlink(missing_ok=True)
            part_md5.unlink(missing_ok=True)
            raise RetryableServiceError(f"MD5 mismatch for {url}: got {got}, expected {want_md5}")
        os.replace(part, target)
        target.with_suffix(target.suffix + ".md5").write_text(want_md5)
        part_md5.unlink(missing_ok=True)
        return target.stat().st_size


def plan_osm_access(registry: Registry, store: NetStore | None, area_km2: float,
                    overpass_max_area_km2: float = 250.0, enabled_extra: list[str] | None = None) -> OsmPlan:
    """Choose how OSM data will be obtained for an area, preferring extracts.

    Overpass is only considered for small areas and only when no extract mirror looks usable.
    If neither is usable the plan is `unavailable` and OSM coverage must be recorded as not_run -
    never as "no businesses found".
    """
    latest = store.latest_results() if store else {}

    def usable(capability: str) -> list[str]:
        from app.net.diagnostics import service_status
        keys = []
        for s in registry.services_for(capability):
            if not registry.effective_enabled(s, enabled_extra):
                continue
            rows = latest.get(s.key)
            status = service_status(True, rows)[0] if rows else "not_tested"
            if status in ("operational", "degraded", "not_tested"):
                keys.append(s.key)
        return keys

    ext = usable("osm_extract")
    if ext:
        return OsmPlan("extract", "Geofabrik extract mirror available", ext)
    ovp = usable("osm_overpass")
    if ovp and area_km2 <= overpass_max_area_km2:
        return OsmPlan("overpass", f"no extract mirror usable; area {area_km2:.0f} km² within Overpass limit", ovp)
    why = "no extract mirror usable" + ("" if ovp else " and no Overpass instance usable") + \
          ("" if area_km2 <= overpass_max_area_km2 else f"; area {area_km2:.0f} km² too large for Overpass")
    return OsmPlan("unavailable", why, [])


__all__ = ["GeofabrikExtracts", "ExtractResult", "OsmPlan", "plan_osm_access", "AllMirrorsFailed"]
