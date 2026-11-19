"""技能库（V6.2 专家技能引入方案）

新增 `skill_library`：把外部技能生态（WorkBuddy/CodeBuddy 公开市场的
SKILL.md / mcp.json / 专家团队包）以统一格式收录进来，经审核后发布给企业。

三种 kind：
* skill     — 提示型技能（SKILL.md：frontmatter + 方法论正文）
* connector — 连接器蓝图（mcp.json，MCP server 配置）
* expert    — 专家团队包（agents/*.md 人设 + 预设编排）

审核流：import 落库 → auto_pass（无品牌引用）或 review（含品牌引用/付费依赖）
→ 超管放行 published / 拒绝 rejected。企业端只见 published。

Revision ID: 0028
Revises: 0027
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, Sequence[str], None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_library",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False,
                  server_default="workbuddy"),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False,
                  server_default="skill"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("trigger_keywords", sa.JSON(), nullable=True),
        sa.Column("industry_tags", sa.JSON(), nullable=True),
        sa.Column("license", sa.String(length=60), nullable=True),
        sa.Column("brand_refs", sa.JSON(), nullable=True),
        sa.Column("review_status", sa.String(length=20), nullable=False,
                  server_default="review"),
        sa.Column("reviewer", sa.String(length=80), nullable=True),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("installed_companies", sa.JSON(), nullable=True),
        sa.Column("install_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id", name="uq_skill_library_source"),
    )
    op.create_index("ix_skill_library_source", "skill_library", ["source"])
    op.create_index("ix_skill_library_kind", "skill_library", ["kind"])
    op.create_index("ix_skill_library_review_status", "skill_library",
                    ["review_status"])
    op.create_index("ix_skill_library_company_id", "skill_library",
                    ["company_id"])


def downgrade() -> None:
    op.drop_table("skill_library")
