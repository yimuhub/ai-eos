"""模型供应商与模型类型

新增 `model_providers`：一个供应商保存地址、密钥变量名与默认调用配置，
其下挂多个 `models`。升级前的模型没有供应商这一层，迁移时按
(company_id, provider_type, base_url, api_key_env) 归并出供应商并回填
`models.provider_id`，老数据在界面上不会变成孤岛。

`models` 新增列全部可空：
* provider_id / model_type / multimodal / capabilities / enabled /
  config_source / upstream_meta / last_synced_at。
老行按「文本模型、默认启用」回填；其余字段保持 NULL，读取侧有等价语义。

Revision ID: 0012
Revises: 0011
"""
import uuid
from typing import Sequence, Union
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROVIDER_LABELS = {
    "openai_compat": "OpenAI 兼容",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
}


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("api_key_env", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(), nullable=True),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_providers_company_id", "model_providers",
                    ["company_id"])

    with op.batch_alter_table("models") as batch_op:
        batch_op.add_column(sa.Column("provider_id", sa.String(length=36),
                                      nullable=True))
        batch_op.add_column(sa.Column("model_type", sa.String(length=20),
                                      nullable=True))
        batch_op.add_column(sa.Column("multimodal", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("capabilities", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("enabled", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("config_source", sa.String(length=20),
                                      nullable=True))
        batch_op.add_column(sa.Column("upstream_meta", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("last_synced_at", sa.DateTime(),
                                      nullable=True))
        batch_op.create_foreign_key(
            "fk_models_provider_id", "model_providers",
            ["provider_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_models_provider_id", ["provider_id"])
        batch_op.create_index("ix_models_model_type", ["model_type"])

    _backfill_legacy_providers()


def _backfill_legacy_providers() -> None:
    """把升级前的直连模型按端点归并成供应商。

    不做这一步，老用户的模型会全部变成 `provider_id=NULL` 的孤岛：
    能聊天，但在新的「供应商 → 模型」界面里无处可管。
    """
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, company_id, provider_type, base_url, api_key_env "
        "FROM models WHERE company_id IS NOT NULL "
        "AND (provider_id IS NULL OR provider_id = '')")).mappings().all()
    groups: dict[tuple, dict] = {}
    for row in rows:
        ptype = row["provider_type"] or "openai_compat"
        key = (row["company_id"], ptype, row["base_url"] or "",
               row["api_key_env"] or "")
        group = groups.setdefault(key, {"model_ids": []})
        group["model_ids"].append(row["id"])

    for (company_id, ptype, base_url, api_key_env), group in groups.items():
        host = urlparse(base_url).netloc if base_url else ""
        label = _PROVIDER_LABELS.get(ptype, ptype)
        name = f"{label} · {host}" if host else label
        provider_id = str(uuid.uuid4())
        bind.execute(sa.text(
            "INSERT INTO model_providers "
            "(id, company_id, name, provider_type, base_url, api_key_env, "
            " enabled) "
            "VALUES (:id, :company_id, :name, :ptype, :base_url, :env, "
            "        :enabled)"),
            {"id": provider_id, "company_id": company_id, "name": name,
             "ptype": ptype, "base_url": base_url or None,
             "env": api_key_env or None, "enabled": True})
        bind.execute(sa.text(
            "UPDATE models SET provider_id = :pid, model_type = 'text', "
            "enabled = :enabled, config_source = 'default' "
            "WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)),
            {"pid": provider_id, "enabled": True,
             "ids": group["model_ids"]})


def downgrade() -> None:
    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_index("ix_models_model_type")
        batch_op.drop_index("ix_models_provider_id")
        batch_op.drop_constraint("fk_models_provider_id", type_="foreignkey")
        batch_op.drop_column("last_synced_at")
        batch_op.drop_column("upstream_meta")
        batch_op.drop_column("config_source")
        batch_op.drop_column("enabled")
        batch_op.drop_column("capabilities")
        batch_op.drop_column("multimodal")
        batch_op.drop_column("model_type")
        batch_op.drop_column("provider_id")

    op.drop_index("ix_model_providers_company_id", table_name="model_providers")
    op.drop_table("model_providers")
