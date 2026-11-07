"""Business database schema (Phase 1 subset of docs/DATA_MODEL.md).

Conventions (DATA_MODEL.md §0): bigint identity keys with natural-key uniques, UTC timestamptz,
EPSG:4326 geometry, append-only history tables, soft retraction, actor columns.
Network diagnostics tables are NOT here - they live in their own store (app/net/store.py).
"""
from __future__ import annotations

from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ----------------------------------------------------------------------------- configuration (F1)
class ConfigVersion(Base):
    """Versioned editable settings. Packaged YAML defaults seed version 1 of each kind."""
    __tablename__ = "config_version"
    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(40), index=True)          # areas | keywords | industries | scoring | sources
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))                     # proposed | active | superseded | rejected
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("config_version.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = _now()
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("kind", "version"),)


# ----------------------------------------------------------------------------- sources & provenance
class Source(Base):
    __tablename__ = "source"
    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    cost_class: Mapped[str] = mapped_column(String(16), default="free")
    licence: Mapped[str | None] = mapped_column(String(120))
    attribution: Mapped[str | None] = mapped_column(Text)
    reliability: Mapped[dict] = mapped_column(JSONB, default=dict)      # field group -> 0..1
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SourceFile(Base):
    __tablename__ = "source_file"
    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))                      # cor | cor_event | fic | fic_event | overture_snapshot ...
    logical_date: Mapped[date | None] = mapped_column(Date)
    remote_path: Mapped[str] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    remote_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str | None] = mapped_column(String(64))
    local_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))                     # downloaded | imported | failed | superseded
    records: Mapped[int] = mapped_column(Integer, default=0)
    quarantined: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _now()
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[str | None] = mapped_column(String(32))     # importer rules that produced `records`
    # one row per remote version: the same content republished with a new Last-Modified is a new row
    __table_args__ = (UniqueConstraint("source_id", "remote_path", "sha256", "remote_last_modified",
                                       name="uq_source_file_version", postgresql_nulls_not_distinct=True),)


class SourceDateLedger(Base):
    __tablename__ = "source_date_ledger"
    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    kind: Mapped[str] = mapped_column(String(30))
    day: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(24))   # verified | not_yet_published | absent_holiday | absent_unexplained | failed
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("source_file.id"))
    detail: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("source_id", "kind", "day"),)


class SourceRecord(Base):
    """Append-only raw record. A changed payload for the same external id is a new row."""
    __tablename__ = "source_record"
    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), index=True)
    record_kind: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("source_file.id"))
    line_no: Mapped[int | None] = mapped_column(Integer)
    raw_line: Mapped[bytes | None] = mapped_column(LargeBinary)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    licence_tag: Mapped[str | None] = mapped_column(String(60))
    fetched_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("source_id", "record_kind", "external_id", "payload_hash"),
                      Index("ix_source_record_ext", "source_id", "record_kind", "external_id"))


class QuarantineRecord(Base):
    __tablename__ = "quarantine_record"
    id: Mapped[int] = _pk()
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    raw_line: Mapped[bytes] = mapped_column(LargeBinary)
    reason: Mapped[str] = mapped_column(Text)
    field: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("source_file_id", "line_no"),)


class Fact(Base):
    """Field-level provenance. Append-only: a new value supersedes the old one (superseded_at)."""
    __tablename__ = "fact"
    id: Mapped[int] = _pk()
    subject_type: Mapped[str] = mapped_column(String(20))              # location | organization
    subject_id: Mapped[int] = mapped_column(BigInteger)
    field: Mapped[str] = mapped_column(String(40))
    value: Mapped[dict] = mapped_column(JSONB)
    value_norm: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    source_key: Mapped[str] = mapped_column(String(60))
    provenance: Mapped[str] = mapped_column(String(16))                 # directory | website | registry | inferred | manual
    verification_state: Mapped[str] = mapped_column(String(16), default="ingested")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = _now()
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ttl_days: Mapped[int | None] = mapped_column(Integer)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_fact_subject", "subject_type", "subject_id", "field"),
                      UniqueConstraint("subject_type", "subject_id", "field", "value_norm", "source_record_id"))


# ----------------------------------------------------------------------------- geography & runs
class Area(Base):
    __tablename__ = "area"
    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))                       # county | place | zcta | radius | polygon | state | multi
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=True))
    is_target: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("source_file.id"))
    created_at: Mapped[datetime] = _now()


