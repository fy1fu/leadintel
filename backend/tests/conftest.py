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
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DB_URL, future=True)
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
