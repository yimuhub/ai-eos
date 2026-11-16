"""数据规整层与指标层（V6 M3）：Canonical 实体 / 自定义字段 / 指标 / 同步账

新增四张表：canonical_records（统一实体，AI 取数的事实源）、
custom_field_defs（企业自定义字段底座）、metric_defs（结构化指标口径）、
connector_sync_runs（连接器同步账）。对应模型 `app/models/canonical.py`。

Revision ID: 0021
Revises: 0020
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, Sequence[str], None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts_cols():
    return (
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "canonical_records",
        *_ts_cols(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("custom_data", sa.JSON(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "entity_type", "source",
                            "external_id", name="uq_canonical_source_id"),
    )
    op.create_index("ix_canonical_records_company_id", "canonical_records",
                    ["company_id"])
    op.create_index("ix_canonical_records_entity_type", "canonical_records",
                    ["entity_type"])
    op.create_index("ix_canonical_records_source", "canonical_records",
                    ["source"])

    op.create_table(
        "custom_field_defs",
        *_ts_cols(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False,
                  server_default="text"),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "entity_type", "key",
                            name="uq_custom_field"),
    )
    op.create_index("ix_custom_field_defs_company_id", "custom_field_defs",
                    ["company_id"])
    op.create_index("ix_custom_field_defs_entity_type", "custom_field_defs",
                    ["entity_type"])

    op.create_table(
        "metric_defs",
        *_ts_cols(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False,
                  server_default=""),
        sa.Column("direction", sa.String(length=20), nullable=False,
                  server_default="higher_better"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("required_capability", sa.String(length=20), nullable=False,
                  server_default="view"),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_metric_key"),
    )
    op.create_index("ix_metric_defs_company_id", "metric_defs", ["company_id"])
    op.create_index("ix_metric_defs_key", "metric_defs", ["key"])

    op.create_table(
        "connector_sync_runs",
        *_ts_cols(),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False,
                  server_default="rest"),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="running"),
        sa.Column("records_upserted", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("watermark", sa.String(length=200), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_sync_runs_company_id",
                    "connector_sync_runs", ["company_id"])
    op.create_index("ix_connector_sync_runs_tool_id", "connector_sync_runs",
                    ["tool_id"])


def downgrade() -> None:
    op.drop_index("ix_connector_sync_runs_tool_id",
                  table_name="connector_sync_runs")
    op.drop_index("ix_connector_sync_runs_company_id",
                  table_name="connector_sync_runs")
    op.drop_table("connector_sync_runs")
    op.drop_index("ix_metric_defs_key", table_name="metric_defs")
    op.drop_index("ix_metric_defs_company_id", table_name="metric_defs")
    op.drop_table("metric_defs")
    op.drop_index("ix_custom_field_defs_entity_type",
                  table_name="custom_field_defs")
    op.drop_index("ix_custom_field_defs_company_id",
                  table_name="custom_field_defs")
    op.drop_table("custom_field_defs")
    op.drop_index("ix_canonical_records_source", table_name="canonical_records")
    op.drop_index("ix_canonical_records_entity_type",
                  table_name="canonical_records")
    op.drop_index("ix_canonical_records_company_id",
                  table_name="canonical_records")
    op.drop_table("canonical_records")