class DiscoveryRun(Base):
    __tablename__ = "discovery_run"
    id: Mapped[int] = _pk()
    area_keys: Mapped[list[str]] = mapped_column(ARRAY(String))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16))                     # running | completed | partial | failed
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Coverage(Base):
    __tablename__ = "coverage"
    id: Mapped[int] = _pk()
    run_id: Mapped[int] = mapped_column(ForeignKey("discovery_run.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(60))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("area.id"))
    state: Mapped[str] = mapped_column(String(16))                      # complete | partial | failed | not_run | unsupported
    reason: Mapped[str | None] = mapped_column(Text)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()


class GeocodeCache(Base):
    __tablename__ = "geocode_cache"
    id: Mapped[int] = _pk()
    address_key: Mapped[str] = mapped_column(Text, unique=True)
    matched: Mapped[bool] = mapped_column(Boolean)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    county_fips: Mapped[str | None] = mapped_column(String(5))
    quality: Mapped[str | None] = mapped_column(String(20))
    response: Mapped[dict] = mapped_column(JSONB, default=dict)
    geocoded_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- organizations
class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[int] = _pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    document_number: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(Text)
    name_norm: Mapped[str] = mapped_column(Text, index=True)
    filing_kind: Mapped[str] = mapped_column(String(30))
    filing_type_code: Mapped[str | None] = mapped_column(String(15))
    status: Mapped[str] = mapped_column(String(16))                     # active | inactive | dissolved | unknown
    registration_date: Mapped[date | None] = mapped_column(Date)
    status_changed_at: Mapped[date | None] = mapped_column(Date)
    county_hint: Mapped[str | None] = mapped_column(String(40))
    latest_source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("source_id", "document_number"),
                      Index("ix_organization_name_trgm", "name_norm", postgresql_using="gin", postgresql_ops={"name_norm": "gin_trgm_ops"}))


class OrganizationLink(Base):
    __tablename__ = "organization_link"
    id: Mapped[int] = _pk()
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    to_org_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"), index=True)
    to_document_number: Mapped[str] = mapped_column(String(20))         # kept so links resolve when the owner arrives later
    kind: Mapped[str] = mapped_column(String(30))                       # dba_owned_by | successor_of | same_entity_as
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("from_org_id", "to_document_number", "kind"),)


class PersonRole(Base):
    """Officers / owners / registered agents. Used for linking only; never surfaced as lead contacts."""
    __tablename__ = "person_role"
    id: Mapped[int] = _pk()
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    role: Mapped[str] = mapped_column(String(30))                       # officer | owner | registered_agent
    title: Mapped[str | None] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(Text)
    is_entity: Mapped[bool] = mapped_column(Boolean, default=False)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    __table_args__ = (UniqueConstraint("organization_id", "role", "name", "source_record_id"),)


class Address(Base):
    __tablename__ = "address"
    id: Mapped[int] = _pk()
    owner_type: Mapped[str] = mapped_column(String(20))                 # organization | location
    owner_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(20))                       # principal | mailing | registered_agent | owner | listing
    line1: Mapped[str | None] = mapped_column(Text)
    line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(2))
    zip5: Mapped[str | None] = mapped_column(String(5), index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    norm_key: Mapped[str | None] = mapped_column(Text, index=True)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=True), nullable=True)
    county_fips: Mapped[str | None] = mapped_column(String(5))
    geocode_method: Mapped[str | None] = mapped_column(String(20))      # census_match | zip_centroid | source_point
    is_operating_location: Mapped[bool] = mapped_column(Boolean, default=True)
    not_operating_reason: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    __table_args__ = (Index("ix_address_owner", "owner_type", "owner_id"),
                      UniqueConstraint("owner_type", "owner_id", "role", "norm_key"))


# ----------------------------------------------------------------------------- locations (prospect unit)
class Location(Base):
    __tablename__ = "location"
    id: Mapped[int] = _pk()
    display_name: Mapped[str] = mapped_column(Text)
    name_norm: Mapped[str] = mapped_column(Text, index=True)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=True), nullable=True)
    county_fips: Mapped[str | None] = mapped_column(String(5), index=True)
    category: Mapped[str | None] = mapped_column(Text)
    industry_profile: Mapped[str | None] = mapped_column(String(60))
    chain_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    brand_wikidata: Mapped[str | None] = mapped_column(String(20))
    operating_status: Mapped[str] = mapped_column(String(24), default="unknown")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("location.id"))
    completeness: Mapped[dict] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ix_location_name_trgm", "name_norm", postgresql_using="gin", postgresql_ops={"name_norm": "gin_trgm_ops"}),)


