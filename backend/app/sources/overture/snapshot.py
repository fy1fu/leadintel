"""Local Overture snapshots per release (approved decision F6).

A snapshot is a GeoParquet-compatible file holding every place whose bbox intersects the
requested area bbox, for one Overture release, plus a manifest:

    data/overture/<release>/places_<bboxhash>.parquet
    data/overture/<release>/places_<bboxhash>.manifest.json

The manifest records release, bbox, the mirror used for listing, every remote source file with
its size, row count, SHA-256 of the snapshot and creation time. A snapshot is reused when the
release matches, its bbox covers the request and the checksum verifies; then no remote data is
read. A checksum mismatch (truncated/corrupt file) triggers a rebuild. Cleanup keeps the newest
`keep` releases and never deletes a release referenced by an unfinished discovery run.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from app.db.locks import ensure_write_allowed
from app.sources.common import sha256_file
from app.sources.overture.reader import BBox, OvertureReader

SNAPSHOT_COLUMNS = ["id", "names", "basic_category", "taxonomy", "confidence", "operating_status", "websites",
                    "phones", "emails", "socials", "brand", "addresses", "sources", "geometry", "bbox", "version"]


@dataclass
class Snapshot:
    release: str
    path: Path
    manifest: dict
    reused: bool


def _bbox_key(b: BBox) -> str:
    return hashlib.sha256(json.dumps([round(b.xmin, 4), round(b.ymin, 4), round(b.xmax, 4), round(b.ymax, 4)]).encode()).hexdigest()[:12]


def _covers(outer: list[float], inner: BBox) -> bool:
    return outer[0] <= inner.xmin and outer[1] <= inner.ymin and outer[2] >= inner.xmax and outer[3] >= inner.ymax


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root / "overture"
        if not self.root.is_dir():
            ensure_write_allowed(self.root)
            self.root.mkdir(parents=True, exist_ok=True)

    def find(self, release: str, bbox: BBox) -> Snapshot | None:
        d = self.root / release
        for m in sorted(d.glob("*.manifest.json")) if d.exists() else []:
            man = json.loads(m.read_text())
            data = d / man["file"]
            if man.get("status") != "complete" or not _covers(man["bbox"], bbox) or not data.exists():
                continue
            if sha256_file(data) != man["sha256"]:
                continue  # corrupt or truncated -> caller rebuilds
            return Snapshot(release, data, man, True)
        return None

    def latest_local(self) -> Snapshot | None:
        """Newest complete local snapshot (used when the network is unavailable)."""
        for d in sorted((p for p in self.root.iterdir() if p.is_dir()), reverse=True):
            for m in sorted(d.glob("*.manifest.json")):
                man = json.loads(m.read_text())
                data = d / man["file"]
                if man.get("status") == "complete" and data.exists() and sha256_file(data) == man["sha256"]:
                    return Snapshot(d.name, data, man, True)
        return None

    def ensure(self, reader: OvertureReader, bbox: BBox, release: str | None = None) -> Snapshot:
        if release is None:
            release, _svc = reader.latest_release()
        found = self.find(release, bbox)
        if found:
            return found
        d = self.root / release
        key = _bbox_key(bbox)
        data, tmp = d / f"places_{key}.parquet", d / f"places_{key}.parquet.part"
        table, stats = reader.read_bbox(bbox, columns=SNAPSHOT_COLUMNS, release=release)
        ensure_write_allowed(tmp)
        d.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, tmp, compression="zstd")
        ensure_write_allowed(data)
        os.replace(tmp, data)
        man = {
            "release": release, "bbox": [bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax], "file": data.name,
            "rows": table.num_rows, "sha256": sha256_file(data), "size": data.stat().st_size,
            "listing_service": stats.listing_service, "services_used": sorted(stats.services_used),
            "source_files": [{"path": p, "size": n} for p, n in sorted(stats.file_sizes.items())],
            "row_groups_read": stats.row_groups_read, "bytes_read": stats.bytes_read,
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "complete",
            "licence_note": "Overture Maps Places; per-record licences in the `sources` column",
        }
        mpath = d / f"places_{key}.manifest.json"
        mtmp = mpath.with_suffix(".json.part")
        ensure_write_allowed(mtmp)
        mtmp.write_text(json.dumps(man, indent=2))
        ensure_write_allowed(mpath)
        os.replace(mtmp, mpath)
        return Snapshot(release, data, man, False)

    def cleanup(self, keep: int = 2, protected: set[str] | None = None) -> list[str]:
        """Delete old releases, keeping the newest `keep` and any `protected` (in-use) releases."""
        protected = protected or set()
        releases = sorted((p.name for p in self.root.iterdir() if p.is_dir()), reverse=True)
        removed = []
        for name in releases[keep:]:
            if name in protected:
                continue
            ensure_write_allowed(self.root / name)
            shutil.rmtree(self.root / name)
            removed.append(name)
        return removed
