"""One-phrase Command Deck API (V4.0 一句话指挥台).

/command
  POST /command/plan        一句话 → 结构化方案（不执行，老板确认用）
  POST /command/execute     确认方案 → 执行（DIRECT/EXPERT/PROJECT 三路）
  GET  /command/report/{ref} 执行摘要 + 单文件 HTML 报告（分享用）
  POST /command/notify      报告推钉钉/飞书/企微 webhook

ref 约定：DIRECT → task_id 列表；EXPERT → 团队蓝图 job 列表；PROJECT → 作战室 sid
"""
import html
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.command_engine import command_execute, command_plan
from app.models.user import User

router = APIRouter(prefix="/command", tags=["command"])


class PlanIn(BaseModel):
    phrase: str
    attachments: list[dict] | None = None   # [{id,name,size}] 附件口


class ExecuteIn(BaseModel):
    plan: dict            # /command/plan 的原样返回（可被前端改过）


class NotifyIn(BaseModel):
    report: str           # HTML 文本
    webhook: str
    platform: str = "auto"    # auto/dingtalk/feishu/wecom


@router.post("/plan", status_code=201)
async def plan(
    data: PlanIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = await command_plan(db, company_id, data.phrase, data.attachments)
    return p


@router.post("/execute")
async def execute(
    data: ExecuteIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    r = await command_execute(db, company_id, data.plan, user.id)
    return r


# ---------- 执行摘要 + 单文件报告 ----------

def _html_report(title: str, lines: list[str], tokens: int, cost: float) -> str:
    rows = "".join(
        f'<div class="row"><span class="dot"></span>{html.escape(l)}</div>'
        for l in lines)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;background:#09090f;
color:#e5e7eb;margin:0;padding:24px;max-width:720px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:#9ca3af;font-size:12px;margin-bottom:16px}}
.row{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:10px;padding:10px 14px;margin:8px 0;font-size:13px;
display:flex;gap:10px;align-items:center}}
.dot{{width:8px;height:8px;border-radius:50%;background:#6366f1;flex:none}}
.foot{{color:#6b7280;font-size:11px;margin-top:16px}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">AI-EOS 一句话指挥台 · 自动生成的执行报告</div>
{rows}
<div class="foot">共 {tokens:,} tokens · 费用 ¥{cost:.4f} · 由 AI-EOS 自动生成</div>
</body></html>"""


@router.get("/report/{ref}")
async def report(
    ref: str,
    as_json: int = Query(0),
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """ref 支持：任务 task_id、作战室 sid、团队 job_id。"""
    lines: list[str] = []
    title = "执行报告"
    tokens, cost = 0, 0.0

    # 1) 任务
    from app.models.task import Task
    t = await db.get(Task, ref)
    if t:
        title = f"任务：{t.title}"
        lines.append(f"状态：{getattr(t, 'status', '?')}")
        for p in (t.progress or 0) and ["进度 {:.0%}".format(t.progress)] or []:
            lines.append(p)
        lines.append("（查看任务详情页获取完整过程）")
    else:
        # 2) 作战室
        from app.models.war_room import WarRoomSession
        sess = await db.get(WarRoomSession, ref)
        if sess:
            title = f"作战室：{sess.goal[:30]}"
            lines.append(f"整体状态：{sess.status}")
            from app.models.war_room import WarRoomTask
            tasks = (await db.execute(select(WarRoomTask).where(
                WarRoomTask.session_id == ref))).scalars().all()
            for w in tasks:
                lines.append(f"{w.seq}. {w.title[:40]} · {w.status} · 负责人 {w.agent_id[:8]}")
        else:
            # 3) 团队蓝图 job
            from app.factory_engine import AgentFactoryJob
            jobs = (await db.execute(select(AgentFactoryJob).where(
                AgentFactoryJob.company_id == company_id,
                AgentFactoryJob.id == ref))).scalars().all()
            if jobs:
                title = "专家团队蓝图"
                for j in jobs:
                    bp = j.blueprint or {}
                    lines.append(f"{bp.get('role') or bp.get('name')} · 状态 {j.status}")
            else:
                raise HTTPException(404, "找不到对应的任务/作战室/蓝图")

    # Token 费用：公司维度 SUM（单条聚合，避免多列无 GROUP BY）
    try:
        from app.models.token_usage import TokenUsage
        from sqlalchemy import func as _func
        q = await db.execute(
            select(_func.coalesce(_func.sum(TokenUsage.total_tokens), 0),
                   _func.coalesce(_func.sum(TokenUsage.total_cost), 0))
            .where(TokenUsage.company_id == company_id))
        row = q.first()
        if row:
            tokens, cost = row[0], row[1]
    except Exception:
        pass

    html_doc = _html_report(title, lines, tokens, cost)
    if as_json:
        return {"ref": ref, "title": title, "lines": lines,
                "html": html_doc, "tokens": tokens, "cost": cost}
    return HTMLResponse(html_doc)


# ---------- 群推送（钉钉/飞书/企微，按域名自适应消息体）----------

@router.post("/notify")
async def notify(data: NotifyIn, db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    import httpx
    text = data.report[:500] if "<" in data.report else data.report
    webhook = data.webhook
    platform = data.platform
    if platform == "auto":
        platform = ("dingtalk" if "dingtalk" in webhook else
                    "feishu" if "feishu" in webhook else
                    "wecom" if "qyapi.weixin" in webhook or "wecom" in webhook
                    else "dingtalk")
    if platform == "feishu":
        body = {"msg_type": "text", "content": {"text": text}}
    elif platform == "wecom":
        body = {"msgtype": "text", "text": {"content": text}}
    else:
        body = {"msgtype": "text", "text": {"content": text}}
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(webhook, json=body)
        ok = r.status_code == 200
        detail = r.json() if ok else r.text[:200]
    return {"ok": ok, "platform": platform, "detail": detail}