class LocationAlias(Base):
    __tablename__ = "location_alias"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    alias: Mapped[str] = mapped_column(Text)
    alias_norm: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(String(20))                       # listing | legal | dba | common
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    __table_args__ = (UniqueConstraint("location_id", "alias_norm", "kind"),
                      Index("ix_location_alias_trgm", "alias_norm", postgresql_using="gin", postgresql_ops={"alias_norm": "gin_trgm_ops"}))


class LocationSourceLink(Base):
    __tablename__ = "location_source_link"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(200))
    match_method: Mapped[str] = mapped_column(String(40))
    match_score: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    linked_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ix_lsl_ext", "source_key", "external_id"),)


class LocationOrganization(Base):
    __tablename__ = "location_organization"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))                       # legal_owner | trade_name | franchise_parent
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("location_id", "organization_id", "role"),)


class OnlineProfile(Base):
    __tablename__ = "online_profile"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    url_norm: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(20))                   # website | facebook | instagram | ... | google_listing | yelp
    platform_kind: Mapped[str] = mapped_column(String(16))              # independent | social | link_hub | listing
    source_key: Mapped[str] = mapped_column(String(60))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    verification_state: Mapped[str] = mapped_column(String(16), default="ingested")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("location_id", "url_norm"),)


class ContactPoint(Base):
    __tablename__ = "contact_point"
    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(10))                       # phone | email | postal
    value_norm: Mapped[str] = mapped_column(Text)
    display: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("kind", "value_norm"),)


class LocationContact(Base):
    __tablename__ = "location_contact"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    contact_point_id: Mapped[int] = mapped_column(ForeignKey("contact_point.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(60))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    verification_state: Mapped[str] = mapped_column(String(16), default="ingested")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("location_id", "contact_point_id", "source_key"),)


class ReviewQueue(Base):
    __tablename__ = "review_queue"
    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(30))                       # location_merge | org_location_link
    subject_a: Mapped[int] = mapped_column(BigInteger)
    subject_b: Mapped[int] = mapped_column(BigInteger)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="open")     # open | accepted | rejected
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("kind", "subject_a", "subject_b"),)


class MergeEvent(Base):
    __tablename__ = "merge_event"
    id: Mapped[int] = _pk()
    action: Mapped[str] = mapped_column(String(10))                     # merge | unmerge
    survivor_id: Mapped[int] = mapped_column(BigInteger)
    merged_id: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    moved: Mapped[dict] = mapped_column(JSONB, default=dict)            # ids of rows re-pointed, for exact reversal
    actor: Mapped[str] = mapped_column(String(80), default="system")
    reverts_event_id: Mapped[int | None] = mapped_column(ForeignKey("merge_event.id"))
    at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- targeting & scoring (Phase 1 M2)
class KeywordHit(Base):
    __tablename__ = "keyword_hit"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    group: Mapped[str] = mapped_column(String(40))
    polarity: Mapped[str] = mapped_column(String(8))                   # positive | negative
    term: Mapped[str] = mapped_column(String(80))
    matched_text: Mapped[str] = mapped_column(Text)
    field: Mapped[str] = mapped_column(String(30))                      # name | alias | category
    effect: Mapped[float] = mapped_column(Float)
    neutralized: Mapped[bool] = mapped_column(Boolean, default=False)
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_version.id"))
    computed_at: Mapped[datetime] = _now()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Score(Base):
    """Append-only. The current score per (location, kind) has superseded_at IS NULL."""
    __tablename__ = "score"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))                       # bos | lps | wqs
    stage: Mapped[str] = mapped_column(String(12))                     # provisional | final
    value: Mapped[float | None] = mapped_column(Float)
    pre_cap_value: Mapped[float | None] = mapped_column(Float)
    caps_applied: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_points: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    config_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    inputs_hash: Mapped[str | None] = mapped_column(String(64))       # unchanged inputs -> no new row
    computed_at: Mapped[datetime] = _now()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_score_current", "location_id", "kind", "superseded_at"),)


