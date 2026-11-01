"""CEO 自动招人：部门下的组 + 统筹新招台账

新表 `agent_groups`：部门 → 组 的组织层级，CEO 按需招人时把新员工放进去。
老表补两列（全部可空，老数据保持 NULL）：
* `agents.group_id`：员工所属的组，SET NULL 级联（删组不删人）；
* `ceo_missions.new_agents`：本次统筹新招了谁（开工前是提议，开工后回填 id）。

SQLite 加带外键的列必须走 batch 重建表，约束显式命名。

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("department_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("department_id", "name",
                            name="uq_agent_group_dept_name"),
    )
    op.create_index("ix_agent_groups_company_id", "agent_groups", ["company_id"])
    op.create_index("ix_agent_groups_department_id", "agent_groups",
                    ["department_id"])

    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.String(length=36),
                                      nullable=True))
        batch_op.create_foreign_key(
            "fk_agents_group_id", "agent_groups", ["group_id"], ["id"],
            ondelete="SET NULL")
        batch_op.create_index("ix_agents_group_id", ["group_id"])

    with op.batch_alter_table("ceo_missions") as batch_op:
        batch_op.add_column(sa.Column("new_agents", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_agents_group_id", table_name="agents")
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("group_id")

    with op.batch_alter_table("ceo_missions") as batch_op:
        batch_op.drop_column("new_agents")

    op.drop_table("agent_groups")
