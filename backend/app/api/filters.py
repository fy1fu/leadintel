"""One definition of prospect filtering, sorting and paging (M1 S2a).

Every view that lists or counts prospects - the list, the summary counts, the map points and (from S3) the
exports - builds its SQL from ``ProspectFilter`` so they always agree. Values are only ever bound parameters;
column names and sort orders come from fixed whitelists, never from the request.

Meanings are deliberately literal:

* website status follows the scoring presence logic (``app.scoring.model._presence``):
    ``listed``       - at least one source lists an independent website (listed, not checked)
    ``social_only``  - no independent website listed, but a social or link-hub page is
    ``none_recorded``- no qualifying website recorded in the sources checked so far (NOT proof of no website)
    ``unknown``      - not enough source data to say
* ``phone`` / ``email`` mean *listed* in a source (active contact link); nothing here means verified.
* operating status: ``open`` = open; ``closed`` = permanently or temporarily closed; ``unknown`` = anything else,
  including no value. Unknown is never folded into open.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.normalize.core import name_norm

WEBSITE_STATUSES = ("listed", "social_only", "none_recorded", "unknown")
OPERATING_STATUSES = ("open", "closed", "unknown")
QUALIFICATIONS = ("provisionally_qualified", "needs_research", "not_qualified", "unscored")
SORTS = ("lps", "bos", "name")
MAX_PAGE_SIZE = 200
MAX_LEGACY_LIMIT = 5000
MAX_POINTS = 120_000

# Fixed FROM clause shared by every prospect query (same joins as the original list query).
FROM_SQL = """
FROM location l
JOIN area a ON a.kind = 'county' AND a.is_target AND a.spec->>'geoid' = l.county_fips
LEFT JOIN score b ON b.location_id = l.id AND b.kind = 'bos' AND b.superseded_at IS NULL
LEFT JOIN score p ON p.location_id = l.id AND p.kind = 'lps' AND p.superseded_at IS NULL
LEFT JOIN qualification q ON q.location_id = l.id AND q.superseded_at IS NULL
"""

_INDEPENDENT = "EXISTS (SELECT 1 FROM online_profile o WHERE o.location_id = l.id AND o.platform_kind = 'independent')"
_SOCIAL = ("EXISTS (SELECT 1 FROM online_profile o WHERE o.location_id = l.id "
           "AND o.platform_kind IN ('social', 'link_hub'))")
_ANY_SOURCE = "EXISTS (SELECT 1 FROM location_source_link k WHERE k.location_id = l.id AND k.active)"
WEBSITE_STATUS_SQL = {
    "listed": _INDEPENDENT,
    "social_only": f"(NOT {_INDEPENDENT} AND {_SOCIAL})",
    "none_recorded": f"(NOT {_INDEPENDENT} AND NOT {_SOCIAL} AND {_ANY_SOURCE})",
    "unknown": f"(NOT {_INDEPENDENT} AND NOT {_SOCIAL} AND NOT {_ANY_SOURCE})",
}
WEBSITE_STATUS_SELECT = ("CASE WHEN {listed} THEN 'listed' WHEN {social} THEN 'social_only' "
                         "WHEN {src} THEN 'none_recorded' ELSE 'unknown' END").format(
    listed=_INDEPENDENT, social=_SOCIAL, src=_ANY_SOURCE)


def _contact(kind: str) -> str:
    return ("EXISTS (SELECT 1 FROM location_contact lc JOIN contact_point c ON c.id = lc.contact_point_id "
            f"WHERE lc.location_id = l.id AND lc.active AND c.kind = '{kind}')")


PHONE_SQL, EMAIL_SQL = _contact("phone"), _contact("email")
OPERATING_SQL = {
    "open": "l.operating_status = 'open'",
    "closed": "l.operating_status IN ('permanently_closed', 'temporarily_closed')",
    "unknown": "(l.operating_status IS NULL OR l.operating_status NOT IN ('open', 'permanently_closed', 'temporarily_closed'))",
}
ORDER_SQL = {
    "lps": "p.value DESC NULLS LAST, l.id",
    "bos": "b.value DESC NULLS LAST, l.id",
    "name": "lower(l.display_name), l.id",
}


class FilterError(ValueError):
    """Invalid filter input (reported to the client as HTTP 422)."""


def split_list(value: str | None) -> list[str]:
    """'a, b,,c' -> ['a', 'b', 'c']; None or '' -> [] (meaning: no restriction)."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _check(values: list[str], allowed: tuple[str, ...], name: str) -> list[str]:
    bad = [v for v in values if v not in allowed]
    if bad:
        raise FilterError(f"{name}: unknown value(s) {bad}; allowed: {list(allowed)}")
    return values


