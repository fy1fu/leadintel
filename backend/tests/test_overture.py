import io
import json

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.config import REPO_ROOT
from app.costs.guard import CostGuard
from app.net.http import InfraClient
from app.net.registry import load_registry
from app.net.store import NetStore
from app.sources.overture.mirrors import list_objects, list_releases, object_url
from app.sources.overture.reader import BBox, OvertureReader
from helpers import make_settings

REAL = load_registry(REPO_ROOT / "config" / "network.yaml")
S3, BLOB, DFS = (REAL.service(k) for k in ("overture_s3", "overture_azure_blob", "overture_azure_dfs"))
REL = "2026-10-21.0/theme=places/type=place/part-00000.parquet"


def s3_list(url):
    if "delimiter" in url:
        return (b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                b"<CommonPrefixes><Prefix>release/2026-09-23.0/</Prefix></CommonPrefixes>"
                b"<CommonPrefixes><Prefix>release/2026-10-21.0/</Prefix></CommonPrefixes></ListBucketResult>")
    return (b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<Contents><Key>release/" + REL.encode() + b"</Key></Contents>"
            b"<Contents><Key>release/2026-10-21.0/theme=places/type=place/_SUCCESS</Key></Contents></ListBucketResult>")


def blob_list(url):
    if "delimiter" in url:
        return (b"<EnumerationResults><Blobs><BlobPrefix><Name>2026-09-23.0/</Name></BlobPrefix>"
                b"<BlobPrefix><Name>2026-10-21.0/</Name></BlobPrefix><BlobPrefix><Name>bridgefiles/</Name></BlobPrefix>"
                b"</Blobs><NextMarker /></EnumerationResults>")
    return b"<EnumerationResults><Blobs><Blob><Name>" + REL.encode() + b"</Name></Blob></Blobs><NextMarker /></EnumerationResults>"


def dfs_list(url):
    if "directory=" not in url:
        return json.dumps({"paths": [{"name": "2026-10-21.0", "isDirectory": "true"},
                                     {"name": "2026-09-23.0", "isDirectory": "true"}]}).encode()
    return json.dumps({"paths": [{"name": REL}]}).encode()


@pytest.mark.parametrize("service,fetch", [(S3, s3_list), (BLOB, blob_list), (DFS, dfs_list)])
def test_listing_parsers_for_each_mirror(service, fetch):
    assert list_releases(fetch, service) == ["2026-09-23.0", "2026-10-21.0"]
    assert list_objects(fetch, service, "2026-10-21.0") == [REL]
    assert object_url(service, REL).startswith(f"https://{service.host}/release/2026-10-21.0/theme=places/")


def sample_parquet() -> bytes:
    xs = [-81.40, -81.39, -80.00, -81.385]
    t = pa.table({
        "id": ["a", "b", "far", "c"],
        "names": [{"primary": n} for n in ("Alpha Roofing", "Beta Dental", "Miami Thing", "Gamma Cafe")],
        "bbox": [{"xmin": x, "xmax": x, "ymin": 30.29, "ymax": 30.29} for x in xs],
    })
    buf = io.BytesIO()
    pq.write_table(t, buf, row_group_size=2)
    return buf.getvalue()


def test_reader_fails_over_mid_file_and_filters_bbox(tmp_path):
    data = sample_parquet()
    hits = {"s3": 0, "blob": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        host, url = req.url.host, str(req.url)
        if host == S3.host:
            hits["s3"] += 1
            if "list-type" in url:
                return httpx.Response(200, content=s3_list(url))
            raise httpx.ConnectError("simulated S3 outage during file read")
        if host == BLOB.host:
            hits["blob"] += 1
            rng = req.headers["range"][6:].split("-")
            a, b = int(rng[0]), int(rng[1])
            chunk = data[a:b + 1]
            return httpx.Response(206, content=chunk, headers={"Content-Range": f"bytes {a}-{a + len(chunk) - 1}/{len(data)}"})
        raise AssertionError(f"unexpected host {host}")

    s = make_settings(tmp_path)
    store = NetStore("sqlite://")
    with InfraClient(REAL, s, CostGuard(REAL, s), transport=httpx.MockTransport(handler)) as c:
        table, stats = OvertureReader(REAL, c, store).read_bbox(BBox(-81.41, 30.27, -81.38, 30.31))
    assert sorted(table.column("id").to_pylist()) == ["a", "b", "c"]
    assert stats.listing_service == "overture_s3" and stats.services_used == {"overture_azure_blob"}
    assert hits["blob"] > 0
    assert any(not e.ok for e in store.recent_events("overture_s3"))
    assert store.last_successful_download("overture_azure_blob")[0] is not None
