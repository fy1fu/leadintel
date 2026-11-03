"""Read Overture Places for a bounding box over HTTPS range requests, with mirror failover.

Only parquet footers and the row groups whose bbox statistics intersect the area are
downloaded. If a mirror fails mid-read, the same file is continued from the next mirror
(all mirrors publish byte-identical files under the same relative path).
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.net.http import InfraClient
from app.net.mirrors import AllMirrorsFailed, MirrorSet, RetryableServiceError, raise_for_retryable
from app.net.registry import Registry, Service
from app.net.store import NetStore
from app.sources.overture.mirrors import list_objects, list_releases, object_url

DEFAULT_COLUMNS = ["id", "names", "basic_category", "taxonomy", "confidence", "operating_status", "websites",
                   "phones", "emails", "socials", "brand", "addresses", "sources", "geometry", "bbox"]


@dataclass(frozen=True)
class BBox:
    xmin: float
    ymin: float
    xmax: float
    ymax: float


class FailoverRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range requests that fails over between mirrors."""

    def __init__(self, client: InfraClient, services: list[Service], rel: str, store: NetStore | None = None):
        super().__init__()
        self.client, self.services, self.rel, self.store = client, list(services), rel, store
        self._i = 0
        self._pos = 0
        self.bytes_read = 0
        self.size = self._with_failover(self._size)

    def _url(self) -> str:
        return object_url(self.services[self._i], self.rel)

    def _with_failover(self, fn):
        errors = []
        while self._i < len(self.services):
            try:
                return fn()
            except (httpx.TransportError, RetryableServiceError) as exc:
                errors.append(f"{self.services[self._i].key}: {exc}")
                if self.store:
                    self.store.record_event(self.services[self._i].key, "download", False, url=self._url(),
                                            error=str(exc)[:500])
                self._i += 1
        raise RetryableServiceError(f"all mirrors failed for {self.rel}: " + "; ".join(errors))

    def _size(self) -> int:
        resp = raise_for_retryable(self.client.get(self._url(), headers={"Range": "bytes=0-0"}))
        cr = resp.headers.get("content-range", "")
        if "/" not in cr:
            raise RetryableServiceError("server did not honour Range requests")
        return int(cr.rsplit("/", 1)[1])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b) -> int:
        if self._pos >= self.size:
            return 0
        end = min(self.size, self._pos + len(b)) - 1

        def fetch() -> bytes:
            resp = raise_for_retryable(self.client.get(self._url(), headers={"Range": f"bytes={self._pos}-{end}"}))
            if resp.status_code != 206:
                raise RetryableServiceError("range request not honoured")
            return resp.content

        data = self._with_failover(fetch)
        n = len(data)
        b[:n] = data
        self._pos += n
        self.bytes_read += n
        return n


@dataclass
class ReadStats:
    release: str
    listing_service: str
    files: int = 0
    row_groups_read: int = 0
    row_groups_total: int = 0
    bytes_read: int = 0
    services_used: set[str] = field(default_factory=set)
    file_sizes: dict[str, int] = field(default_factory=dict)   # relative path -> remote size


class OvertureReader:
    def __init__(self, registry: Registry, client: InfraClient, store: NetStore | None = None,
                 enabled_extra: list[str] | None = None):
        self.registry, self.client, self.store = registry, client, store
        self.mirrors = MirrorSet(registry, store, "overture_places", enabled_extra)

    def _fetch(self, url: str) -> bytes:
        return raise_for_retryable(self.client.get(url)).content

    def latest_release(self) -> tuple[str, Service]:
        def op(svc: Service) -> str:
            releases = list_releases(self._fetch, svc)
            if not releases:
                raise RetryableServiceError(f"{svc.host} listed no releases")
            return releases[-1]
        release, svc, _ = self.mirrors.run(op, op_name="list")
        return release, svc

    def read_bbox(self, bbox: BBox, columns: list[str] | None = None, release: str | None = None,
                  max_files: int | None = None) -> tuple[pa.Table, ReadStats]:
        if release is None:
            release, _ = self.latest_release()
        files, list_svc, _ = self.mirrors.run(lambda s: list_objects(self._fetch, s, release), op_name="list")
        stats = ReadStats(release=release, listing_service=list_svc.key)
        cols = columns or DEFAULT_COLUMNS
        tables: list[pa.Table] = []
        for rel in files[:max_files]:
            f = FailoverRangeFile(self.client, self.mirrors.services, rel, self.store)
            pf = pq.ParquetFile(f)
            md = pf.metadata
            names = [md.schema.column(i).path for i in range(md.num_columns)]
            ix = {n: names.index(n) for n in ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")}
            keep = []
            for rg in range(md.num_row_groups):
                stats.row_groups_total += 1
                g = md.row_group(rg)
                st = {k: g.column(i).statistics for k, i in ix.items()}
                if any(s is None or not s.has_min_max for s in st.values()):
                    keep.append(rg)
                    continue
                if (st["bbox.xmax"].max < bbox.xmin or st["bbox.xmin"].min > bbox.xmax or
                        st["bbox.ymax"].max < bbox.ymin or st["bbox.ymin"].min > bbox.ymax):
                    continue
                keep.append(rg)
            if keep:
                t = pf.read_row_groups(keep, columns=[c for c in cols if c in pf.schema_arrow.names])
                b = t.column("bbox")
                mask = pc.and_(pc.and_(pc.greater_equal(pc.struct_field(b, "xmax"), bbox.xmin),
                                       pc.less_equal(pc.struct_field(b, "xmin"), bbox.xmax)),
                               pc.and_(pc.greater_equal(pc.struct_field(b, "ymax"), bbox.ymin),
                                       pc.less_equal(pc.struct_field(b, "ymin"), bbox.ymax)))
                tables.append(t.filter(mask))
                stats.row_groups_read += len(keep)
            stats.files += 1
            stats.file_sizes[rel] = f.size
            stats.bytes_read += f.bytes_read
            stats.services_used.add(f.services[min(f._i, len(f.services) - 1)].key)
        if self.store:
            for key in stats.services_used:
                self.store.record_event(key, "download", True, url=f"overture:{release}", nbytes=stats.bytes_read)
        table = pa.concat_tables(tables) if tables else pa.table({})
        return table, stats


__all__ = ["OvertureReader", "BBox", "ReadStats", "FailoverRangeFile", "AllMirrorsFailed"]
