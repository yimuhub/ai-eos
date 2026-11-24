"""Align team loadout creator foreign key with the ORM model.

Revision ID: 0040
Revises: 0039
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, Sequence[str], None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("team_loadouts") as batch_op:
            batch_op.create_foreign_key(
                "fk_team_loadouts_created_by_users", "users",
                ["created_by"], ["id"])
    else:
        op.create_foreign_key(
            "fk_team_loadouts_created_by_users", "team_loadouts", "users",
            ["created_by"], ["id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("team_loadouts") as batch_op:
            batch_op.drop_constraint(
                "fk_team_loadouts_created_by_users", type_="foreignkey")
    else:
        op.drop_constraint(
            "fk_team_loadouts_created_by_users", "team_loadouts",
            type_="foreignkey")
