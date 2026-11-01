"""Alembic 环境：离线/在线、同步/异步方言统一入口。

## 目标库的选择顺序（谁先命中用谁）

1. ``alembic -x db_url=...``：显式命令行参数，优先级最高（演练脚本用它）。
2. ``ALEMBIC_DATABASE_URL`` 环境变量。
3. ``alembic.ini`` 里的 ``sqlalchemy.url``（非占位符时）。
4. ``app.core.config.settings.database_url``：兜底，保证部署脚本里
   裸跑 ``alembic upgrade head`` 仍然操作"应用当前配置的那个库"。

## 为什么保留 async 分支

应用运行期用的是 ``sqlite+aiosqlite`` / ``postgresql+asyncpg``，
Alembic 官方模板对这两种 URL 走 ``async_engine_from_config`` + ``run_sync``；
而测试与 CI 为了方便会用同步 URL（``sqlite:///...``）。两条分支都支持，
避免"换个 URL 方案就报 MissingGreenlet / 需要 aiosqlite"。

## 安全边界

本文件**绝不**在建表之外做任何事：不读业务数据、不写业务数据、
不在 import 阶段连接数据库。目标 URL 在 import 应用模型之前写入
``DATABASE_URL``，这样应用模块里任何"顺手创建的 engine"也只会指向
本次迁移的目标库，不会误连真实库。
"""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, make_url, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# 1) 让 `app` 可导入：不依赖调用者的 cwd
# ---------------------------------------------------------------------------
API_ROOT = Path(__file__).resolve().parent.parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

config = context.config

# 日志：只在通过 alembic.ini 调用时生效；测试里 Config 可能没有关联文件
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_PLACEHOLDER_URLS = {"", "driver://user:pass@localhost/dbname"}


def resolve_database_url() -> str:
    """按"显式 > 环境变量 > ini > 应用配置"的顺序确定目标库。"""
    x_args = context.get_x_argument(as_dictionary=True)
    url = (x_args.get("db_url") or "").strip()
    if url:
        return url

    url = (os.environ.get("ALEMBIC_DATABASE_URL") or "").strip()
    if url:
        return url

    url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if url not in _PLACEHOLDER_URLS:
        return url

    # 兜底：应用当前配置。注意这会读取 .env，但只解析 URL，不建立连接。
    from app.core.config import settings  # 延迟导入，避免无谓副作用

    return settings.database_url


DATABASE_URL = resolve_database_url()


def _as_async_url(url: str) -> str:
    """把同步 URL 换成等价的 async driver，仅用于喂给应用侧环境变量。

    app.db.session 在 import 阶段就 `create_async_engine(settings.database_url)`，
    如果这里把 DATABASE_URL 写成 `sqlite:///...`，应用模块一被 import 就会抛
    "The asyncio extension requires an async driver"。换成 aiosqlite/asyncpg
    后既保持同一个目标库，又不会让惰性 engine 的构造失败。
    """
    try:
        url_obj = make_url(url)
        if url_obj.get_dialect().is_async:
            return url
        async_driver = {"sqlite": "aiosqlite", "postgresql": "asyncpg"}.get(
            url_obj.get_backend_name())
        if async_driver is None:
            return url
        return url_obj.set(
            drivername=f"{url_obj.get_backend_name()}+{async_driver}"
        ).render_as_string(hide_password=False)
    except Exception:  # noqa: BLE001 — URL 解析失败时保持原样，让 Alembic 报原始错误
        return url


# 让"迁移进程里 import 到的应用代码"和 Alembic 操作同一个库。
# 必须在 import app.models 之前设置（engine 在 import 阶段创建，虽然
# create_async_engine 是惰性的，但目标 URL 应当一致）。
os.environ["DATABASE_URL"] = _as_async_url(DATABASE_URL)

config.set_main_option("sqlalchemy.url", DATABASE_URL)

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401 — 全部模型注册到 Base.metadata

# 历史遗留：下面这些表不是定义在 app/models/ 里，而是散落在路由/引擎模块
# （agent_chat_messages / group_chat_messages / learning_plans / doc_styles
# 定义在 app/api/routes/*.py，runtime_locks 定义在 app/task_engine.py）。
# 运行时 app/main.py 导入这些路由后 create_all 会把它们建出来，所以基线
# 必须包含；漏掉它们会出现"运行时建了表、alembic 基线没有"的分裂。
# 新增模型请放 app/models/ 并在 __init__.py 注册，别再往路由里塞表。
import app.task_engine  # noqa: E402,F401 — runtime_locks
import app.api.routes.agent_chat  # noqa: E402,F401 — agent_chat_messages
import app.api.routes.group_chat  # noqa: E402,F401 — group_chat_messages
import app.api.routes.growth_center  # noqa: E402,F401 — learning_plans
import app.api.routes.doc_styles  # noqa: E402,F401 — doc_styles

target_metadata = Base.metadata


def _is_async_url(url: str) -> bool:
    try:
        return bool(make_url(url).get_dialect().is_async)
    except Exception:  # noqa: BLE001 — 未知方言交给 SQLAlchemy 自己报错
        return "+aiosqlite" in url or "+asyncpg" in url


def _configure(connection: Connection | None, url: str) -> None:
    """统一的 context.configure：SQLite 打开 batch 模式以便 ALTER 重建表。"""
    dialect_name = make_url(url).get_backend_name()
    context.configure(
        connection=connection,
        url=url if connection is None else None,
        target_metadata=target_metadata,
        # SQLite 不支持 ALTER COLUMN，后续迁移必须走 batch 重建表
        render_as_batch=(dialect_name == "sqlite"),
        # 类型变更也参与 autogenerate：否则 String(36)→String(64) 静默漏掉
        compare_type=True,
        # 不比较 server_default：SQLite 会把 func.now() 存成 CURRENT_TIMESTAMP，
        # 与模型里的文本形式对不上，会产生永远消不掉的假 diff。
        compare_server_default=False,
        literal_binds=(connection is None),
        dialect_opts={"paramstyle": "named"} if connection is None else {},
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连库。"""
    _configure(None, DATABASE_URL)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection, DATABASE_URL)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=DATABASE_URL,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    if _is_async_url(DATABASE_URL):
        asyncio.run(run_async_migrations())
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=DATABASE_URL,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
