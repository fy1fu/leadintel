"""Editable application settings (approved decision F1).

* Packaged defaults in `config/defaults/*.yaml` always load, so the app can start and recover
  when the business database is unavailable.
* The business database holds the versioned, editable copy (`config_version`). Every change is a
  new version; nothing is overwritten. Kinds that affect scoring require approval before they
  become active; any earlier version can be restored (a restore is itself a new version).
* Network settings (`config/network.yaml`) are deliberately NOT managed here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ConfigVersion

KINDS = ("areas", "keywords", "industries", "scoring", "sources")
APPROVAL_REQUIRED = {"scoring"}
REQUIRED_KEYS = {
    "areas": ["targets", "boundaries"],
    "keywords": ["groups", "negative", "hard_exclude"],
    "industries": ["profiles"],
    "scoring": ["bos_weights", "evidence_points", "evidence_caps", "lps", "qualification"],
    "sources": ["sources"],
}


class ConfigError(ValueError):
    pass


def _hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(kind: str, content: dict) -> None:
    if kind not in KINDS:
        raise ConfigError(f"unknown config kind {kind!r}")
    if not isinstance(content, dict):
        raise ConfigError(f"{kind}: content must be a mapping")
    missing = [k for k in REQUIRED_KEYS[kind] if k not in content]
    if missing:
        raise ConfigError(f"{kind}: missing keys {missing}")
    if kind == "scoring":
        w = content["bos_weights"]
        if abs(sum(w.values()) - 1.0) > 1e-6:
            raise ConfigError("scoring.bos_weights must sum to 1.0")
        if any(int(k) < 0 or v > 100 for k, v in content["evidence_caps"].items()):
            raise ConfigError("scoring.evidence_caps invalid")


def load_defaults(defaults_dir: Path) -> dict[str, dict]:
    out = {}
    for kind in KINDS:
        path = defaults_dir / f"{kind}.yaml"
        with open(path, encoding="utf-8") as fh:
            content = yaml.safe_load(fh)
        validate(kind, content)
        out[kind] = content
    return out


@dataclass
class EffectiveConfig:
    content: dict[str, dict]
    versions: dict[str, int | None]            # kind -> config_version.id (None = packaged default)
    mode: str                                   # database | defaults (database unavailable: ...)
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, kind: str) -> dict:
        return self.content[kind]


class ConfigService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.defaults = load_defaults(settings.defaults_dir)

    # ---- reads --------------------------------------------------------------
    def active(self, s: Session, kind: str) -> ConfigVersion | None:
        return s.execute(select(ConfigVersion).where(ConfigVersion.kind == kind, ConfigVersion.status == "active")
                         .order_by(ConfigVersion.version.desc()).limit(1)).scalar_one_or_none()

    def history(self, s: Session, kind: str) -> list[ConfigVersion]:
        return list(s.execute(select(ConfigVersion).where(ConfigVersion.kind == kind)
                              .order_by(ConfigVersion.version)).scalars())

    def effective(self, engine: Engine | None) -> EffectiveConfig:
        """Active DB versions, or packaged defaults when the database is unavailable."""
        if engine is None:
            return EffectiveConfig(dict(self.defaults), {k: None for k in KINDS}, "defaults (no database configured)")
        try:
            with Session(engine) as s:
                content, versions, warnings = {}, {}, []
                for kind in KINDS:
                    row = self.active(s, kind)
                    if row is None:
                        content[kind], versions[kind] = self.defaults[kind], None
                        warnings.append(f"{kind}: no active version in database; using packaged default")
                    else:
                        content[kind], versions[kind] = row.content, row.id
                return EffectiveConfig(content, versions, "database", warnings)
        except Exception as exc:  # noqa: BLE001 - must always be able to start
            return EffectiveConfig(dict(self.defaults), {k: None for k in KINDS},
                                   f"defaults (database unavailable: {type(exc).__name__})",
                                   ["running on packaged defaults; edits are disabled until the database is back"])

    # ---- writes (append-only) ----------------------------------------------
    def _next_version(self, s: Session, kind: str) -> int:
        rows = self.history(s, kind)
        return (rows[-1].version + 1) if rows else 1

    def seed(self, s: Session) -> list[str]:
        seeded = []
        for kind in KINDS:
            if self.active(s, kind) is None and not self.history(s, kind):
                content = self.defaults[kind]
                s.add(ConfigVersion(kind=kind, version=1, content=content, content_hash=_hash(content), status="active",
                                    requires_approval=kind in APPROVAL_REQUIRED, created_by="defaults",
                                    approved_by="defaults" if kind in APPROVAL_REQUIRED else None,
                                    approved_at=datetime.now(timezone.utc) if kind in APPROVAL_REQUIRED else None,
                                    note="seeded from packaged defaults"))
                seeded.append(kind)
        s.flush()
        return seeded

    def _activate(self, s: Session, row: ConfigVersion) -> None:
        current = self.active(s, row.kind)
        if current is not None and current.id != row.id:
            current.status = "superseded"
            row.supersedes_id = current.id
        row.status = "active"

    def propose(self, s: Session, kind: str, content: dict, *, created_by: str, note: str | None = None) -> ConfigVersion:
        validate(kind, content)
        current = self.active(s, kind)
        if current is not None and current.content_hash == _hash(content):
            raise ConfigError(f"{kind}: proposed content is identical to the active version")
        row = ConfigVersion(kind=kind, version=self._next_version(s, kind), content=content, content_hash=_hash(content),
                            status="proposed", requires_approval=kind in APPROVAL_REQUIRED, created_by=created_by,
                            note=note)
        s.add(row)
        s.flush()
        if kind not in APPROVAL_REQUIRED:
            self._activate(s, row)
            s.flush()
        return row

    def approve(self, s: Session, version_id: int, *, approver: str) -> ConfigVersion:
        row = s.get(ConfigVersion, version_id)
        if row is None or row.status != "proposed":
            raise ConfigError("only proposed versions can be approved")
        row.approved_by, row.approved_at = approver, datetime.now(timezone.utc)
        self._activate(s, row)
        s.flush()
        return row

    def reject(self, s: Session, version_id: int, *, actor: str) -> ConfigVersion:
        row = s.get(ConfigVersion, version_id)
        if row is None or row.status != "proposed":
            raise ConfigError("only proposed versions can be rejected")
        row.status, row.note = "rejected", (row.note or "") + f" [rejected by {actor}]"
        s.flush()
        return row

    def restore(self, s: Session, kind: str, version_id: int, *, actor: str) -> ConfigVersion:
        """Reverse a change: copy an earlier version's content into a new, explicitly approved version."""
        target = s.get(ConfigVersion, version_id)
        if target is None or target.kind != kind:
            raise ConfigError("unknown version for this kind")
        row = ConfigVersion(kind=kind, version=self._next_version(s, kind), content=target.content,
                            content_hash=target.content_hash, status="proposed",
                            requires_approval=kind in APPROVAL_REQUIRED, created_by=actor,
                            approved_by=actor, approved_at=datetime.now(timezone.utc),
                            note=f"restore of version {target.version}")
        s.add(row)
        s.flush()
        self._activate(s, row)
        s.flush()
        return row