class ScoreFactor(Base):
    __tablename__ = "score_factor"
    id: Mapped[int] = _pk()
    score_id: Mapped[int] = mapped_column(ForeignKey("score.id"), index=True)
    component: Mapped[str] = mapped_column(String(30))
    factor: Mapped[str] = mapped_column(String(60))
    effect: Mapped[float] = mapped_column(Float)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    provenance: Mapped[str | None] = mapped_column(String(16))


class Qualification(Base):
    """Separate from ranking (F8): a gate with reasons. Append-only like scores."""
    __tablename__ = "qualification"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))   # qualified | provisionally_qualified | needs_research | not_qualified
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    stage: Mapped[str] = mapped_column(String(12))
    computed_at: Mapped[datetime] = _now()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------------------- local CRM (M2, migration 0006)
# Stable identities from the start: organizations and users have UUIDs so a later optional sync service can
# merge independent installations without renumbering. The first version has one local organization and one
# local user. Sales data never changes business facts: CRM rows only reference location ids.
from sqlalchemy import CheckConstraint  # noqa: E402
from sqlalchemy.dialects.postgresql import UUID  # noqa: E402

CRM_STATUSES = ("new", "researching", "contacted", "follow_up", "interested", "meeting", "proposal", "won", "lost",
                "not_a_fit")
CRM_ACTIVITY_KINDS = ("note", "call", "email", "meeting", "visit", "claim", "release", "reassign", "status",
                      "next_action", "follow_up", "list_add", "list_remove")


class CrmOrganization(Base):
    __tablename__ = "crm_organization"
    id = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), server_default="local")          # local | synced (future)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ux_crm_organization_one_local", "kind", unique=True, postgresql_where="kind = 'local'"),)


class CrmUser(Base):
    __tablename__ = "crm_user"
    id = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid())
    organization_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_organization.id"), index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text)
    initials: Mapped[str] = mapped_column(String(4))                                  # shown, never an identity
    email: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), server_default="owner")            # owner | admin | member
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_crm_user_role"),)


class CrmProspect(Base):
    """One row per (organization, business): owner (claim), pipeline status, next action, follow-up date.

    UNIQUE(organization_id, location_id) makes a second active claim impossible; claims are conditional
    updates (owner_user_id IS NULL). `version` gives optimistic concurrency for edits.
    """
    __tablename__ = "crm_prospect"
    id: Mapped[int] = _pk()
    organization_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_organization.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), index=True)
    owner_user_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_user.id"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), server_default="new")
    next_action: Mapped[str | None] = mapped_column(Text)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("organization_id", "location_id", name="ux_crm_prospect_org_location"),
                      CheckConstraint("status IN " + str(CRM_STATUSES), name="ck_crm_prospect_status"),
                      Index("ix_crm_prospect_owner", "organization_id", "owner_user_id"),
                      Index("ix_crm_prospect_follow_up", "organization_id", "follow_up_at"),
                      Index("ix_crm_prospect_status", "organization_id", "status"))


class CrmActivity(Base):
    """Append-only sales history: notes, calls, emails, meetings, and every claim/status/follow-up change."""
    __tablename__ = "crm_activity"
    id: Mapped[int] = _pk()
    organization_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_organization.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"))
    user_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_user.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    occurred_at: Mapped[datetime] = _now()
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("kind IN " + str(CRM_ACTIVITY_KINDS), name="ck_crm_activity_kind"),
                      Index("ix_crm_activity_location", "organization_id", "location_id", "occurred_at"),
                      Index("ix_crm_activity_user", "organization_id", "user_id", "occurred_at"))


class CrmList(Base):
    """Saved prospect lists and campaigns."""
    __tablename__ = "crm_list"
    id: Mapped[int] = _pk()
    organization_id = mapped_column(UUID(as_uuid=False), ForeignKey("crm_organization.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), server_default="list")             # list | campaign
    description: Mapped[str | None] = mapped_column(Text)
    created_by = mapped_column(UUID(as_uuid=False), ForeignKey("crm_user.id"))
    archived: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("organization_id", "name", name="ux_crm_list_org_name"),
                      CheckConstraint("kind IN ('list', 'campaign')", name="ck_crm_list_kind"))


class CrmListItem(Base):
    __tablename__ = "crm_list_item"
    list_id: Mapped[int] = mapped_column(ForeignKey("crm_list.id", ondelete="CASCADE"), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), primary_key=True, index=True)
    added_by = mapped_column(UUID(as_uuid=False), ForeignKey("crm_user.id"))
    added_at: Mapped[datetime] = _now()


