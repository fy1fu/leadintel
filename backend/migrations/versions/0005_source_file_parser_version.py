"""source file parser version

Records which importer rules produced a file's import, so a file imported under older rules
(e.g. before the DBPR validity/geography fixes) is re-imported once instead of being skipped.
Additive and nullable: existing rows read as "unknown version" and are re-imported on the next run.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 04:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_file", sa.Column("parser_version", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("source_file", "parser_version")