def _score(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not 0 <= value <= 100:
        raise FilterError(f"{name} must be between 0 and 100")
    return float(value)


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class ProspectFilter:
    counties: list[str] = field(default_factory=list)      # area keys, e.g. duval
    niches: list[str] = field(default_factory=list)        # industry profiles
    qualifications: list[str] = field(default_factory=list)
    q: str | None = None                                   # business-name search
    website: list[str] = field(default_factory=list)
    operating: list[str] = field(default_factory=list)
    phone: bool | None = None                              # True: phone listed; False: none listed; None: either
    email: bool | None = None
    bos_min: float | None = None
    bos_max: float | None = None
    lps_min: float | None = None
    lps_max: float | None = None
    sort: str = "lps"

    def __post_init__(self):
        _check(self.qualifications, QUALIFICATIONS, "qualification")
        _check(self.website, WEBSITE_STATUSES, "website")
        _check(self.operating, OPERATING_STATUSES, "operating")
        if self.sort not in SORTS:
            raise FilterError(f"sort: unknown value {self.sort!r}; allowed: {list(SORTS)}")
        for name in ("bos_min", "bos_max", "lps_min", "lps_max"):
            setattr(self, name, _score(getattr(self, name), name))
        for lo, hi in (("bos_min", "bos_max"), ("lps_min", "lps_max")):
            if getattr(self, lo) is not None and getattr(self, hi) is not None and getattr(self, lo) > getattr(self, hi):
                raise FilterError(f"{lo} is greater than {hi}")
        if self.q is not None:
            self.q = self.q.strip() or None
            if self.q and len(self.q) > 200:
                raise FilterError("q is longer than 200 characters")

    def where(self) -> tuple[str, dict]:
        """WHERE clause (without the keyword) and its bound parameters."""
        conds = ["l.merged_into_id IS NULL"]
        params: dict = {}
        if self.counties:
            conds.append("a.key = ANY(CAST(:f_counties AS text[]))")
            params["f_counties"] = list(self.counties)
        if self.niches:
            conds.append("l.industry_profile = ANY(CAST(:f_niches AS text[]))")
            params["f_niches"] = list(self.niches)
        if self.qualifications:
            parts = []
            named = [v for v in self.qualifications if v != "unscored"]
            if named:
                parts.append("q.status = ANY(CAST(:f_quals AS text[]))")
                params["f_quals"] = named
            if "unscored" in self.qualifications:
                parts.append("q.status IS NULL")
            conds.append("(" + " OR ".join(parts) + ")")
        if self.q:
            norm = name_norm(self.q)
            if norm:   # normalized name, served by the trigram index on location.name_norm
                conds.append("l.name_norm ILIKE :f_q ESCAPE '\\'")
                params["f_q"] = f"%{like_escape(norm)}%"
            else:      # e.g. only a legal suffix such as "LLC": search the displayed name instead
                conds.append("l.display_name ILIKE :f_q ESCAPE '\\'")
                params["f_q"] = f"%{like_escape(self.q)}%"
        if self.website:
            conds.append("(" + " OR ".join(WEBSITE_STATUS_SQL[w] for w in self.website) + ")")
        if self.operating:
            conds.append("(" + " OR ".join(OPERATING_SQL[o] for o in self.operating) + ")")
        if self.phone is not None:
            conds.append(PHONE_SQL if self.phone else f"NOT {PHONE_SQL}")
        if self.email is not None:
            conds.append(EMAIL_SQL if self.email else f"NOT {EMAIL_SQL}")
        for col, lo, hi in (("b.value", "bos_min", "bos_max"), ("p.value", "lps_min", "lps_max")):
            if getattr(self, lo) is not None:
                conds.append(f"{col} >= :f_{lo}")
                params[f"f_{lo}"] = getattr(self, lo)
            if getattr(self, hi) is not None:
                conds.append(f"{col} <= :f_{hi}")
                params[f"f_{hi}"] = getattr(self, hi)
        return " AND ".join(conds), params

    def order_by(self) -> str:
        return ORDER_SQL[self.sort]


LIST_COLUMNS = f"""
SELECT l.id, l.display_name AS name, coalesce(l.industry_profile, 'unclassified') AS industry, a.key AS county,
  l.operating_status, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, b.value AS bos, p.value AS lps, b.stage,
  b.evidence_points, q.status AS qualification,
  {_INDEPENDENT} AS website_listed,
  {WEBSITE_STATUS_SELECT} AS website_status,
  {PHONE_SQL} AS has_phone,
  {EMAIL_SQL} AS has_email
"""


def list_query(f: ProspectFilter, limit: int, offset: int) -> tuple[str, dict]:
    where, params = f.where()
    sql = f"{LIST_COLUMNS}{FROM_SQL}WHERE {where}\nORDER BY {f.order_by()}\nLIMIT :limit OFFSET :offset"
    return sql, {**params, "limit": int(limit), "offset": int(offset)}


def summary_query(f: ProspectFilter) -> tuple[str, dict]:
    """Totals: matching businesses, with and without coordinates, and per qualification."""
    where, params = f.where()
    sql = (f"SELECT coalesce(q.status, 'unscored') AS qualification, count(*) AS n, "
           f"count(*) FILTER (WHERE l.geom IS NOT NULL) AS with_coordinates "
           f"{FROM_SQL}WHERE {where}\nGROUP BY 1")
    return sql, params


def count_query(f: ProspectFilter) -> tuple[str, dict]:
    where, params = f.where()
    return f"SELECT count(*) {FROM_SQL}WHERE {where}", params


def points_query(f: ProspectFilter, cap: int = MAX_POINTS) -> tuple[str, dict]:
    """Mappable businesses only (cap + 1 rows are fetched so truncation can be detected)."""
    where, params = f.where()
    sql = (f"SELECT l.id, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, q.status AS qualification "
           f"{FROM_SQL}WHERE {where} AND l.geom IS NOT NULL\nORDER BY {f.order_by()}\nLIMIT :cap")
    return sql, {**params, "cap": int(cap) + 1}
