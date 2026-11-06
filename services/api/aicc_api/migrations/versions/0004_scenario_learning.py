"""scenario learning: 승인된 시나리오 변경 + AI 제안

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenario_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("proposal_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scenario_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("target_code", sa.String(60), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("scenario_proposals")
    op.drop_table("scenario_changes")
