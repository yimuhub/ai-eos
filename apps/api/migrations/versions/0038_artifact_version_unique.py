"""Unique artifact version numbers for concurrent submissions.

Revision ID: 0038
Revises: 0037
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, Sequence[str], None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        'SELECT artifact_id, version, COUNT(*) AS "n" '
        'FROM artifact_versions GROUP BY artifact_id, version HAVING COUNT(*) > 1 '
        "ORDER BY artifact_id, version LIMIT 20")).all()
    if duplicates:
        details = ", ".join(f"{row.artifact_id}:v{row.version}({row.n})"
                            for row in duplicates)
        raise RuntimeError(
            "artifact_versions 存在重复版本号，无法安全建立唯一约束；"
            "请先人工核对并修复历史版本，再迁移。重复项：" + details)
    with op.batch_alter_table("artifact_versions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_artifact_versions_artifact_id_version", ["artifact_id", "version"])


def downgrade() -> None:
    with op.batch_alter_table("artifact_versions") as batch_op:
        batch_op.drop_constraint(
            "uq_artifact_versions_artifact_id_version", type_="unique")
