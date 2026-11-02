"""CEO 常驻对话线程

新增 `ceo_threads`，并给 `agent_chat_messages` 补可空 `thread_id`。
老消息 thread_id 为空，首次打开 CEO 默认线程时由应用归入其中，
因此迁移阶段不需要回填业务数据。

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ceo_threads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summarized_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ceo_threads", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ceo_threads_company_id"),
                              ["company_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ceo_threads_agent_id"),
                              ["agent_id"], unique=False)

    with op.batch_alter_table("agent_chat_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("thread_id", sa.String(length=36),
                                      nullable=True))
        batch_op.create_index(batch_op.f("ix_agent_chat_messages_thread_id"),
                              ["thread_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_chat_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_chat_messages_thread_id"))
        batch_op.drop_column("thread_id")

    with op.batch_alter_table("ceo_threads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ceo_threads_agent_id"))
        batch_op.drop_index(batch_op.f("ix_ceo_threads_company_id"))

    op.drop_table("ceo_threads")
