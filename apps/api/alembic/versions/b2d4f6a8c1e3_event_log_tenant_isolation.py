"""event_log tenant isolation

审计事件此前没有任何公司归属，`GET /events` 与 `/events/export` 会把全平台
日志返回给任意登录用户。本次为 event_log 增加可空的 company_id，供查询侧做
租户过滤；历史行保持 NULL，查询侧一律不返回（安全优先）。

同时为 created_at 补索引，避免事件列表/导出按时间倒序时全表排序。

Revision ID: b2d4f6a8c1e3
Revises: a1c7e9c0ffee
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c1e3"
down_revision: str | None = "a1c7e9c0ffee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_log",
        sa.Column("company_id", sa.String(36), nullable=True),
    )
    op.create_index(op.f("ix_event_log_company_id"), "event_log",
                    ["company_id"], unique=False)
    op.create_index("ix_event_log_created_at", "event_log",
                    ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_event_log_created_at", table_name="event_log")
    op.drop_index(op.f("ix_event_log_company_id"), table_name="event_log")
    op.drop_column("event_log", "company_id")
