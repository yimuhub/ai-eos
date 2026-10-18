"""meetings.attachments 会议附件列

会议创建表单带 📎 附件口（附件经 /upload 真实入库为资产记录），
提交时把附件摘要数组（[{id, name, size, ...}]）存到 meetings.attachments。
可空 JSON：老行/未带附件的会议为 NULL，读侧一律 `m.attachments or []`。

Revision ID: f3a1c9e77b02
Revises: d4e7f2a91b3c
Create Date: 2026-10-18 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'f3a1c9e77b02'
down_revision: Union[str, Sequence[str], None] = 'd4e7f2a91b3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """幂等：列已存在（手工 DDL / 自动补列已加过）时直接跳过。"""
    conn = op.get_bind()
    existing = {c["name"] for c in inspect(conn).get_columns("meetings")}
    if "attachments" not in existing:
        op.add_column("meetings", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    existing = {c["name"] for c in inspect(conn).get_columns("meetings")}
    if "attachments" in existing:
        op.drop_column("meetings", "attachments")
