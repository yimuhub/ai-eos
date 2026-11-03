"""远程执行设备与命令下发队列

新增 `computer_devices`：登记可被操作的其他机器（sidecar）。接入码与设备密钥
都只在库里留 SHA-256，明文只在创建/接入那一次返回。
新增 `computer_dispatches`：后端判定通过后交给设备执行的命令，状态机
pending → claimed → succeeded/failed，超时惰性判 expired。
`computer_policies` 补 `default_device_id`：公司级别的默认执行机器，
为空保持改造前的「后端本机直连」。

Revision ID: 0015
Revises: 0014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "computer_devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=True),
        sa.Column("hostname", sa.String(length=120), nullable=True),
        sa.Column("agent_version", sa.String(length=40), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("secret_hash", sa.String(length=64), nullable=True),
        sa.Column("enroll_code_hash", sa.String(length=64), nullable=True),
        sa.Column("enroll_expires_at", sa.DateTime(), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", name="uq_device_company_name"),
    )
    op.create_index("ix_computer_devices_company_id",
                    "computer_devices", ["company_id"])
    op.create_index("ix_computer_devices_secret_hash", "computer_devices",
                    ["secret_hash"], unique=True)
    op.create_index("ix_computer_devices_enroll_code_hash", "computer_devices",
                    ["enroll_code_hash"], unique=True)

    op.create_table(
        "computer_dispatches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("capability", sa.String(length=40), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("log_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["computer_devices.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_computer_dispatches_company_id",
                    "computer_dispatches", ["company_id"])
    op.create_index("ix_computer_dispatches_device_id",
                    "computer_dispatches", ["device_id"])
    op.create_index("ix_dispatch_claim", "computer_dispatches",
                    ["device_id", "status", "created_at"])

    with op.batch_alter_table("computer_policies") as batch_op:
        batch_op.add_column(sa.Column("default_device_id",
                                      sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("computer_policies") as batch_op:
        batch_op.drop_column("default_device_id")

    op.drop_index("ix_dispatch_claim", table_name="computer_dispatches")
    op.drop_index("ix_computer_dispatches_device_id",
                  table_name="computer_dispatches")
    op.drop_index("ix_computer_dispatches_company_id",
                  table_name="computer_dispatches")
    op.drop_table("computer_dispatches")

    op.drop_index("ix_computer_devices_enroll_code_hash",
                  table_name="computer_devices")
    op.drop_index("ix_computer_devices_secret_hash", table_name="computer_devices")
    op.drop_index("ix_computer_devices_company_id", table_name="computer_devices")
    op.drop_table("computer_devices")
