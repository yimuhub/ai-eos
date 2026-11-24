"""启动/关闭钩子（lifespan）必须真的被执行到。

## 为什么这组用例存在

conftest 用 `httpx.ASGITransport(app=...)` 直连应用，**不会触发 lifespan**。
因此"启动时校验数据库迁移 / 启动调度器"这些只在真实启动时发生的副作用，
必须显式进入 `router.lifespan_context` 验证。

这里钉住：启动后调度器在跑、Alembic 版本与结构就绪；退出后调度器已停止。
"""
from __future__ import annotations

import asyncio

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

        # 启动副作用 2：Alembic 版本与结构已就绪
        async with engine.connect() as conn:
            n = (await conn.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='table'"))).scalar()
        assert n and n > 20, f"启动后表数量异常：{n}"

    # 关闭副作用：调度循环被取消（cancel 是异步的，让出一拍再断言）
    import asyncio

    await asyncio.sleep(0.05)
    assert scheduler_engine._loop_task is None or scheduler_engine._loop_task.done()


async def test_lifespan_migration_verification_is_idempotent():
    """二次启动必须通过同一版本与结构校验，不得重复改表。"""
    from app.main import app as asgi_app

    async with asgi_app.router.lifespan_context(asgi_app):
        pass
    async with asgi_app.router.lifespan_context(asgi_app):
        pass


@pytest.mark.parametrize("exit_kind", ["exception", "cancel"])
async def test_lifespan_always_cleans_recovery_scheduler_and_mdns(monkeypatch, exit_kind):
    from app import main, mdns_advertise, scheduler_engine
    from app.api.routes import evals as eval_routes

    stopped = []
    recovery_tasks = []
    recovery_started = asyncio.Event()
    monkeypatch.setattr(scheduler_engine, "start_scheduler", lambda: None)
    monkeypatch.setattr(scheduler_engine, "stop_scheduler", lambda: stopped.append("scheduler"))
    monkeypatch.setattr(mdns_advertise, "start", lambda: None)
    monkeypatch.setattr(mdns_advertise, "stop", lambda: stopped.append("mdns"))

    async def tracked_recovery():
        recovery_tasks.append(asyncio.current_task())
        recovery_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(eval_routes, "run_periodic_eval_recovery", tracked_recovery)
    entered = asyncio.Event()

    async def run():
        async with main.app.router.lifespan_context(main.app):
            await asyncio.wait_for(recovery_started.wait(), timeout=1)
            assert len(recovery_tasks) == 1
            recovery = recovery_tasks[0]
            assert recovery is not None and not recovery.done()
            entered.set()
            if exit_kind == "exception":
                raise RuntimeError("application failed")
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=15)
    if exit_kind == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="application failed"):
            await task

    assert recovery_tasks[0].done()
    assert stopped == ["scheduler", "mdns"]
