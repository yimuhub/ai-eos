"""超级管理后台：公司行业类别列

新增 `companies.industry`（可空）：运营后台给公司解锁行业时写入的类别标记，
与 feature flag `industry:<pack_id>`（已装行业包）配合构成授权事实源。

Revision ID: 0025
Revises: 0024
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, Sequence[str], None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("industry", sa.String(length=40),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "industry")
