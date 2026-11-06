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
* sales filters (M2) read the local CRM only and need its context (``crm``, ``crm_org``, ``crm_user``), which the
  API sets from the local profile. ``sales=new`` includes businesses nobody has touched yet (no CRM row).
  Follow-up periods use the database server's clock (the same machine in the Friend Edition):
  ``overdue`` before today, ``today``, ``week`` today through the next 6 days, ``any`` / ``none`` set or not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.normalize.core import name_norm

WEBSITE_STATUSES = ("listed", "social_only", "none_recorded", "unknown")
OPERATING_STATUSES = ("open", "closed", "unknown")
QUALIFICATIONS = ("provisionally_qualified", "needs_research", "not_qualified", "unscored")
SORTS = ("lps", "bos", "name", "follow_up")
SALES_STATUSES = ("new", "researching", "contacted", "follow_up", "interested", "meeting", "proposal", "won", "lost",
                  "not_a_fit")   # same values as app.db.models.CRM_STATUSES (checked by a test)
CLAIM_FILTERS = ("mine", "others", "unclaimed", "claimed")
FOLLOW_UP_FILTERS = ("overdue", "today", "week", "any", "none")
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
# Added after FROM_SQL only when the CRM tables exist (ProspectFilter.crm); at most one row per business.
CRM_FROM_SQL = "LEFT JOIN crm_prospect cp ON cp.location_id = l.id AND cp.organization_id = CAST(:crm_org AS uuid)\n"
_DAY = "date_trunc('day', now())"
FOLLOW_UP_SQL = {
    "overdue": f"cp.follow_up_at < {_DAY}",
    "today": f"(cp.follow_up_at >= {_DAY} AND cp.follow_up_at < {_DAY} + interval '1 day')",
    "week": f"(cp.follow_up_at >= {_DAY} AND cp.follow_up_at < {_DAY} + interval '7 days')",
    "any": "cp.follow_up_at IS NOT NULL",
    "none": "cp.follow_up_at IS NULL",
}
CLAIM_SQL = {
    "mine": "cp.owner_user_id = CAST(:crm_user AS uuid)",
    "others": "(cp.owner_user_id IS NOT NULL AND cp.owner_user_id IS DISTINCT FROM CAST(:crm_user AS uuid))",
    "unclaimed": "cp.owner_user_id IS NULL",
    "claimed": "cp.owner_user_id IS NOT NULL",
}

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
    "follow_up": "cp.follow_up_at ASC NULLS LAST, p.value DESC NULLS LAST, l.id",
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
    fips: list[str] = field(default_factory=list)          # internal (CLI/legacy export): county FIPS codes
    sales: list[str] = field(default_factory=list)         # M2: CRM pipeline status ('new' includes no CRM row)
    claim: str | None = None                               # mine / others / unclaimed / claimed
    follow_up: str | None = None                           # overdue / today / week / any / none
    list_id: int | None = None                             # saved list or campaign
    crm: bool = False                                      # internal: the CRM tables exist
    crm_org: str | None = None                             # internal: local organization id (None before setup)
    crm_user: str | None = None                            # internal: local user id

    def __post_init__(self):
        _check(self.qualifications, QUALIFICATIONS, "qualification")
        _check(self.website, WEBSITE_STATUSES, "website")
        _check(self.operating, OPERATING_STATUSES, "operating")
        if self.sort not in SORTS:
            raise FilterError(f"sort: unknown value {self.sort!r}; allowed: {list(SORTS)}")
        _check(self.sales, SALES_STATUSES, "sales")
        if self.claim is not None:
            _check([self.claim], CLAIM_FILTERS, "claim")
        if self.follow_up is not None:
            _check([self.follow_up], FOLLOW_UP_FILTERS, "follow_up")
        if not self.crm and (self.sales or self.claim or self.follow_up or self.list_id is not None
                             or self.sort == "follow_up"):
            raise FilterError("the sales filters need a database upgrade (run Setup again)")
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
        if self.fips:
            conds.append("l.county_fips = ANY(CAST(:f_fips AS text[]))")
            params["f_fips"] = list(self.fips)
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
        if self.crm:
            params.update(crm_org=self.crm_org, crm_user=self.crm_user)   # None before setup: nothing is joined
            if self.sales:
                parts = []
                named = [v for v in self.sales if v != "new"]
                if named:
                    parts.append("cp.status = ANY(CAST(:f_sales AS text[]))")
                    params["f_sales"] = named
                if "new" in self.sales:
                    parts.append("(cp.status IS NULL OR cp.status = 'new')")
                conds.append("(" + " OR ".join(parts) + ")")
            if self.claim:
                conds.append(CLAIM_SQL[self.claim])
            if self.follow_up:
                conds.append(FOLLOW_UP_SQL[self.follow_up])
            if self.list_id is not None:
                conds.append("EXISTS (SELECT 1 FROM crm_list_item li JOIN crm_list cl ON cl.id = li.list_id "
                             "WHERE li.location_id = l.id AND li.list_id = :f_list "
                             "AND cl.organization_id = CAST(:crm_org AS uuid))")
                params["f_list"] = int(self.list_id)
        return " AND ".join(conds), params

    def order_by(self) -> str:
        return ORDER_SQL[self.sort]

    def from_sql(self) -> str:
        return FROM_SQL + (CRM_FROM_SQL if self.crm else "")


