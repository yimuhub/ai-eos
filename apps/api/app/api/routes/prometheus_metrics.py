"""Prometheus 文本格式指标端点（GET /metrics/prometheus）。

手写 Prometheus exposition 文本格式，不引入 prometheus_client 新依赖。
与 /metrics（业务 JSON，给人看）并存：本端点给抓取器看。
与 /health 一样不设鉴权，方便 Prometheus/探针直接抓取。
"""
from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.core.logging_config import get_logger
from app.core.metrics import render_prometheus
from app.db.session import engine

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])

_START_TIME = time.time()


async def _probe_db() -> tuple[bool, float]:
    """数据库探针：返回 (是否连通, 耗时秒)。失败只记日志，不抛异常。"""
    begin = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, time.perf_counter() - begin
    except Exception as e:
        logger.warning("prometheus db 探针失败: %s", e)
        return False, time.perf_counter() - begin


@router.get("/metrics/prometheus")
async def prometheus_metrics():
    db_up, db_latency = await _probe_db()
    now = time.time()
    lines = [
        "# HELP aieos_up AI-EOS API 进程是否存活（能抓到即为 1）。",
        "# TYPE aieos_up gauge",
        "aieos_up 1",
        "# HELP aieos_process_start_time_seconds 进程启动时间的 Unix 时间戳。",
        "# TYPE aieos_process_start_time_seconds gauge",
        f"aieos_process_start_time_seconds {_START_TIME:.0f}",
        "# HELP aieos_db_up 数据库连通性（1=正常，0=异常）。",
        "# TYPE aieos_db_up gauge",
        f"aieos_db_up {1 if db_up else 0}",
        "# HELP aieos_db_probe_duration_seconds 最近一次数据库探针耗时（秒）。",
        "# TYPE aieos_db_probe_duration_seconds gauge",
        f"aieos_db_probe_duration_seconds {db_latency:.3f}",
        "# HELP aieos_db_probe_timestamp_seconds 最近一次数据库探针的 Unix 时间戳。",
        "# TYPE aieos_db_probe_timestamp_seconds gauge",
        f"aieos_db_probe_timestamp_seconds {now:.0f}",
    ]
    # 探针指标之后追加 RED / LLM / 任务队列指标（同一份抓取响应）。
    payload = "\n".join(lines) + "\n" + render_prometheus()
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4",
    )
