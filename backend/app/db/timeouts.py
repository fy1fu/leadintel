"""Default PostgreSQL connection timeout for every engine LeadIntel creates.

Without one, psycopg waits up to its built-in 130 s before giving up. On Windows a refused connection
(PostgreSQL service stopped) is not reported early, so commands and the API hung for 130 s instead of
reporting "business database unavailable" (found during the Windows validation, V12).

A timeout the user configured is never overridden: a `connect_timeout` in the database URL, or libpq's
standard PGCONNECT_TIMEOUT environment variable, wins over this default.
"""
from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

PG_CONNECT_TIMEOUT_S = 5


def pg_connect_args(url: str | URL) -> dict:
    """`create_engine` keyword arguments adding the default connect timeout to PostgreSQL URLs only."""
    u = make_url(url)
    if not u.drivername.startswith("postgresql"):
        return {}
    if "connect_timeout" in u.query or os.environ.get("PGCONNECT_TIMEOUT"):
        return {}
    return {"connect_args": {"connect_timeout": PG_CONNECT_TIMEOUT_S}}
