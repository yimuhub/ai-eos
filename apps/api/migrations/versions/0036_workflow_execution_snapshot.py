"""工作流执行固定定义快照，避免运行中改图影响历史执行。

Revision ID: 0036
Revises: 0035
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, Sequence[str], None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_executions",
                  sa.Column("definition_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_executions", "definition_snapshot")
