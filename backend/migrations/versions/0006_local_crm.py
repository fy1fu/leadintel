"""local crm

M2: single-user local CRM with stable identities (UUID organizations and users), claims (one row per
organization and business; a second active claim is impossible), pipeline status, next action and follow-up
date, append-only activity history (notes, calls, emails, meetings, and every claim/status change), and saved
lists / campaigns. Purely additive: no existing table or row is changed. CRM rows only reference location ids,
so business facts, provenance and scores are untouched. Downgrade drops only these new tables.

Numbering: 0006 was earlier reserved for B0.5 (fencing token); the owner decided on 2026-11-06 that M2 takes
0006 and B0.5, if still needed, follows as the next linear migration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 10:00:00.000000
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
STATUSES = "('new', 'researching', 'contacted', 'follow_up', 'interested', 'meeting', 'proposal', 'won', 'lost', 'not_a_fit')"
KINDS = ("('note', 'call', 'email', 'meeting', 'visit', 'claim', 'release', 'reassign', 'status', 'next_action', "
         "'follow_up', 'list_add', 'list_remove')")


def _now():
    return sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)


def upgrade() -> None:
    op.create_table(
        'crm_organization',
        sa.Column('id', UUID, server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(16), server_default='local', nullable=False),
        _now(),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ux_crm_organization_one_local', 'crm_organization', ['kind'], unique=True,
                    postgresql_where=sa.text("kind = 'local'"))

    op.create_table(
        'crm_user',
        sa.Column('id', UUID, server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', UUID, sa.ForeignKey('crm_organization.id'), nullable=False),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('initials', sa.String(4), nullable=False),
        sa.Column('email', sa.Text(), nullable=True),
        sa.Column('role', sa.String(16), server_default='owner', nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name='ck_crm_user_role'))
    op.create_index('ix_crm_user_organization_id', 'crm_user', ['organization_id'])

    op.create_table(
        'crm_prospect',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('organization_id', UUID, sa.ForeignKey('crm_organization.id'), nullable=False),
        sa.Column('location_id', sa.BigInteger(), sa.ForeignKey('location.id'), nullable=False),
        sa.Column('owner_user_id', UUID, sa.ForeignKey('crm_user.id'), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(24), server_default='new', nullable=False),
        sa.Column('next_action', sa.Text(), nullable=True),
        sa.Column('follow_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        _now(),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'location_id', name='ux_crm_prospect_org_location'),
        sa.CheckConstraint(f"status IN {STATUSES}", name='ck_crm_prospect_status'))
    op.create_index('ix_crm_prospect_location_id', 'crm_prospect', ['location_id'])
    op.create_index('ix_crm_prospect_owner', 'crm_prospect', ['organization_id', 'owner_user_id'])
    op.create_index('ix_crm_prospect_follow_up', 'crm_prospect', ['organization_id', 'follow_up_at'])
    op.create_index('ix_crm_prospect_status', 'crm_prospect', ['organization_id', 'status'])

    op.create_table(
        'crm_activity',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('organization_id', UUID, sa.ForeignKey('crm_organization.id'), nullable=False),
        sa.Column('location_id', sa.BigInteger(), sa.ForeignKey('location.id'), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('crm_user.id'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('data', postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(f"kind IN {KINDS}", name='ck_crm_activity_kind'))
    op.create_index('ix_crm_activity_location', 'crm_activity', ['organization_id', 'location_id', 'occurred_at'])
    op.create_index('ix_crm_activity_user', 'crm_activity', ['organization_id', 'user_id', 'occurred_at'])

    op.create_table(
        'crm_list',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('organization_id', UUID, sa.ForeignKey('crm_organization.id'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(16), server_default='list', nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by', UUID, sa.ForeignKey('crm_user.id'), nullable=True),
        sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'name', name='ux_crm_list_org_name'),
        sa.CheckConstraint("kind IN ('list', 'campaign')", name='ck_crm_list_kind'))

    op.create_table(
        'crm_list_item',
        sa.Column('list_id', sa.BigInteger(), sa.ForeignKey('crm_list.id', ondelete='CASCADE'), nullable=False),
        sa.Column('location_id', sa.BigInteger(), sa.ForeignKey('location.id'), nullable=False),
        sa.Column('added_by', UUID, sa.ForeignKey('crm_user.id'), nullable=True),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('list_id', 'location_id'))
    op.create_index('ix_crm_list_item_location_id', 'crm_list_item', ['location_id'])


def downgrade() -> None:
    for table in ('crm_list_item', 'crm_list', 'crm_activity', 'crm_prospect', 'crm_user', 'crm_organization'):
        op.drop_table(table)
