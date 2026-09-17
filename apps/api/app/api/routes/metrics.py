"""质量度量：回答"这套系统到底靠不靠谱"。

## 为什么需要

改造前系统里只有 token 用量与费用——那是**投入**的度量，不是**结果**的度量。
整个项目没有任何地方能回答：

* 任务成功率是多少？
* 一个任务平均多久跑完？
* 失败的主要原因是什么？集中在哪个员工身上？
* 模型调用本身稳不稳？平均延迟多少？

缺了这些数字，"能不能直接去用"就只能靠感觉。

## 一条重要的口径约定

**把"没有样本"和"数值是 0"区分开**：样本为空时返回 `null` 而不是 `0.0`。
否则"这个公司还没跑过任何任务"会被前端渲染成"成功率 0%"，
把"没数据"误读成"全失败"——这是度量看板最容易犯的错。

## 为什么在 Python 里算而不全交给 SQL

耗时要用执行记录的 `created_at → updated_at` 求差，而
`DateTime(timezone=True)` 在 SQLite 与 PostgreSQL 上的存储与比较语义不同，
用 `julianday()` / `EXTRACT(EPOCH ...)` 会把度量逻辑绑死在某一种数据库上。
本模块的定位是"给人看的结果汇总"，不是高频接口，因此选择
**抓取样本后在 Python 里统一按 UTC 归一化计算**：跨库一致、可读、可测试。
样本量用上限保护，并在响应里显式声明是否被截断。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Company
from app.models.task import AgentExecution, Task, TaskStatus
from app.models.token_usage import TokenUsage

router = APIRouter(prefix="/metrics", tags=["metrics"])

# 单次统计的样本上限。超过则在响应里声明被截断，
# 而不是悄悄算出一个偏了的数字。
MAX_SAMPLE = 5000

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """把时间统一成"UTC 朴素时间"，避免 SQLite(朴素) 与 PG(带时区) 语义打架。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value  # SQLite 的 func.now() 本来就是 UTC 朴素时间
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _rate(ok: int, bad: int) -> float | None:
    """成功率。**样本为空时返回 None**，而不是 0.0。"""
    total = ok + bad
    if total == 0:
        return None
    return round(ok / total, 4)


def _normalize_reason(text: str | None) -> str | None:
    """把错误文本压成可聚合的"原因"，便于回答"主要为什么失败"。"""
    if not text:
        return None
    first = (text.strip().splitlines() or [""])[0].strip()
    first = _UUID_RE.sub("<id>", first)
    first = re.sub(r"\s+", " ", first)
    return first[:120] or None


