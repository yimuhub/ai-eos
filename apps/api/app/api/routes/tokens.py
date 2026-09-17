"""Token 统计与计费后台：计费设置 CRUD + 六维用量统计。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.logging_config import get_logger
from app.models.agent import Agent, Model
from app.models.company import Company, Department
from app.models.token_usage import PricingConfig, TokenUsage, new_id
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/tokens", tags=["tokens"])

SCENARIOS = ["CHAT", "MEETING", "TASK", "RUNTIME", "FACTORY", "TRAINING",
             "LEARNING", "REFLECTION", "ORCHESTRATION", "TEAM_GEN", "SKILL", "OTHER"]


def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ================= 计费设置 =================

class PricingIn(BaseModel):
    model_id: str
    model_name: str | None = None
    billing_mode: str = "USAGE"              # USAGE / SUBSCRIPTION
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    monthly_fee: float = 0.0
    overage_input_per_m: float = 0.0
    overage_output_per_m: float = 0.0
    included_tokens_per_month: int = 0
    currency: str = "CNY"
    enabled: bool = True
    note: str | None = None


@router.get("/pricing")
async def list_pricing(db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """计费标准列表（带模型名回填）。"""
    q = await db.execute(select(PricingConfig).order_by(PricingConfig.created_at.desc()))
    rows = q.scalars().all()
    out = []
    for p in rows:
        m = await db.get(Model, p.model_id)
        out.append({"id": p.id, "model_id": p.model_id,
                    "model_name": m.name if m else (p.model_name or "-"),
                    "billing_mode": p.billing_mode,
                    "input_price_per_m": p.input_price_per_m,
                    "output_price_per_m": p.output_price_per_m,
                    "monthly_fee": p.monthly_fee,
                    "overage_input_per_m": p.overage_input_per_m,
                    "overage_output_per_m": p.overage_output_per_m,
                    "included_tokens_per_month": p.included_tokens_per_month,
                    "currency": p.currency, "enabled": p.enabled, "note": p.note})
    return out


@router.get("/pricing/models")
async def list_models_for_pricing(db: AsyncSession = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """可配置计费的模型清单（模型配置页的全部模型）。"""
    q = await db.execute(select(Model))
    return [{"id": m.id, "name": m.name, "model_id": m.model_id,
             "provider_type": str(m.provider_type)} for m in q.scalars().all()]


@router.post("/pricing", status_code=201)
async def upsert_pricing(data: PricingIn, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """新建或更新（按 model_id 幂等）计费标准。"""
    if data.billing_mode not in ("USAGE", "SUBSCRIPTION"):
        raise HTTPException(400, "billing_mode 必须是 USAGE 或 SUBSCRIPTION")
    m = await db.get(Model, data.model_id)
    q = await db.execute(select(PricingConfig).where(
        PricingConfig.model_id == data.model_id, PricingConfig.enabled.is_(True)))
    p = q.scalars().first()
    if not p:
        p = PricingConfig(id=new_id(), model_id=data.model_id)
        db.add(p)
    p.model_name = (m.name if m else None) or data.model_name
    p.billing_mode = data.billing_mode
    p.input_price_per_m = data.input_price_per_m
    p.output_price_per_m = data.output_price_per_m
    p.monthly_fee = data.monthly_fee
    p.overage_input_per_m = data.overage_input_per_m
    p.overage_output_per_m = data.overage_output_per_m
    p.included_tokens_per_month = data.included_tokens_per_month
    p.currency = data.currency
    p.enabled = data.enabled
    p.note = data.note
    await db.commit()
    return {"id": p.id, "model_id": p.model_id, "enabled": p.enabled}


@router.delete("/pricing/{pid}")
async def delete_pricing(pid: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    p = await db.get(PricingConfig, pid)
    if not p:
        raise HTTPException(404, "not found")
    # 计费标准挂在模型上，归属经由模型推导。原先按 id 就删：
    # 任何登录用户都能抹掉别人公司的计费标准（成本口径随之失真）。
    from app.api.deps import assert_company_owned
    from app.models.agent import Model as ModelRow
    model = await db.get(ModelRow, p.model_id)
    if model is None or model.company_id is None:
        raise HTTPException(
            409, "该计费标准所属的模型未归属任何公司，无法确认所有权。"
                 "请先在自己的公司下重新登记模型，再配置计费标准。")
    await assert_company_owned(db, user.id, model.company_id)
    await db.delete(p)
    await db.commit()
    return {"deleted": True}


# ================= 统计核心 =================

def _sum_cols():
    return (func.coalesce(func.sum(TokenUsage.input_tokens), 0),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            func.coalesce(func.sum(TokenUsage.total_cost), 0),
            func.count(TokenUsage.id))


async def _group_stats(db, group_col, base_filters=None, days: int | None = None):
    conds = []
    if base_filters:
        conds += base_filters
    if days:
        since = _now_utc() - timedelta(days=days)
        conds.append(TokenUsage.created_at >= since)
    inp_s, out_s, tot_s, cost_s, cnt = _sum_cols()
    q = await db.execute(
        select(group_col, inp_s, out_s, tot_s, cost_s, cnt)
        .where(*conds).group_by(group_col)
        .order_by(tot_s.desc()))
    return [{"key": str(k) if k is not None else "(未归属)",
             "input_tokens": int(r[0] or 0), "output_tokens": int(r[1] or 0),
             "total_tokens": int(r[2] or 0), "total_cost": round(float(r[3] or 0), 4),
             "calls": int(r[4])} for k, *r in q.all()]


async def _attach_names(db, rows, kind: str):
    """把 key 回填为可读名称。"""
    for r in rows:
        r["name"] = r["key"]
        oid = r["key"]
        if oid == "(未归属)" or not oid:
            continue
        obj = None
        if kind == "agent":
            obj = await db.get(Agent, oid)
        elif kind == "department":
            obj = await db.get(Department, oid)
        elif kind == "company":
            obj = await db.get(Company, oid)
        elif kind == "project":
            from app.models.project import Project
            obj = await db.get(Project, oid)
        elif kind == "task":
            from app.models.task import Task
            obj = await db.get(Task, oid)
        if obj is not None:
            r["name"] = getattr(obj, "name", None) or r["key"]
    return rows


def _parse_range(days: int | None, start: str | None, end: str | None):
    """解析时间范围过滤条件。

    无效的时间字符串此前被静默忽略——用户传了 `start=2026-13-45`，
    系统会**当作没有过滤条件**返回全量数据，界面上看不出任何异常。
    这里改为留痕；同时保留"非法值即忽略"的宽松行为，避免直接 500。
    """
    conds = []
    if start:
        try:
            conds.append(TokenUsage.created_at >= datetime.fromisoformat(start))
        except ValueError as exc:
            logger.warning("用量查询的 start 参数不是合法时间，已忽略该过滤条件 "
                           "start=%r: %s", start, exc)
    if end:
        try:
            e = datetime.fromisoformat(end)
            conds.append(TokenUsage.created_at <= e)
        except ValueError as exc:
            logger.warning("用量查询的 end 参数不是合法时间，已忽略该过滤条件 "
                           "end=%r: %s", end, exc)
    if not conds and days:
        conds.append(TokenUsage.created_at >= _now_utc() - timedelta(days=days))
    return conds


# ================= 六维统计接口 =================

@router.get("/stats/summary")
async def stats_summary(company_id: str = Query(None),
                        start: str = Query(None), end: str = Query(None),
                        days: int = Query(30),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """总览卡片：总量/费用/调用次数/平均延迟 + 按天趋势。"""
    conds = _parse_range(days, start, end)
    if company_id:
        conds.append(TokenUsage.company_id == company_id)
    inp_s, out_s, tot_s, cost_s, cnt = _sum_cols()
    row = (await db.execute(
        select(inp_s, out_s, tot_s, cost_s, cnt,
               func.coalesce(func.avg(TokenUsage.latency_ms), 0))
        .where(*conds))).one()
    # 按天趋势
    day = func.date(TokenUsage.created_at)
    trend_q = await db.execute(
        select(day, tot_s, cost_s, cnt).where(*conds).group_by(day).order_by(day))
    trend = [{"date": str(d), "total_tokens": int(t or 0),
              "total_cost": round(float(c or 0), 4), "calls": int(n)}
             for d, t, c, n in trend_q.all()]
    return {"input_tokens": int(row[0] or 0), "output_tokens": int(row[1] or 0),
            "total_tokens": int(row[2] or 0), "total_cost": round(float(row[3] or 0), 4),
            "calls": int(row[4]), "avg_latency_ms": int(float(row[5] or 0)),
            "trend": trend}


@router.get("/stats/by-agent")
async def stats_by_agent(company_id: str = Query(...), days: int = Query(30),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """(a) 个人维度：每个 Agent 用了多少。"""
    conds = [TokenUsage.company_id == company_id]
    conds += _parse_range(days, None, None)
    rows = await _group_stats(db, TokenUsage.agent_id, conds)
    rows = await _attach_names(db, rows, "agent")
    return rows


@router.get("/stats/by-department")
async def stats_by_department(company_id: str = Query(...), days: int = Query(30),
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """(b) 部门维度：经 Agent.department 归属。"""
    # agent→department 映射
    aq = await db.execute(select(Agent.id, Agent.department_id).where(
        Agent.company_id == company_id))
    amap = {aid: did for aid, did in aq.all()}
    conds = _parse_range(days, None, None)
    inp_s, out_s, tot_s, cost_s, cnt = _sum_cols()
    q = await db.execute(select(TokenUsage.agent_id, inp_s, out_s, tot_s, cost_s, cnt)
                         .where(TokenUsage.company_id == company_id, *conds)
                         .group_by(TokenUsage.agent_id))
    agg: dict = {}
    for aid, i, o, t, c, n in q.all():
        did = amap.get(aid) or "(未分配部门)"
        cur = agg.setdefault(did, [0, 0, 0, 0.0, 0])
        cur[0] += int(i or 0); cur[1] += int(o or 0); cur[2] += int(t or 0)
        cur[3] += float(c or 0); cur[4] += int(n)
    out_rows = [{"key": k, "total_tokens": v[2], "input_tokens": v[0],
                 "output_tokens": v[1], "total_cost": round(v[3], 4), "calls": v[4]}
                for k, v in agg.items()]
    out_rows.sort(key=lambda r: -r["total_tokens"])
    return await _attach_names(db, out_rows, "department")


@router.get("/stats/by-company")
async def stats_by_company(days: int = Query(30),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """(c) 公司维度。"""
    rows = await _group_stats(db, TokenUsage.company_id, _parse_range(days, None, None))
    return await _attach_names(db, rows, "company")


@router.get("/stats/by-project")
async def stats_by_project(company_id: str = Query(...), days: int = Query(30),
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """(d) 项目维度。"""
    conds = [TokenUsage.company_id == company_id,
             TokenUsage.project_id.isnot(None)]
    conds += _parse_range(days, None, None)
    rows = await _group_stats(db, TokenUsage.project_id, conds)
    return await _attach_names(db, rows, "project")


@router.get("/stats/by-task")
async def stats_by_task(company_id: str = Query(...), days: int = Query(30),
                        limit: int = Query(50, le=200),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """(e) 任务维度（Top N）。"""
    conds = [TokenUsage.company_id == company_id,
             TokenUsage.task_id.isnot(None)]
    conds += _parse_range(days, None, None)
    rows = await _group_stats(db, TokenUsage.task_id, conds)
    rows.sort(key=lambda r: -r["total_tokens"])
    return await _attach_names(db, rows[:limit], "task")


@router.get("/stats/by-scenario")
async def stats_by_scenario(company_id: str = Query(...), days: int = Query(30),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """(f) 学习/场景维度：学习·会议·任务·聊天…各占多少。"""
    conds = [TokenUsage.company_id == company_id]
    conds += _parse_range(days, None, None)
    return await _group_stats(db, TokenUsage.scenario, conds)


SCENARIO_LABELS = {
    "CHAT": "对话聊天", "MEETING": "会议", "TASK": "任务执行",
    "RUNTIME": "运行时", "FACTORY": "Agent工厂", "TRAINING": "培训",
    "LEARNING": "学习成长", "REFLECTION": "复盘反思",
    "ORCHESTRATION": "编排调度", "TEAM_GEN": "团队生成",
    "SKILL": "技能", "OTHER": "其他",
}


@router.get("/stats/scenario-labels")
async def scenario_labels(user: User = Depends(get_current_user)):
    return SCENARIO_LABELS


# ================= 明细流水 =================

@router.get("/usage-logs")
async def usage_logs(company_id: str = Query(...), limit: int = Query(100, le=500),
                     offset: int = Query(0),
                     scenario: str = Query(None), agent_id: str = Query(None),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """明细流水（最新在前），可按场景/Agent 筛选。"""
    conds = [TokenUsage.company_id == company_id]
    if scenario:
        conds.append(TokenUsage.scenario == scenario)
    if agent_id:
        conds.append(TokenUsage.agent_id == agent_id)
    q = await db.execute(
        select(TokenUsage).where(*conds)
        .order_by(TokenUsage.created_at.desc())
        .limit(limit).offset(offset))
    return [{"id": r.id, "agent_name": r.agent_name or "(系统)",
             "scenario": r.scenario, "scenario_label": SCENARIO_LABELS.get(r.scenario, r.scenario),
             "model_name": r.model_name or "-", "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens, "total_tokens": r.total_tokens,
             "total_cost": r.total_cost, "latency_ms": r.latency_ms,
             "success": r.success, "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in q.scalars().all()]
