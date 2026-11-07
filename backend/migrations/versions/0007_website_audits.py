"""website audits and saved searches

M3: website audits (runs, pages, screenshot/file references with checksums, findings, recommendations, benchmark
snapshots) and CRM saved searches (filters including the Search Area). Purely additive: no existing table or row
changes. The WQS itself is written to the existing `score` table (kind 'wqs'). Screenshots stay as files in the
managed data folder; only relative paths, checksums and sizes are stored. Downgrade drops only these tables.

Numbering: 0007 is reserved for M3 (owner decision 2026-11-06); B0.5 (fencing token) takes the next number.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06 20:00:00.000000
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB()
STATUSES = ("('running', 'completed', 'partial', 'failed', 'blocked', 'robots_disallowed', 'robots_unavailable', "
            "'no_website', 'interrupted')")


def _now(name='created_at'):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)


def _audit_fk():
    return sa.Column('audit_id', sa.BigInteger(), sa.ForeignKey('website_audit.id', ondelete='CASCADE'), nullable=False)


def upgrade() -> None:
    op.create_table(
        'website_audit',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('location_id', sa.BigInteger(), sa.ForeignKey('location.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('start_url', sa.Text()), sa.Column('reason', sa.Text()), sa.Column('robots', sa.String(16)),
        sa.Column('crawl_delay_s', sa.Float()),
        sa.Column('pages_crawled', sa.Integer(), server_default='0', nullable=False),
        sa.Column('requested_by', UUID),
        _now('started_at'),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('wqs', sa.Float()), sa.Column('wqs_confidence', sa.Float()),
        sa.Column('wqs_version', sa.String(20)), sa.Column('wqs_status', sa.String(20)),
        sa.Column('dimensions', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('explanation', JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('observations', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('score_id', sa.BigInteger(), sa.ForeignKey('score.id')),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(f"status IN {STATUSES}", name='ck_website_audit_status'))
    op.create_index('ix_website_audit_location', 'website_audit', ['location_id', 'started_at'])

    op.create_table(
        'website_audit_page',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False), _audit_fk(),
        sa.Column('seq', sa.Integer(), nullable=False), sa.Column('url', sa.Text(), nullable=False),
        sa.Column('final_url', sa.Text()), sa.Column('status', sa.Integer()), sa.Column('title', sa.Text()),
        sa.Column('html_bytes', sa.Integer()), sa.Column('text_sha256', sa.String(64)), sa.Column('links', sa.Integer()),
        sa.Column('elapsed_ms', sa.Integer()), sa.Column('blocked_reason', sa.Text()), sa.Column('error', sa.Text()),
        sa.Column('facts', JSONB), sa.Column('axe', JSONB),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('audit_id', 'seq', name='ux_website_audit_page_seq'))

    op.create_table(
        'website_audit_artifact',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False), _audit_fk(),
        sa.Column('page_id', sa.BigInteger(), sa.ForeignKey('website_audit_page.id', ondelete='CASCADE')),
        sa.Column('kind', sa.String(24), nullable=False), sa.Column('rel_path', sa.Text(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False), sa.Column('bytes', sa.Integer(), nullable=False),
        _now(), sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_website_audit_artifact_audit_id', 'website_audit_artifact', ['audit_id'])

    op.create_table(
        'website_audit_finding',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False), _audit_fk(),
        sa.Column('criterion', sa.String(60), nullable=False), sa.Column('dimension', sa.String(16), nullable=False),
        sa.Column('kind', sa.String(10), nullable=False), sa.Column('passed', sa.Boolean()),
        sa.Column('weight', sa.Float(), nullable=False), sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False), _now('measured_at'), sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_website_audit_finding_audit_id', 'website_audit_finding', ['audit_id'])

    op.create_table(
        'website_audit_recommendation',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False), _audit_fk(),
        sa.Column('criterion', sa.String(60), nullable=False), sa.Column('severity', sa.String(10), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False), sa.Column('title', sa.Text(), nullable=False),
        sa.Column('finding', sa.Text(), nullable=False), sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('why', sa.Text(), nullable=False), sa.Column('suggestion', sa.Text(), nullable=False),
        sa.Column('benchmark', sa.Text()), sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_website_audit_recommendation_audit_id', 'website_audit_recommendation', ['audit_id'])

    op.create_table(
        'website_benchmark',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('audit_id', sa.BigInteger(), sa.ForeignKey('website_audit.id', ondelete='CASCADE'), nullable=False),
        sa.Column('scope', sa.String(10), nullable=False), sa.Column('niche', sa.String(40)), sa.Column('area', sa.Text()),
        sa.Column('peers', sa.Integer(), nullable=False),
        sa.Column('stats', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        _now('computed_at'), sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('audit_id'))

    op.create_table(
        'crm_saved_search',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('organization_id', UUID, sa.ForeignKey('crm_organization.id'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False), sa.Column('params', JSONB, nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('created_by', UUID, sa.ForeignKey('crm_user.id')),
        _now(), _now('updated_at'), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'name', name='ux_crm_saved_search_org_name'))


def downgrade() -> None:
    for table in ('crm_saved_search', 'website_benchmark', 'website_audit_recommendation', 'website_audit_finding',
                  'website_audit_artifact', 'website_audit_page', 'website_audit'):
        op.drop_table(table)
