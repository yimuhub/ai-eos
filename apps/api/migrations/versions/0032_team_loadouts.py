"""角色档案与阵容锁定（M3-B）

新增 `role_profiles` / `team_loadouts` 两张表，并给 `agents` 加可空的
`role_profile_id`。全部是新增，不改老列；老员工该列为 NULL，行为不变。

Revision ID: 0032
Revises: 0031
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, Sequence[str], None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "role_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=True),
        sa.Column("capability_tags", sa.JSON(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("default_gate", sa.JSON(), nullable=True),
        sa.Column("model_tier", sa.String(length=40), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default="custom"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_role_profiles_company_id", "role_profiles",
                    ["company_id"])
    op.create_index("ix_role_profiles_key", "role_profiles", ["key"])

    op.create_table(
        "team_loadouts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=True),
        sa.Column("default_inputs", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_team_loadouts_company_id", "team_loadouts",
                    ["company_id"])

    op.add_column("agents",
                  sa.Column("role_profile_id", sa.String(length=36), nullable=True))
    op.create_index("ix_agents_role_profile_id", "agents", ["role_profile_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_role_profile_id", table_name="agents")
    op.drop_column("agents", "role_profile_id")
    op.drop_index("ix_team_loadouts_company_id", table_name="team_loadouts")
    op.drop_table("team_loadouts")
    op.drop_index("ix_role_profiles_key", table_name="role_profiles")
    op.drop_index("ix_role_profiles_company_id", table_name="role_profiles")
    op.drop_table("role_profiles")
