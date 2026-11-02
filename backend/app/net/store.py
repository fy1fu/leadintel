"""Persistence for probe results and runtime network events (SQLite by default, Postgres via env)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.net.probe import ProbeResult


class Base(DeclarativeBase):
    pass


class ProbeRow(Base):
    __tablename__ = "net_probe_result"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    service_key: Mapped[str] = mapped_column(String(80), index=True)
    probe_key: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    failure_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    via_proxy: Mapped[bool] = mapped_column(default=False)
    network_reached: Mapped[bool | None] = mapped_column(nullable=True)
    stages: Mapped[list] = mapped_column(JSON, default=list)


class NetEventRow(Base):
    """Runtime outcome of real work (downloads, listings) - not just diagnostics."""
    __tablename__ = "net_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_key: Mapped[str] = mapped_column(String(80), index=True)
    operation: Mapped[str] = mapped_column(String(40))      # download | list | query
    ok: Mapped[bool] = mapped_column()
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class NetStore:
    def __init__(self, url: str | None = None, engine: Engine | None = None):
        self.engine = engine or _make_engine(url or "sqlite://")
        Base.metadata.create_all(self.engine)

    # ---- writes -----------------------------------------------------------
    def save_probe_results(self, run_id: str, results: Iterable[ProbeResult]) -> None:
        with Session(self.engine) as s, s.begin():
            for r in results:
                d = r.to_dict()
                s.add(ProbeRow(run_id=run_id, started_at=r.started_at, **{k: d[k] for k in (
                    "service_key", "probe_key", "kind", "host", "port", "url", "duration_ms", "outcome",
                    "failure_stage", "failure_kind", "error", "http_status", "bytes_downloaded", "via_proxy",
                    "network_reached", "stages")}))

    def record_event(self, service_key: str, operation: str, ok: bool, *, url: str | None = None,
                     nbytes: int = 0, error: str | None = None) -> None:
        with Session(self.engine) as s, s.begin():
            s.add(NetEventRow(service_key=service_key, operation=operation, ok=ok, url=url, bytes=nbytes,
                              error=error, at=datetime.now(timezone.utc)))

    # ---- reads ------------------------------------------------------------
    def latest_run_id(self) -> str | None:
        with Session(self.engine) as s:
            row = s.execute(select(ProbeRow.run_id).order_by(ProbeRow.started_at.desc()).limit(1)).first()
            return row[0] if row else None

    def latest_results(self) -> dict[str, list[ProbeRow]]:
        """Most recent result of every (service, probe) pair, grouped by service."""
        with Session(self.engine) as s:
            sub = (select(ProbeRow.service_key, ProbeRow.probe_key, func.max(ProbeRow.id).label("mid"))
                   .group_by(ProbeRow.service_key, ProbeRow.probe_key).subquery())
            rows = s.execute(select(ProbeRow).join(sub, ProbeRow.id == sub.c.mid)).scalars().all()
            s.expunge_all()
        out: dict[str, list[ProbeRow]] = {}
        for r in rows:
            out.setdefault(r.service_key, []).append(r)
        return out

    def last_success(self, service_key: str) -> datetime | None:
        with Session(self.engine) as s:
            return s.execute(select(func.max(ProbeRow.started_at)).where(
                ProbeRow.service_key == service_key, ProbeRow.outcome == "success")).scalar()

    def last_successful_download(self, service_key: str) -> tuple[datetime | None, int]:
        """Latest successful byte transfer from diagnostics OR real runtime downloads."""
        with Session(self.engine) as s:
            p = s.execute(select(ProbeRow.started_at, ProbeRow.bytes_downloaded).where(
                ProbeRow.service_key == service_key, ProbeRow.outcome == "success",
                ProbeRow.kind == "download").order_by(ProbeRow.started_at.desc()).limit(1)).first()
            e = s.execute(select(NetEventRow.at, NetEventRow.bytes).where(
                NetEventRow.service_key == service_key, NetEventRow.ok.is_(True),
                NetEventRow.operation == "download").order_by(NetEventRow.at.desc()).limit(1)).first()
        cands = [c for c in (p, e) if c]
        if not cands:
            return None, 0
        best = max(cands, key=lambda c: _aware(c[0]))
        return _aware(best[0]), best[1]

    def recent_events(self, service_key: str, limit: int = 10) -> list[NetEventRow]:
        with Session(self.engine) as s:
            rows = s.execute(select(NetEventRow).where(NetEventRow.service_key == service_key)
                             .order_by(NetEventRow.at.desc()).limit(limit)).scalars().all()
            s.expunge_all()
            return list(rows)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        kw: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool  # one shared in-memory database across threads
        return create_engine(url, **kw)
    return create_engine(url, pool_pre_ping=True)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