# ---------------------------------------------------------------- M3: website audits (migration 0007)
AUDIT_STATUSES = ("running", "completed", "partial", "failed", "blocked", "robots_disallowed", "robots_unavailable",
                  "no_website", "interrupted")


class WebsiteAudit(Base):
    """One website audit of one business. Pages, findings, artifacts and recommendations hang off it; the WQS also
    goes into `score` (kind 'wqs') with per-dimension factors. Screenshots stay in the managed data folder."""
    __tablename__ = "website_audit"
    id: Mapped[int] = _pk()
    location_id: Mapped[int] = mapped_column(ForeignKey("location.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    start_url: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    robots: Mapped[str | None] = mapped_column(String(16))
    crawl_delay_s: Mapped[float | None] = mapped_column(Float)
    pages_crawled: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    requested_by: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wqs: Mapped[float | None] = mapped_column(Float)
    wqs_confidence: Mapped[float | None] = mapped_column(Float)
    wqs_version: Mapped[str | None] = mapped_column(String(20))
    wqs_status: Mapped[str | None] = mapped_column(String(20))
    dimensions: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    explanation: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    observations: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)   # domain history, timing
    score_id: Mapped[int | None] = mapped_column(ForeignKey("score.id"))
    __table_args__ = (Index("ix_website_audit_location", "location_id", "started_at"),
                      CheckConstraint("status IN " + str(AUDIT_STATUSES), name="ck_website_audit_status"))


class WebsiteAuditPage(Base):
    __tablename__ = "website_audit_page"
    id: Mapped[int] = _pk()
    audit_id: Mapped[int] = mapped_column(ForeignKey("website_audit.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    html_bytes: Mapped[int | None] = mapped_column(Integer)
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    links: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    facts: Mapped[dict | None] = mapped_column(JSONB)
    axe: Mapped[list | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("audit_id", "seq", name="ux_website_audit_page_seq"),)


class WebsiteAuditArtifact(Base):
    """A file in the managed data folder (path relative to it), with checksum and size; never the bytes."""
    __tablename__ = "website_audit_artifact"
    id: Mapped[int] = _pk()
    audit_id: Mapped[int] = mapped_column(ForeignKey("website_audit.id", ondelete="CASCADE"), nullable=False, index=True)
    page_id: Mapped[int | None] = mapped_column(ForeignKey("website_audit_page.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _now()


class WebsiteAuditFinding(Base):
    __tablename__ = "website_audit_finding"
    id: Mapped[int] = _pk()
    audit_id: Mapped[int] = mapped_column(ForeignKey("website_audit.id", ondelete="CASCADE"), nullable=False, index=True)
    criterion: Mapped[str] = mapped_column(String(60), nullable=False)
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)          # measured | heuristic
    passed: Mapped[bool | None] = mapped_column(Boolean)                    # NULL = not assessed (never "failed")
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    measured_at: Mapped[datetime] = _now()


class WebsiteAuditRecommendation(Base):
    __tablename__ = "website_audit_recommendation"
    id: Mapped[int] = _pk()
    audit_id: Mapped[int] = mapped_column(ForeignKey("website_audit.id", ondelete="CASCADE"), nullable=False, index=True)
    criterion: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)       # high | medium | low
    priority: Mapped[int] = mapped_column(Integer, nullable=False)          # 1 = first
    title: Mapped[str] = mapped_column(Text, nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    why: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    benchmark: Mapped[str | None] = mapped_column(Text)


class WebsiteBenchmark(Base):
    """Peer comparison as computed for one audit (snapshot: the peer set can change later)."""
    __tablename__ = "website_benchmark"
    id: Mapped[int] = _pk()
    audit_id: Mapped[int] = mapped_column(ForeignKey("website_audit.id", ondelete="CASCADE"), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)          # county | region | none
    niche: Mapped[str | None] = mapped_column(String(40))
    area: Mapped[str | None] = mapped_column(Text)
    peers: Mapped[int] = mapped_column(Integer, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    computed_at: Mapped[datetime] = _now()


class CrmSavedSearch(Base):
    """A named prospect search: all filters, the Search Area (center, radius in metres, display unit), sort and view."""
    __tablename__ = "crm_saved_search"
    id: Mapped[int] = _pk()
    organization_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("crm_organization.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    created_by: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("crm_user.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("organization_id", "name", name="ux_crm_saved_search_org_name"),)
