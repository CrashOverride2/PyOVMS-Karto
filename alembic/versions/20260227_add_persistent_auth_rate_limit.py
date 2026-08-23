"""Add persistent auth failure and IP ban tables

Revision ID: 20260227
Revises: 7c8c122629f0
Create Date: 2026-02-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260227'
down_revision: Union[str, None] = '7c8c122629f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'auth_failure_state',
        sa.Column('ip_address', sa.String(length=45), nullable=False),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('ip_address')
    )

    op.create_table(
        'auth_ip_bans',
        sa.Column('ip_address', sa.String(length=45), nullable=False),
        sa.Column('banned_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('ip_address')
    )
    op.create_index(op.f('ix_auth_ip_bans_banned_until'), 'auth_ip_bans', ['banned_until'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_auth_ip_bans_banned_until'), table_name='auth_ip_bans')
    op.drop_table('auth_ip_bans')
    op.drop_table('auth_failure_state')
