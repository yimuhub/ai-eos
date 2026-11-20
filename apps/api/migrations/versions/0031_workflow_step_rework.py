"""步骤级返工（M3-A）：给 workflow_steps 加输入指纹与失效戳。

只加两列、都可空，存量步骤不需要回填；旧行 input_signature 为 NULL，
等价于"从未复用"，行为与升级前一致。

Revision ID: 0031
Revises: 0030
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, Sequence[str], None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_steps",
                  sa.Column("input_signature", sa.String(length=64), nullable=True))
    op.add_column("workflow_steps",
                  sa.Column("stale_at", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_steps", "stale_at")
    op.drop_column("workflow_steps", "input_signature")
