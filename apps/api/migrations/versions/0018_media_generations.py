"""创作中心：图片/视频生成记录

新增 `media_generations`：模型目录此前已支持 image/video 分类，但产品
没有生成路径。本表记录每次生成的提示词、参数、上游任务号与结果文件，
让「配置了图片/视频模型」真正变成「能生成、能回看、能下载」。

Revision ID: 0018
Revises: 0017
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_generations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("model_row_id", sa.String(length=36), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("upstream_video_id", sa.String(length=200), nullable=True),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("local_path", sa.String(length=500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_row_id"], ["models.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_generations_company_id",
                    "media_generations", ["company_id"])
    op.create_index("ix_media_generations_status",
                    "media_generations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_media_generations_status",
                  table_name="media_generations")
    op.drop_index("ix_media_generations_company_id",
                  table_name="media_generations")
    op.drop_table("media_generations")