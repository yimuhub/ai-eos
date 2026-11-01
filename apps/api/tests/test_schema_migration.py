"""升级不炸：老数据库必须能被自动补齐。

## 为什么单独守护这件事

`Base.metadata.create_all` 只建缺失的**表**，不会给已存在的表补新列。
这意味着"给模型加一个字段"在开发机上永远是对的（每次跑测试都是新建库），
一到用户机器上就变成 `no such column`——而且只在真正用到该字段时才报错。

这类问题在本地测试里**天然是隐形的**，只有专门造一个"老库"才能覆盖。

注：这里用标准库 `tempfile` 而不是 pytest 的 `tmp_path`，
以免测试对环境（沙箱、目录权限）产生额外依赖。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrations import ensure_columns

pytestmark = pytest.mark.asyncio

# 模拟改造前的老库：有 agent_executions 表，但没有 completed_at 列
_LEGACY_DDL = """
CREATE TABLE agent_executions (
    id VARCHAR(36) PRIMARY KEY,
    agent_id VARCHAR(36),
    task_id VARCHAR(36),
    project_id VARCHAR(36),
    status VARCHAR(20),
    input TEXT,
    output TEXT,
    error TEXT,
    metadata_json JSON,
    created_at VARCHAR(50),
    updated_at VARCHAR(50)
)
"""


@asynccontextmanager
async def legacy_engine(ddl: str = _LEGACY_DDL):
    """一个只含"老表结构"的临时 SQLite 库。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aieos_migrate_"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(ddl))
        yield engine
    finally:
        await engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


async def _columns(engine, table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns(table)})


async def test_missing_nullable_column_is_added():
    """老库缺列时，启动补列必须真的把列加上。"""
    async with legacy_engine() as engine:
        assert "completed_at" not in await _columns(engine, "agent_executions")

        added = await ensure_columns(engine)

        assert "agent_executions.completed_at" in added, \
            f"未补齐模型新增的列，实际补齐：{added}"
        assert "completed_at" in await _columns(engine, "agent_executions")


async def test_ensure_columns_is_idempotent():
    """重复启动不能反复 ALTER，也不能报错。"""
    async with legacy_engine() as engine:
        await ensure_columns(engine)
        second = await ensure_columns(engine)
        assert second == [], f"第二次补列不应再改动任何东西：{second}"


async def test_existing_data_survives():
    """补列的完整意义是"不动数据"。"""
    async with legacy_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO agent_executions (id, agent_id, task_id, status, output)"
                " VALUES ('e1', 'a1', 't1', 'completed', '原有产出')"))

        await ensure_columns(engine)

        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, status, output, completed_at FROM agent_executions"
            ))).one()
        assert row.id == "e1"
        assert row.status == "completed"
        assert row.output == "原有产出"
        assert row.completed_at is None


async def test_tables_absent_from_database_are_left_alone():
    """库里没有的表不该被这个函数顺手建出来——建表是 create_all 的职责。

    否则一旦补列逻辑出错，会连带产生"半张表"，问题更难查。
    """
    async with legacy_engine() as engine:
        added = await ensure_columns(engine)
        assert all(name.startswith("agent_executions.") for name in added), \
            f"补列函数越界改动了其它表：{added}"
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert tables == {"agent_executions"}, f"凭空多出了表：{tables}"


_WORKFLOW_LEGACY_DDL = """
CREATE TABLE workflow_definitions (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(36),
    name VARCHAR(200),
    status VARCHAR(20),
    created_by VARCHAR(36)
)
"""


async def test_workflow_version_columns_backfill_unambiguous_defaults():
    """0008 给老流程补版本/计数列：老行填 1/0 没有歧义，必须自动补齐。"""
    async with legacy_engine(_WORKFLOW_LEGACY_DDL) as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO workflow_definitions (id, company_id, name, status)"
                " VALUES ('w1', 'c1', '旧流程', 'PUBLISHED')"))

        added = await ensure_columns(engine)

        for col in ("version", "source", "run_count", "success_count",
                    "failure_count"):
            assert f"workflow_definitions.{col}" in added, \
                f"未补齐 {col}，实际补齐：{added}"
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT version, source, run_count, success_count, failure_count"
                " FROM workflow_definitions WHERE id = 'w1'"))).one()
        assert row.version == 1
        assert row.source == "manual"
        assert (row.run_count, row.success_count, row.failure_count) == (0, 0, 0)


@asynccontextmanager
async def full_schema_engine():
    """建出完整表结构（模拟新版本库），供"删一列再补回来"使用。"""
    from app.db.base import Base

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aieos_migrate_full_"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp / 'full.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c))
        yield engine
    finally:
        await engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


async def test_non_nullable_json_column_is_added_with_empty_default():
    """`Mapped[list] = mapped_column(JSON, default=list)` 这类非空列也要补。

    这类列（公司/项目/自动化的附件列表）在模型里是 nullable=False，早先的
    补列规则一律跳过——老库一读 /companies 就 `no such column`，用户升级后
    直接登录不进去。语义上"没有附件"只有一种答案：空集合。
    """
    async with full_schema_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO companies (id, owner_id, name, attachments)"
                " VALUES ('c1', 'u1', '老公司', '[]')"))
            # 模拟老库：这一列根本不存在（老行数据保留）
            await conn.execute(text("ALTER TABLE companies DROP COLUMN attachments"))

        added = await ensure_columns(engine)

        assert "companies.attachments" in added, f"非空 JSON 列未补上：{added}"
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT name, attachments FROM companies WHERE id='c1'"))).one()
            ddl = (await conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='companies'"
            ))).scalar()
        assert row.name == "老公司", "补列不能动原有数据"
        val = row.attachments.strip() if isinstance(row.attachments, str) else row.attachments
        assert val == [] or val == "[]", f"老行必须回落到空集合，实际：{row.attachments!r}"
        assert "DEFAULT '[]'" in (ddl or ""), f"补列时必须带上默认值，DDL：{ddl}"


async def test_non_nullable_non_json_column_still_requires_human():
    """没有机械默认值的非空列仍要留给人决策，不能被自动填一个假值。"""
    from app.db.migrations import _auto_addable

    class FakeType:
        def compile(self, dialect=None):
            return "INTEGER"

    class FakeColumn:
        name = "amount"
        nullable = False
        primary_key = False
        type = FakeType()
        default = None

    assert _auto_addable(FakeColumn()) is False

