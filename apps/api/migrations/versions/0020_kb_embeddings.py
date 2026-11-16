"""知识库混合检索：kb_chunks.embedding 列护栏（V6 M1-f）

`kb_chunks.embedding`（JSON 浮点数组）自基线 0001 起就作为预留列存在，
`Base.metadata.create_all` 建出的库同样带它——所以本迁移**不引入新结构**，
upgrade 先用 inspector 探列、缺了才补（幂等，有列时是 no-op）。作用：

1. 兜底极少数缺列的库（如手工删过列的 PG 实例），保证 `upgrade head`
   之后结构完整，混合检索（V6 M1-f）能写入向量；
2. 在演进历史上标记「知识库向量语义检索」这一特性节点。

不能无条件 `ADD COLUMN`：空库 `upgrade head` 时 0001 已经建出该列，
再来一次就是 duplicate column，`test_alembic_baseline_matches_create_all`
（空库 upgrade head == create_all）会直接红。

Revision ID: 0020
Revises: 0019
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, Sequence[str], None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("kb_chunks")}
    if "embedding" in columns:
        return
    op.add_column("kb_chunks",
                  sa.Column("embedding", sa.JSON(), nullable=True))


def downgrade() -> None:
    # 该列不属于本迁移（0001 基线已建、一直存在），downgrade 不删除，
    # 否则会破坏基线结构、与 0019 时的库形态不一致。
    return None
