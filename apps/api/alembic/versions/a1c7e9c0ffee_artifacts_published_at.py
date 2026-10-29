"""artifacts.published_at

发布动作从未真正可用：routes/artifacts.py 一直往 artifacts 表写 published_at，
但 models/artifact.py 从未定义该列（SQLite 靠端点内幂等补列兜底，PG 必须有迁移）。

Revision ID: a1c7e9c0ffee
Revises: f3a1c9e77b02
Create Date: 2026-10-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e9c0ffee"
down_revision: str | None = "f3a1c9e77b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "published_at")
