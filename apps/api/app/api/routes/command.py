"""One-phrase Command Deck API (V4.0 一句话指挥台).

/command
  POST /command/plan        一句话 → 结构化方案（不执行，老板确认用）
  POST /command/execute     确认方案 → 执行（DIRECT/EXPERT/PROJECT 三路）
  GET  /command/report/{ref} 执行摘要 + 单文件 HTML 报告（分享用）
  POST /command/notify      报告推钉钉/飞书/企微 webhook

ref 约定：DIRECT → task_id 列表；EXPERT → 团队蓝图 job 列表；PROJECT → 作战室 sid
"""
import html

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.command_engine import command_execute, command_plan
from app.core.http_client import http_client
from app.core.logging_config import get_logger
from app.core.permissions import require_company_scope
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/command", tags=["command"],
                   dependencies=[Depends(require_company_scope())])


class PlanIn(BaseModel):
    phrase: str
    attachments: list[dict] | None = None   # [{id,name,size}] 附件口


class ExecuteIn(BaseModel):
    plan: dict            # /command/plan 的原样返回（可被前端改过）


class RunIn(BaseModel):
    phrase: str
    attachments: list[dict] | None = None
    # 默认直达：老板一句话就是授权。设为 False 可退回"先看方案再放行"。
    auto_run: bool = True


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


@router.post("/run", status_code=201)
async def run_one_shot(
    data: RunIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一句话直达：解析 → 执行一次完成，不需要中间确认。

    这是"老板下一道旨意，三省六部自己干活"的入口：DIRECT 路径创建的
    任务全部入队执行；PROJECT 路径跳过逐人宣告与负责人校对，直接开工。
    返回里同时带 plan（让前端能展示"它是怎么理解的"）与执行结果。
    """
    p = await command_plan(db, company_id, data.phrase, data.attachments)
    r = await command_execute(db, company_id, p, user.id, auto_run=data.auto_run)
    return {"plan": p, **r}


@router.get("/report-templates")
async def report_templates(user: User = Depends(get_current_user)):
    """可用的专业报告骨架（前端在任务/报告页展示，也用于说明生成规范）。"""
    from app.report_templates import list_templates
    return list_templates()


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
    except Exception as e:
        # 费用聚合失败不阻断报告生成，但报告里的 token/费用会是 0，
        # 必须留痕否则用户以为"这次执行没花 token"
        logger.warning("执行报告 token 费用聚合失败 company=%s：%s: %s",
                       company_id, type(e).__name__, e, exc_info=True)

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

    from app.core.net_guard import (OutboundURLRejected,
                                    validate_outbound_url_async)

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

    # webhook 是用户提供的出站目标：拒绝回环/私网/元数据
    try:
        await validate_outbound_url_async(webhook, allow_private=False)
    except OutboundURLRejected as e:
        raise HTTPException(400, f"webhook 不安全：{e}")

    try:
        async with http_client(timeout=15,
                                     follow_redirects=False) as cli:
            r = await cli.post(webhook, json=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"webhook 请求失败：{type(e).__name__}")

    if r.status_code != 200:
        # 只回状态码，绝不回显对方响应体（可能含内网信息/回显内容）
        return {"ok": False, "platform": platform,
                "detail": {"http": r.status_code}}
    try:
        detail = r.json()
    except Exception:
        logger.warning("连平台返回 200 但响应不是 JSON，只保留状态码（platform=%s）",
                       platform, exc_info=True)
        detail = {"http": r.status_code}
    return {"ok": True, "platform": platform, "detail": detail}