LIST_COLUMNS = f"""
SELECT l.id, l.display_name AS name, coalesce(l.industry_profile, 'unclassified') AS industry, a.key AS county,
  l.operating_status, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, b.value AS bos, p.value AS lps, b.stage,
  b.evidence_points, q.status AS qualification,
  {_INDEPENDENT} AS website_listed,
  {WEBSITE_STATUS_SELECT} AS website_status,
  {PHONE_SQL} AS has_phone,
  {EMAIL_SQL} AS has_email
"""
CRM_LIST_COLUMNS = """,
  coalesce(cp.status, 'new') AS sales_status, cp.follow_up_at, cp.next_action,
  coalesce(cp.owner_user_id = CAST(:crm_user AS uuid), false) AS owned_by_me,
  (SELECT ou.initials FROM crm_user ou WHERE ou.id = cp.owner_user_id) AS owner_initials
"""


def list_query(f: ProspectFilter, limit: int, offset: int) -> tuple[str, dict]:
    """One page of prospects.

    The page's ids are chosen first using only the filter and the sort keys; the per-row EXISTS columns
    (website status, phone, email) are then computed for those rows only. Computing them for every row
    skipped by OFFSET made deep pages ~6x slower (1.25 s vs 0.2 s at page 400 of 100k rows, S2b measurement).
    """
    where, params = f.where()
    order, frm = f.order_by(), f.from_sql()
    cols = LIST_COLUMNS + (CRM_LIST_COLUMNS if f.crm else "")
    sql = (f"WITH page AS MATERIALIZED (SELECT l.id AS pid {frm}WHERE {where}\n"
           f"ORDER BY {order}\nLIMIT :limit OFFSET :offset)\n"
           f"{cols}{frm.replace('FROM location l', 'FROM page JOIN location l ON l.id = page.pid', 1)}"
           f"ORDER BY {order}")
    return sql, {**params, "limit": int(limit), "offset": int(offset)}


def summary_query(f: ProspectFilter) -> tuple[str, dict]:
    """Totals: matching businesses, with and without coordinates, and per qualification."""
    where, params = f.where()
    sql = (f"SELECT coalesce(q.status, 'unscored') AS qualification, count(*) AS n, "
           f"count(*) FILTER (WHERE l.geom IS NOT NULL) AS with_coordinates "
           f"{f.from_sql()}WHERE {where}\nGROUP BY 1")
    return sql, params


def count_query(f: ProspectFilter) -> tuple[str, dict]:
    where, params = f.where()
    return f"SELECT count(*) {f.from_sql()}WHERE {where}", params


def points_query(f: ProspectFilter, cap: int = MAX_POINTS) -> tuple[str, dict]:
    """Mappable businesses only (cap + 1 rows are fetched so truncation can be detected)."""
    where, params = f.where()
    sql = (f"SELECT l.id, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, q.status AS qualification "
           f"{f.from_sql()}WHERE {where} AND l.geom IS NOT NULL\nORDER BY {f.order_by()}\nLIMIT :cap")
    return sql, {**params, "cap": int(cap) + 1}