@router.get("/overview")
async def overview(
    company_id: str = Query(...),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """公司维度的质量度量总览（默认最近 30 天）。"""
    company = await db.get(Company, company_id)
    if not company or company.owner_id != user.id:
        raise HTTPException(404, "Company not found")

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # ---------------- 任务 ----------------
    task_rows = list((await db.execute(
        select(Task).where(Task.company_id == company_id)
        .order_by(Task.created_at.desc()).limit(MAX_SAMPLE))).scalars())
    tasks_in_window = [
        t for t in task_rows
        if (_as_naive_utc(t.created_at) or cutoff) >= cutoff
    ]
    counts = Counter(t.status.value for t in tasks_in_window)
    completed = counts.get(TaskStatus.COMPLETED.value, 0)
    failed = counts.get(TaskStatus.FAILED.value, 0)

    # ---------------- 执行记录 ----------------
    exec_rows = list((await db.execute(
        select(AgentExecution).join(Task, Task.id == AgentExecution.task_id)
        .where(Task.company_id == company_id)
        .order_by(AgentExecution.created_at.desc()).limit(MAX_SAMPLE))).scalars())
    execs_in_window = [
        e for e in exec_rows
        if (_as_naive_utc(e.created_at) or cutoff) >= cutoff
    ]

    durations: list[float] = []
    failed_execs: list[AgentExecution] = []
    for e in execs_in_window:
        if e.status == "failed":
            failed_execs.append(e)
        if e.status in ("completed", "failed"):
            start = _as_naive_utc(e.created_at)
            end = _as_naive_utc(e.updated_at)
            if start and end:
                delta = (end - start).total_seconds()
                if 0 <= delta < 24 * 3600:  # 超过一天的视为异常样本，不污染均值
                    durations.append(delta)

    exec_completed = sum(1 for e in execs_in_window if e.status == "completed")
    exec_failed = sum(1 for e in execs_in_window if e.status == "failed")

    # 失败原因聚合
    reasons = Counter(
        r for r in (_normalize_reason(e.error) for e in failed_execs) if r)
    failure_reasons = [{"reason": r, "count": n}
                       for r, n in reasons.most_common(5)]

    # 失败集中在哪个员工身上
    by_agent = Counter(e.agent_id for e in failed_execs)
    agent_names: dict[str, str] = {}
    if by_agent:
        rows = (await db.execute(select(Agent.id, Agent.name).where(
            Agent.id.in_(list(by_agent)[:50])))).all()
        agent_names = {aid: name for aid, name in rows}
    failure_by_agent = [
        {"agent_id": aid, "agent_name": agent_names.get(aid) or aid[:8],
         "failed": n}
        for aid, n in by_agent.most_common(5)
    ]

    # 按天吞吐（看趋势，也是"有没有在干活"的最直观证据）
    daily: dict[str, dict[str, int]] = {}
    for e in execs_in_window:
        day = _as_naive_utc(e.created_at)
        if not day or e.status not in ("completed", "failed"):
            continue
        key = day.date().isoformat()
        bucket = daily.setdefault(key, {"completed": 0, "failed": 0})
        bucket["completed" if e.status == "completed" else "failed"] += 1

    # ---------------- 模型调用 ----------------
    usage_rows = list((await db.execute(
        select(TokenUsage).where(TokenUsage.company_id == company_id)
        .order_by(TokenUsage.created_at.desc()).limit(MAX_SAMPLE))).scalars())
    usage_in_window = [
        u for u in usage_rows
        if (_as_naive_utc(u.created_at) or cutoff) >= cutoff
    ]
    call_ok = sum(1 for u in usage_in_window if u.success)
    call_bad = len(usage_in_window) - call_ok
    latencies = [u.latency_ms for u in usage_in_window if (u.latency_ms or 0) > 0]

    return {
        "company": {"id": company.id, "name": company.name},
        "window_days": days,
        "tasks": {
            "total": len(tasks_in_window),
            "completed": completed,
            "failed": failed,
            "running": counts.get(TaskStatus.RUNNING.value, 0)
            + counts.get(TaskStatus.PLANNING.value, 0)
            + counts.get(TaskStatus.WAITING.value, 0),
            "cancelled": counts.get(TaskStatus.CANCELLED.value, 0),
            "todo": counts.get(TaskStatus.TODO.value, 0),
            # 成功率只在"有结论"的任务里算：跑着的任务既不算成功也不算失败。
            "success_rate": _rate(completed, failed),
        },
        "executions": {
            "total": len(execs_in_window),
            "completed": exec_completed,
            "failed": exec_failed,
            "success_rate": _rate(exec_completed, exec_failed),
            "avg_duration_seconds": (round(sum(durations) / len(durations), 1)
                                     if durations else None),
        },
        "model_calls": {
            "total": len(usage_in_window),
            "success": call_ok,
            "failed": call_bad,
            "success_rate": _rate(call_ok, call_bad),
            "avg_latency_ms": (round(sum(latencies) / len(latencies))
                               if latencies else None),
        },
        "failure_reasons": failure_reasons,
        "failure_by_agent": failure_by_agent,
        "throughput_by_day": [
            {"date": d, **v} for d, v in sorted(daily.items())
        ],
        "sampled": {
            "tasks": len(task_rows),
            "executions": len(exec_rows),
            "model_calls": len(usage_rows),
            "limit": MAX_SAMPLE,
            "truncated": any(n >= MAX_SAMPLE for n in
                             (len(task_rows), len(exec_rows), len(usage_rows))),
        },
    }
