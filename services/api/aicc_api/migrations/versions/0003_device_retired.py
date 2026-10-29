"""device retired_at (단말 목록에서 삭제 — 통화 기록이 있으면 숨김·토큰 폐기)

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as b:
        b.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("devices") as b:
        b.drop_column("retired_at")
