"""Target areas from Census cartographic boundary files (approved D1: Duval, St. Johns, Clay)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import MultiPolygon, shape
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import Area
from app.net.http import InfraClient
from app.sources.common import download_checked, ensure_source, register_file

CENSUS_SOURCE = {"name": "US Census cartographic boundaries", "licence": "Public domain (US Government)",
                 "attribution": "U.S. Census Bureau"}


@dataclass
class AreaLoadResult:
    loaded: list[str]
    targets: list[str]
    boundary_sha256: str
    reused_download: bool


def _read_counties(zip_path: Path, state_fips: str, id_field: str, name_field: str):
    with shapefile.Reader(str(zip_path)) as r:
        fields = [f[0] for f in r.fields[1:]]
        for sr in r.iterShapeRecords():
            rec = dict(zip(fields, sr.record))
            if str(rec.get("STATEFP")) != state_fips:
                continue
            geom = shape(sr.shape.__geo_interface__)
            if geom.geom_type == "Polygon":
                geom = MultiPolygon([geom])
            yield str(rec[id_field]), str(rec[name_field]), geom


def load_counties(s: Session, client: InfraClient, data_dir: Path, areas_cfg: dict, state_fips: str = "12") -> AreaLoadResult:
    """Load every county of the state (for county attribution) and mark configured targets."""
    b = areas_cfg["boundaries"]["county"]
    dest = data_dir / "boundaries" / Path(b["url"]).name
    dl = download_checked(client, b["url"], dest)
    src = ensure_source(s, "census_boundaries", CENSUS_SOURCE)
    sf = register_file(s, src, kind="boundary_county", remote_path=b["url"], dl=dl, status="imported")
    targets = {t["geoid"]: t for t in areas_cfg["targets"] if t["kind"] == "county"}
    loaded = []
    for geoid, name, geom in _read_counties(dl.path, state_fips, b["id_field"], b["name_field"]):
        t = targets.get(geoid)
        key = t["key"] if t else f"county_{geoid}"
        row = s.execute(select(Area).where(Area.key == key)).scalar_one_or_none()
        wkt = f"SRID=4326;{geom.wkt}"
        if row is None:
            row = Area(key=key, name=t["name"] if t else f"{name}, FL", kind="county", spec={"geoid": geoid}, geom=wkt,
                       is_target=bool(t), enabled=bool(t and t.get("enabled", True)), source_file_id=sf.id)
            s.add(row)
        else:  # idempotent refresh
            row.geom, row.source_file_id = wkt, sf.id
            row.is_target, row.enabled = bool(t), bool(t and t.get("enabled", True))
        loaded.append(key)
    s.flush()
    missing = set(targets) - {a.spec.get("geoid") for a in s.execute(select(Area).where(Area.kind == "county")).scalars()}
    if missing:
        raise ValueError(f"target counties not found in boundary file: {sorted(missing)}")
    return AreaLoadResult(loaded, [t["key"] for t in targets.values()], dl.sha256, dl.reused)


def county_fips_at(s: Session, lon: float, lat: float) -> str | None:
    row = s.execute(text("SELECT spec->>'geoid' FROM area WHERE kind='county' AND "
                         "ST_Contains(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) LIMIT 1"),
                    {"lon": lon, "lat": lat}).first()
    return row[0] if row else None


def target_areas(s: Session, keys: list[str] | None = None) -> list[Area]:
    q = select(Area).where(Area.is_target.is_(True), Area.enabled.is_(True))
    if keys:
        q = q.where(Area.key.in_(keys))
    return list(s.execute(q).scalars())


def targets_bbox(s: Session, keys: list[str] | None = None) -> tuple[float, float, float, float]:
    ids = [a.id for a in target_areas(s, keys)]
    if not ids:
        raise ValueError("no enabled target areas")
    box = s.execute(select(func.ST_XMin(func.ST_Extent(Area.geom)), func.ST_YMin(func.ST_Extent(Area.geom)),
                           func.ST_XMax(func.ST_Extent(Area.geom)), func.ST_YMax(func.ST_Extent(Area.geom)))
                    .where(Area.id.in_(ids))).one()
    return tuple(float(v) for v in box)  # type: ignore[return-value]
