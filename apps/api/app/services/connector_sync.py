"""连接器同步编排（V6 M3）：拉取 → 规整层 upsert → 同步账。

调度入口：路由手动「立即同步」；scheduler_engine 的 automation 可挂
本函数做增量定时（kind=TASK_TEMPLATE 之外的拉取型任务，M4 接调度）。
失败语义：一次页内网络失败 = 本次同步失败，水位不推进（下次重拉同批，
幂等 upsert 保证不翻倍）；部分成功页数已 upsert 的行照常保留。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.canonical import ConnectorSyncRun
from app.models.toolbus import ToolRegistry
from app.connectors import get_connector, upsert_records

logger = get_logger(__name__)


async def run_sync(db: AsyncSession, row: ToolRegistry, company_id: str,
                   *, limit: int = 500) -> ConnectorSyncRun:
    """对一条连接器工具行执行一次同步，返回同步账行（已 commit）。"""
    from datetime import datetime, timezone

    cfg = dict(row.config or {})
    kind = str(cfg.get("kind") or "rest")
    entity_type = str(cfg.get("entity_type") or "contact")
    watermark = (cfg.get("watermark") or None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    run = ConnectorSyncRun(company_id=company_id, tool_id=row.id, kind=kind,
                           entity_type=entity_type, status="running",
                           started_at=now)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        connector = get_connector(kind)
        if kind == "webhook":
            # push 型：无拉取动作，本次账记 success/0（数据由 ingest 落）
            run.status = "success"
            run.records_upserted = 0
            run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()
            return run
        result = await connector.pull(db, row, watermark, limit=limit)
        upserted = await upsert_records(
            db, company_id=company_id, source=row.id,
            entity_type=entity_type, records=result.records)
        run.status = "success"
        run.records_upserted = upserted
        if result.watermark:
            run.watermark = str(result.watermark)[:200]
            cfg["watermark"] = run.watermark  # 水位持久化回 config
            row.config = cfg
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        logger.info("连接器同步完成 company=%s tool=%s upserted=%d",
                    company_id, row.name, upserted)
        return run
    except Exception as exc:  # noqa: BLE001 — 同步失败如实入账，不炸调用方
        logger.warning("连接器同步失败 company=%s tool=%s: %s",
                       company_id, row.name, exc, exc_info=True)
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
        run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        return run


async def get_last_sync(db: AsyncSession, tool_id: str):
    return (await db.execute(
        select(ConnectorSyncRun).where(
            ConnectorSyncRun.tool_id == tool_id,
            ConnectorSyncRun.status.in_(("success", "failed")))
        .order_by(ConnectorSyncRun.created_at.desc()).limit(1))).scalars().first()


def sync_run_out(run: ConnectorSyncRun) -> dict:
    return {"id": run.id, "tool_id": run.tool_id, "kind": run.kind,
            "entity_type": run.entity_type, "status": run.status,
            "records_upserted": run.records_upserted,
            "watermark": run.watermark, "error": run.error,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": (run.finished_at.isoformat()
                            if run.finished_at else None)}


__all__ = ["run_sync", "get_last_sync", "sync_run_out"]
