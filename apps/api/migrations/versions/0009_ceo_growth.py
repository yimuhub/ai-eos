"""CEO 拟人化成长：运行人格、分级进化、周考、经验池

四张新表，全部由 0007/0008 之后新增的模型引入：

* `ceo_playbooks`：版本化运行人格，同公司同时只有一条 active；
* `evolution_records`：一次日/周/月进化（指标 → 诊断 → 动作 → 结果），
  按 `(company_id, level, period_key)` 幂等；
* `ceo_exams`：独立监督 Agent 的周考，按 `(company_id, period_key)` 幂等，
  老板人工覆盖以 JSON 留痕；
* `shared_experiences`：Agent 成功/失败经验与已验证工作流，按
  `(task_id, agent_id)` 幂等，供跨 Agent 复用。

索引名必须与 `Base.metadata.create_all` 生成的一致，否则基线护栏会报
"索引集合不一致"。

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "ceo_playbooks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("persona", sa.JSON(), nullable=False),
        sa.Column("principles", sa.JSON(), nullable=False),
        sa.Column("adjustments", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("lessons", sa.JSON(), nullable=False),
        sa.Column("evolution_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "version",
                            name="uq_ceo_playbook_version"),
    )
    op.create_index("ix_ceo_playbooks_company_id", "ceo_playbooks",
                    ["company_id"])
    op.create_index("ix_ceo_playbooks_agent_id", "ceo_playbooks", ["agent_id"])
    op.create_index("ix_ceo_playbooks_active", "ceo_playbooks", ["active"])
    op.create_index("ix_ceo_playbooks_evolution_id", "ceo_playbooks",
                    ["evolution_id"])
    op.create_index("ix_ceo_playbooks_company_active", "ceo_playbooks",
                    ["company_id", "active"])

    op.create_table(
        "evolution_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("period_key", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("assessment", sa.JSON(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("playbook_id", sa.String(length=36), nullable=True),
        sa.Column("playbook_version", sa.Integer(), nullable=True),
        sa.Column("exam_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=36), nullable=True),
        sa.Column("decided_at", sa.String(length=50), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "level", "period_key",
                            name="uq_evolution_period"),
    )
    op.create_index("ix_evolution_records_company_id", "evolution_records",
                    ["company_id"])
    op.create_index("ix_evolution_records_level", "evolution_records",
                    ["level"])
    op.create_index("ix_evolution_records_status", "evolution_records",
                    ["status"])
    op.create_index("ix_evolution_records_playbook_id", "evolution_records",
                    ["playbook_id"])
    op.create_index("ix_evolution_company_created", "evolution_records",
                    ["company_id", "created_at"])

    op.create_table(
        "ceo_exams",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("ceo_agent_id", sa.String(length=36), nullable=True),
        sa.Column("supervisor_agent_id", sa.String(length=36), nullable=True),
        sa.Column("period_key", sa.String(length=20), nullable=False),
        sa.Column("week_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("week_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("focus", sa.String(length=500), nullable=True),
        sa.Column("questions", sa.JSON(), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("grades", sa.JSON(), nullable=True),
        sa.Column("scores", sa.JSON(), nullable=True),
        sa.Column("personality", sa.JSON(), nullable=True),
        sa.Column("overall", sa.Float(), nullable=True),
        sa.Column("grade", sa.String(length=4), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("recommendations", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("override", sa.JSON(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ceo_exams_company_id", "ceo_exams", ["company_id"])
    op.create_index("ix_ceo_exams_period_key", "ceo_exams", ["period_key"])
    op.create_index("ix_ceo_exams_status", "ceo_exams", ["status"])
    op.create_index("ix_ceo_exams_company_period", "ceo_exams",
                    ["company_id", "period_key"], unique=True)

    op.create_table(
        "shared_experiences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("author_agent_name", sa.String(length=200), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_title", sa.String(length=500), nullable=False),
        sa.Column("reuse_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=True),
        sa.Column("pitfalls", sa.JSON(), nullable=True),
        sa.Column("lessons", sa.JSON(), nullable=True),
        sa.Column("token_tips", sa.JSON(), nullable=True),
        sa.Column("quality_tips", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("tokens_saved_est", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("adopted_by", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "agent_id",
                            name="uq_shared_experiences_task_agent"),
    )
    op.create_index("ix_shared_experiences_company_id", "shared_experiences",
                    ["company_id"])
    op.create_index("ix_shared_experiences_agent_id", "shared_experiences",
                    ["agent_id"])
    op.create_index("ix_shared_experiences_task_id", "shared_experiences",
                    ["task_id"])
    op.create_index("ix_shared_experiences_reuse_key", "shared_experiences",
                    ["reuse_key"])
    op.create_index("ix_shared_experiences_kind", "shared_experiences",
                    ["kind"])
    op.create_index("ix_shared_experiences_scope", "shared_experiences",
                    ["scope"])
    op.create_index("ix_shared_experiences_status", "shared_experiences",
                    ["status"])
    op.create_index("ix_shared_experiences_workflow_id", "shared_experiences",
                    ["workflow_id"])
    op.create_index("ix_shared_experiences_company_key", "shared_experiences",
                    ["company_id", "reuse_key"])


def downgrade() -> None:
    op.drop_index("ix_shared_experiences_company_key",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_workflow_id",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_status",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_scope", table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_kind", table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_reuse_key",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_task_id",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_agent_id",
                  table_name="shared_experiences")
    op.drop_index("ix_shared_experiences_company_id",
                  table_name="shared_experiences")
    op.drop_table("shared_experiences")

    op.drop_index("ix_ceo_exams_company_period", table_name="ceo_exams")
    op.drop_index("ix_ceo_exams_status", table_name="ceo_exams")
    op.drop_index("ix_ceo_exams_period_key", table_name="ceo_exams")
    op.drop_index("ix_ceo_exams_company_id", table_name="ceo_exams")
    op.drop_table("ceo_exams")

    op.drop_index("ix_evolution_company_created",
                  table_name="evolution_records")
    op.drop_index("ix_evolution_records_playbook_id",
                  table_name="evolution_records")
    op.drop_index("ix_evolution_records_status",
                  table_name="evolution_records")
    op.drop_index("ix_evolution_records_level", table_name="evolution_records")
    op.drop_index("ix_evolution_records_company_id",
                  table_name="evolution_records")
    op.drop_table("evolution_records")

    op.drop_index("ix_ceo_playbooks_company_active", table_name="ceo_playbooks")
    op.drop_index("ix_ceo_playbooks_evolution_id", table_name="ceo_playbooks")
    op.drop_index("ix_ceo_playbooks_active", table_name="ceo_playbooks")
    op.drop_index("ix_ceo_playbooks_agent_id", table_name="ceo_playbooks")
    op.drop_index("ix_ceo_playbooks_company_id", table_name="ceo_playbooks")
    op.drop_table("ceo_playbooks")
