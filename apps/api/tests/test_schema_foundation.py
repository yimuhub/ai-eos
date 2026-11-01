"""新 schema 地基护栏：队列 / 交付评论 / 订阅 / BYOK 密钥库。

这些表由后续功能共用，模型与迁移一旦漂移（注册漏了、索引没建、
迁移没跟上），故障只会出现在升级后的老库上。这里把"模型 == 元数据 ==
迁移链"钉成断言，全部走 conftest 的临时 SQLite。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 — 先注册全部模型
from app.db.base import Base
from app.db.session import AsyncSessionLocal
from app.models import (Artifact, ArtifactStatus, CompanySecret,  # noqa: F401
                        CompanySubscription, Conversation, DeliverableComment,
                        QueueItem, QueueItemStatus, SubscriptionStatus)

API_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_MIGRATIONS = API_ROOT / "migrations"
LEGACY_MIGRATIONS = API_ROOT / "alembic"

NEW_TABLES = ("queue_items", "deliverable_comments",
              "company_subscriptions", "company_secrets",
              "push_subscriptions", "thread_reads")


def _indexes(table_name: str) -> dict[str, tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {idx.name: tuple(c.name for c in idx.columns) for idx in table.indexes}


def _fk(table_name: str, column: str):
    for fk in Base.metadata.tables[table_name].foreign_keys:
        if fk.parent.name == column:
            return fk
    raise AssertionError(f"{table_name}.{column} 缺少外键")


def _load_revision(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"revision_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_dir(root: Path) -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    return ScriptDirectory.from_config(cfg)


def test_new_tables_are_registered_in_metadata():
    for name in NEW_TABLES:
        assert name in Base.metadata.tables, f"{name} 未被 app.models 注册"
    assert QueueItem.__tablename__ == "queue_items"
    assert DeliverableComment.__tablename__ == "deliverable_comments"
    assert CompanySubscription.__tablename__ == "company_subscriptions"
    assert CompanySecret.__tablename__ == "company_secrets"
    assert "push_subscriptions" in NEW_TABLES and "thread_reads" in NEW_TABLES


def test_queue_items_schema():
    table = Base.metadata.tables["queue_items"]
    assert set(table.c.keys()) == {
        "id", "company_id", "kind", "payload", "status", "attempts",
        "max_attempts", "available_at", "lease_expires_at", "claimed_by",
        "dedupe_key", "last_error", "created_at", "updated_at",
    }
    assert table.c.id.type.length == 36
    assert table.c.kind.type.length == 64
    assert table.c.payload.nullable is False
    assert table.c.status.type.length == 20
    assert set(table.c.status.type.enums) == {s.value for s in QueueItemStatus}
    assert table.c.available_at.nullable is False
    assert table.c.lease_expires_at.nullable is True
    assert _indexes("queue_items") == {
        "ix_queue_items_company_id": ("company_id",),
        "ix_queue_items_kind": ("kind",),
        "ix_queue_items_status": ("status",),
        "ix_queue_items_available_at": ("available_at",),
        "ix_queue_items_lease_expires_at": ("lease_expires_at",),
        "ix_queue_items_dedupe_key": ("dedupe_key",),
    }


def test_artifact_acceptance_columns_keep_status_machine():
    table = Artifact.__table__
    for column in ("task_id", "accepted_by", "accepted_at"):
        assert column in table.c
        assert table.c[column].nullable is True
    fk = _fk("artifacts", "task_id")
    assert fk.target_fullname == "tasks.id"
    assert fk.ondelete == "SET NULL"
    assert "ix_artifacts_task_id" in _indexes("artifacts")
    # 验收记录在列上，不允许靠新增状态值表达
    assert {s.value for s in ArtifactStatus} == {
        "DRAFT", "IN_REVIEW", "APPROVED", "REJECTED", "ARCHIVED"}


def test_conversation_project_link():
    table = Conversation.__table__
    assert table.c.project_id.nullable is True
    fk = _fk("conversations", "project_id")
    assert fk.target_fullname == "projects.id"
    assert fk.ondelete == "SET NULL"
    assert "ix_conversations_project_id" in _indexes("conversations")


def test_deliverable_comments_schema():
    table = Base.metadata.tables["deliverable_comments"]
    assert set(table.c.keys()) == {
        "id", "company_id", "artifact_id", "author_type", "author_id",
        "body", "created_at",
    }
    assert table.c.body.nullable is False
    assert table.c.author_type.type.length == 20
    fk = _fk("deliverable_comments", "artifact_id")
    assert fk.target_fullname == "artifacts.id"
    assert fk.ondelete == "CASCADE"
    assert _indexes("deliverable_comments") == {
        "ix_deliverable_comments_company_id": ("company_id",),
        "ix_deliverable_comments_artifact_id": ("artifact_id",),
    }


def test_company_subscriptions_schema():
    table = Base.metadata.tables["company_subscriptions"]
    assert table.c.company_id.nullable is False
    assert table.c.plan_code.default.arg == "trial"
    assert table.c.status.type.length == 20
    assert set(table.c.status.type.enums) == {s.value for s in SubscriptionStatus}
    for column in ("trial_ends_at", "period_start", "period_end",
                   "seats_limit", "monthly_token_quota"):
        assert table.c[column].nullable is True
    fk = _fk("company_subscriptions", "company_id")
    assert fk.target_fullname == "companies.id"
    assert fk.ondelete == "CASCADE"
    company_index = next(i for i in table.indexes
                         if i.name == "ix_company_subscriptions_company_id")
    assert company_index.unique is True


def test_company_secrets_store_ciphertext_only():
    table = Base.metadata.tables["company_secrets"]
    assert set(table.c.keys()) == {
        "id", "company_id", "name", "ciphertext", "created_at", "updated_at",
    }
    assert not {"plaintext", "secret", "value"} & set(table.c.keys())
    uniques = {c.name: tuple(col.name for col in c.columns)
               for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert uniques["uq_company_secret_name"] == ("company_id", "name")


async def test_subscription_and_queue_item_defaults_via_session(tenants):
    company_id = tenants.alice_co["id"]
    async with AsyncSessionLocal() as session:
        subscription = CompanySubscription(company_id=company_id)
        item = QueueItem(kind="task.execute", company_id=company_id)
        session.add_all([subscription, item])
        await session.commit()
        await session.refresh(subscription)
        await session.refresh(item)

    assert len(subscription.id) == 36
    assert subscription.plan_code == "trial"
    assert subscription.status is SubscriptionStatus.TRIALING
    assert subscription.created_at is not None
    assert subscription.updated_at is not None

    assert len(item.id) == 36
    assert item.payload == {}
    assert item.status is QueueItemStatus.QUEUED
    assert (item.attempts, item.max_attempts) == (0, 3)
    assert item.available_at is not None
    assert item.available_at.tzinfo is None


async def test_company_secret_name_unique_per_company(tenants):
    company_id = tenants.alice_co["id"]
    async with AsyncSessionLocal() as session:
        session.add(CompanySecret(company_id=company_id, name="openai",
                                  ciphertext="v1"))
        await session.commit()

    async with AsyncSessionLocal() as session:
        session.add(CompanySecret(company_id=company_id, name="openai",
                                  ciphertext="v2"))
        with pytest.raises(IntegrityError):
            await session.commit()


def test_thread_reads_scope_is_unique():
    """跨设备合并依赖 (user, company, thread_key) 唯一；缺了它并发写会出重复行。"""
    table = Base.metadata.tables["thread_reads"]
    assert set(table.c.keys()) == {
        "id", "user_id", "company_id", "thread_key", "read_at_ms",
        "updated_at",
    }
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in table.constraints if isinstance(c, UniqueConstraint)
    }
    assert uniques["uq_thread_reads_scope"] == ("user_id", "company_id",
                                                "thread_key")
    assert "ix_thread_reads_user_company" in _indexes("thread_reads")


def test_migration_revisions_extend_current_heads():
    active = _script_dir(ACTIVE_MIGRATIONS)
    assert active.get_heads() == ["0008"]
    assert active.get_revision("0008").down_revision == "0007"
    assert active.get_revision("0007").down_revision == "0006"
    assert active.get_revision("0006").down_revision == "0005"
    assert active.get_revision("0005").down_revision == "0004"
    assert active.get_revision("0004").down_revision == "0003"
    assert active.get_revision("0003").down_revision == "0002"
    revision_file = ACTIVE_MIGRATIONS / "versions" / "0003_schema_foundation.py"
    module = _load_revision(revision_file)
    assert module.revision == "0003"
    assert callable(module.upgrade) and callable(module.downgrade)

    for revision, filename in (
        ("0004", "0004_push_subscriptions.py"),
        ("0005", "0005_thread_reads.py"),
        ("0006", "0006_ceo_missions.py"),
        ("0007", "0007_ceo_auto_hire.py"),
        ("0008", "0008_workflow_authoring.py"),
    ):
        path = ACTIVE_MIGRATIONS / "versions" / filename
        loaded = _load_revision(path)
        assert loaded.revision == revision
        assert callable(loaded.upgrade) and callable(loaded.downgrade)

    legacy = _script_dir(LEGACY_MIGRATIONS)
    assert legacy.get_heads() == ["c8a71f5d3e42"]
    assert legacy.get_revision("c8a71f5d3e42").down_revision == "b2d4f6a8c1e3"
    legacy_module = _load_revision(
        LEGACY_MIGRATIONS / "versions" / "c8a71f5d3e42_schema_foundation.py")
    assert callable(legacy_module.upgrade) and callable(legacy_module.downgrade)

    for table in NEW_TABLES:
        assert table in Base.metadata.tables
