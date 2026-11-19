"""列宽修正（PG 真库回归抓出）：SQLite 不 enforce VARCHAR 长度，
构造型 id（如 `kb_chunks.id = {document_id}-0-{后缀}`、
`agent_memories.context_ref = "experience:" + id`）实际长度超过 36——
SQLite 下从不出错，PG 下 StringDataRightTruncation。

batch_alter_table 兼容 SQLite（SQLite 不支持裸 ALTER COLUMN）。

Revision ID: 0029
Revises: 0028
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, Sequence[str], None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("kb_chunks") as batch:
        batch.alter_column("id", existing_type=sa.String(36),
                           type_=sa.String(100))
    with op.batch_alter_table("agent_memories") as batch:
        batch.alter_column("context_ref", existing_type=sa.String(36),
                           type_=sa.String(100), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("agent_memories") as batch:
        batch.alter_column("context_ref", existing_type=sa.String(100),
                           type_=sa.String(36), nullable=True)
    with op.batch_alter_table("kb_chunks") as batch:
        batch.alter_column("id", existing_type=sa.String(100),
                           type_=sa.String(36))
