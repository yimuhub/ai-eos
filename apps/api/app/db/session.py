"""Async database engine and session factory.

SQLite 是默认（便携）部署形态，并发正确性不能依赖 sqlite3 的默认行为：

* ``busy_timeout`` 默认只有 5 秒，长事务/后台任务同时写时立刻抛
  ``database is locked``；这里统一抬到 15 秒，并与连接级
  ``timeout=15`` 对齐。
* ``foreign_keys`` 是**连接级**开关（每个新连接都要重开）。
  只在主 engine 上挂监听器、后台任务另建 engine 时漏挂，会让后台
  连接的外键约束静默失效——外键只写不校验，脏数据一路落库。

因此把 PRAGMA 收敛到 ``apply_sqlite_pragmas()`` 一处，
主 engine 与后台任务 engine 都通过 ``register_sqlite_pragmas()`` 复用。
"""
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_S = 15


def apply_sqlite_pragmas(dbapi_conn) -> None:
    """在一条新建的 SQLite 连接上设置统一 PRAGMA。

    幂等：SQLAlchemy 的 ``connect`` 事件对每个物理连接触发一次，
    重复执行只会把同样的值再设一遍。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        # 外键约束必须逐连接开启，否则后台/临时 engine 上的外键形同虚设
        cur.execute("PRAGMA foreign_keys=ON")
        # 写锁竞争时最多等待 15 秒再报 "database is locked"
        cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cur.close()


def _on_sqlite_connect(dbapi_conn, _connection_record) -> None:
    apply_sqlite_pragmas(dbapi_conn)


def register_sqlite_pragmas(async_engine) -> bool:
    """给 async engine 挂上 PRAGMA 监听器（非 SQLite 方言直接跳过）。

    返回是否挂载/已挂载。用 ``event.contains`` 去重，后台任务每次新建
    engine 时重复调用也安全。
    """
    if async_engine.dialect.name != "sqlite":
        return False
    sync_engine = async_engine.sync_engine
    if not event.contains(sync_engine, "connect", _on_sqlite_connect):
        event.listen(sync_engine, "connect", _on_sqlite_connect)
    return True


_is_sqlite = settings.database_url.startswith("sqlite")
_engine_kwargs: dict = {"echo": settings.sql_echo}
if _is_sqlite:
    # sqlite3 连接级超时：与 PRAGMA busy_timeout 保持同一量级，
    # 避免"PRAGMA 还没生效前"的首次加锁仍按 5 秒放弃。
    _engine_kwargs["connect_args"] = {"timeout": SQLITE_CONNECT_TIMEOUT_S}
else:
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(settings.database_url, **_engine_kwargs)
register_sqlite_pragmas(engine)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
