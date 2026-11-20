"""CEO 统筹接阵容（M6）：`ceo_missions` 新增可空 `loadout_id`。

只加一列、不改老数据：历史统筹 `loadout_id` 为 NULL，行为与之前完全一致
（不锁阵容）。带 loadout 的统筹才在生成工作流时按阵容过滤员工。

Revision ID: 0035
Revises: 0034
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, Sequence[str], None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 的 ALTER ADD COLUMN 不支持带默认值表达式，这里只加空列
    op.add_column("ceo_missions",
                  sa.Column("loadout_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("ceo_missions", "loadout_id")
