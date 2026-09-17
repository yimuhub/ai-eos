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
async def legacy_engine():
    """一个只含"老表结构"的临时 SQLite 库。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aieos_migrate_"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_LEGACY_DDL))
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
