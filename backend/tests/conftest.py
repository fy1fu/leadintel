import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LEADINTEL_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live network test; set LEADINTEL_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ----------------------------------------------------------------------------- business DB fixtures
TEST_DB_URL = os.environ.get("LEADINTEL_TEST_DATABASE_URL")


def _refuse_non_test_database(engine) -> None:
    """The fixtures below drop and truncate tables: stop the whole run unless this is clearly a test database."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    with engine.connect() as c:
        name = c.execute(text("SELECT current_database()")).scalar()
    if not name.endswith("_test"):
        pytest.exit(f"refusing to reset database {name!r}: LEADINTEL_TEST_DATABASE_URL must name a database "
                    "whose name ends in '_test'", returncode=3)
    business = os.environ.get("LEADINTEL_DATABASE_URL")
    if business:
        b, t = make_url(business), make_url(TEST_DB_URL)
        if (b.host or "localhost", b.port or 5432, b.database) == (t.host or "localhost", t.port or 5432, t.database):
            pytest.exit("refusing to reset the test database: it is the same database as LEADINTEL_DATABASE_URL",
                        returncode=3)


@pytest.fixture(scope="session")
def pg_engine():
    """Migrated Postgres+PostGIS test database (skipped when LEADINTEL_TEST_DATABASE_URL is unset)."""
    if not TEST_DB_URL:
        pytest.skip("LEADINTEL_TEST_DATABASE_URL not set")
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    engine = create_engine(TEST_DB_URL, future=True)
    from sqlalchemy import text
    from app.db.models import Base
    _refuse_non_test_database(engine)
    # Fresh schema each session (downgrade would refuse to drop file-version history). Drop only LeadIntel's
    # own tables - never the schema or the PostGIS / pg_trgm extensions: PostGIS can only be (re)created by a
    # superuser, and the documented setup (WINDOWS_SETUP.md) runs tests as a regular database user.
    ours = {t.name for t in Base.metadata.sorted_tables} | {"alembic_version"}
    with engine.begin() as c:
        existing = set(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        drop = sorted(ours & existing)
        if drop:
            c.execute(text("DROP TABLE " + ", ".join(f'public."{t}"' for t in drop) + " CASCADE"))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(pg_engine):
    """Clean tables for each test (structure kept)."""
    from sqlalchemy import text
    from app.db.models import Base
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with pg_engine.begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return pg_engine
