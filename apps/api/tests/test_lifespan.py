"""启动/关闭钩子（lifespan）必须真的被执行到。

## 为什么这组用例存在

conftest 用 `httpx.ASGITransport(app=...)` 直连应用，**不会触发 lifespan**。
因此"启动时建表 / 补列 / 启动调度器"这些只在真实启动时发生的副作用，
此前一条测试都没跑过——它们坏了也不会有人知道，用户一启动就炸。

这里显式进入 `router.lifespan_context`，把关键副作用钉住：
启动后调度器在跑、新增列已补齐；退出后调度器已停止。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def test_lifespan_runs_startup_and_shutdown():
    from app import scheduler_engine
    from app.db.session import engine
    from app.main import app as asgi_app

    # 进入前：调度器未启动（上一个用例结束后应处于停止态）
    assert scheduler_engine._loop_task is None or scheduler_engine._loop_task.done()

    async with asgi_app.router.lifespan_context(asgi_app):
        # 启动副作用 1：调度循环在跑
        assert scheduler_engine._loop_task is not None
        assert not scheduler_engine._loop_task.done(), "lifespan 未启动调度器"

        # 启动副作用 2：schema 已就绪（ensure_columns 幂等补齐后可查询）
        async with engine.connect() as conn:
            n = (await conn.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='table'"))).scalar()
        assert n and n > 20, f"启动后表数量异常：{n}"

    # 关闭副作用：调度循环被取消（cancel 是异步的，让出一拍再断言）
    import asyncio

    await asyncio.sleep(0.05)
    assert scheduler_engine._loop_task is None or scheduler_engine._loop_task.done()


async def test_lifespan_ensure_columns_is_idempotent():
    """二次启动不应报错（升级后老库补列的核心保证）。"""
    from app.main import app as asgi_app

    async with asgi_app.router.lifespan_context(asgi_app):
        pass
    async with asgi_app.router.lifespan_context(asgi_app):
        pass
