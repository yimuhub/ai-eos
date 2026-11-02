"""CEO 身份与专属技能包

两列都是可空/有默认值，老数据无需回填：

* `agents.pre_ceo_name`：任命 CEO 前的姓名，解除时还原；
* `agent_skills.source`：绑定来源（manual/auto/ceo_bundle），解除 CEO 时
  只删除 `ceo_bundle`，不会误删老板手工安装或员工自己学的技能。
  老行补 `manual`，与升级前的语义一致。

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("pre_ceo_name", sa.String(length=200),
                                      nullable=True))

    with op.batch_alter_table("agent_skills") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=20),
                                      nullable=True,
                                      server_default="manual"))


def downgrade() -> None:
    with op.batch_alter_table("agent_skills") as batch_op:
        batch_op.drop_column("source")

    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("pre_ceo_name")
