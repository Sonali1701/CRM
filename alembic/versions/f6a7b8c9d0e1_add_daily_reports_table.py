"""add daily_reports table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'daily_reports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('client_name', sa.Text(), nullable=True),
        sa.Column('accounts_worked', sa.Integer(), default=0),
        sa.Column('emails_sent', sa.Integer(), default=0),
        sa.Column('calls_dialed', sa.Integer(), default=0),
        sa.Column('meetings_set', sa.Integer(), default=0),
        sa.Column('meetings_attended', sa.Integer(), default=0),
        sa.Column('linkedin_requests_sent', sa.Integer(), default=0),
        sa.Column('linkedin_connections', sa.Integer(), default=0),
        sa.Column('important_conversations', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_daily_reports_user_id', 'daily_reports', ['user_id'])
    op.create_index('ix_daily_reports_report_date', 'daily_reports', ['report_date'])


def downgrade():
    op.drop_index('ix_daily_reports_report_date', table_name='daily_reports')
    op.drop_index('ix_daily_reports_user_id', table_name='daily_reports')
    op.drop_table('daily_reports')
