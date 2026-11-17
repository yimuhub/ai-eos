"""行业专家包与访谈完善（V6.2）

* `expert_templates` 加 4 个可空列：expert_pack_id（来源行业专家包）/
  category（细分领域：小麦/玉米/冲压…）/ skills（标准化技能清单 JSON）/
  interview_history（访谈完善留痕 JSON）——ensure_columns 也会幂等补列。
* 新表 `expert_interviews`：部门访谈资料的登记与总控提炼账。

Revision ID: 0026
Revises: 0025
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, Sequence[str], None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("expert_templates",
                  sa.Column("expert_pack_id", sa.String(length=80),
                            nullable=True))
    op.add_column("expert_templates",
                  sa.Column("category", sa.String(length=60), nullable=True))
    op.add_column("expert_templates", sa.Column("skills", sa.JSON(),
                                                nullable=True))
    op.add_column("expert_templates",
                  sa.Column("interview_history", sa.JSON(), nullable=True))
    op.create_index("ix_expert_templates_expert_pack_id", "expert_templates",
                    ["expert_pack_id"])

    op.create_table(
        "expert_interviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("expert_template_id", sa.String(length=36), nullable=True),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("department", sa.String(length=120), nullable=True),
        sa.Column("interviewee", sa.String(length=120), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("audio_media_id", sa.String(length=36), nullable=True),
        # uploaded | digested | applied | failed
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="uploaded"),
        sa.Column("digest", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expert_interviews_company_id", "expert_interviews",
                    ["company_id"])


def downgrade() -> None:
    op.drop_table("expert_interviews")
    op.drop_index("ix_expert_templates_expert_pack_id",
                  table_name="expert_templates")
    op.drop_column("expert_templates", "interview_history")
    op.drop_column("expert_templates", "skills")
    op.drop_column("expert_templates", "category")
    op.drop_column("expert_templates", "expert_pack_id")
