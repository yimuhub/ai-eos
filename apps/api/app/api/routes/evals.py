"""评测证据 API（M4）：记录并汇总"多 Agent vs 单次基线"的盲评结果。

规则判定先跑（rubric 六维全可复算）；模型盲评由离线 runner 注入，
本接口只负责存证与汇总，避免在请求里长时间调模型。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.db.session import get_db
from app.eval.judge import VERDICTS
from app.eval.runner import run_case
from app.models.eval import EvalRun

router = APIRouter(tags=["evals"])


class EvalCompareIn(BaseModel):
    company_id: str
    suite_key: str
    case_key: str
    multi_text: str
    baseline_text: str
    model_tier: str = "unknown"
    workflow_id: str | None = None
    multi_tokens: int = 0
    baseline_tokens: int = 0
    multi_latency_ms: int = 0
    baseline_latency_ms: int = 0
    notes: str | None = None


def _row_dict(r: EvalRun) -> dict:
    return {"id": r.id, "suite_key": r.suite_key, "case_key": r.case_key,
            "model_tier": r.model_tier, "workflow_id": r.workflow_id,
            "baseline_kind": r.baseline_kind, "blind_order": r.blind_order,
            "rubric": r.rubric, "scores": r.scores, "verdict": r.verdict,
            "judge": r.judge, "tokens": r.tokens, "cost": r.cost,
            "latency_ms": r.latency_ms, "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@router.post("/evals/compare", status_code=201)
async def record_compare(data: EvalCompareIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """记录一组成对产出的盲评结果（确定性维度 + 可选模型结论由离线 runner 给）。"""
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.OPERATE)
    result = await run_case({
        "key": data.case_key, "model_tier": data.model_tier,
        "multi": data.multi_text, "baseline": data.baseline_text,
        "multi_tokens": data.multi_tokens,
        "baseline_tokens": data.baseline_tokens,
        "multi_latency_ms": data.multi_latency_ms,
        "baseline_latency_ms": data.baseline_latency_ms,
    })
    if result["verdict"] not in VERDICTS:
        raise HTTPException(500, "verdict 计算异常")
    run = EvalRun(company_id=data.company_id, suite_key=data.suite_key,
                  case_key=data.case_key, model_tier=data.model_tier,
                  workflow_id=data.workflow_id, baseline_kind="single_agent",
                  blind_order="A_first", rubric=result["scores"]["multi"]["weights"],
                  scores={"multi": result["scores"]["multi"]["total"],
                          "baseline": result["scores"]["baseline"]["total"],
                          "dims": {"multi": result["scores"]["multi"]["dims"],
                                   "baseline": result["scores"]["baseline"]["dims"]},
                          "multi_tokens": data.multi_tokens,
                          "baseline_tokens": data.baseline_tokens},
                  verdict=result["verdict"], judge=None,
                  tokens=data.multi_tokens + data.baseline_tokens,
                  latency_ms=max(data.multi_latency_ms, data.baseline_latency_ms),
                  notes=data.notes, created_by=user.id)
    db.add(run)
    await db.commit()
    return _row_dict(run)


async def _suite_rows(db: AsyncSession, company_id: str,
                      suite_key: str) -> list[EvalRun]:
    q = await db.execute(select(EvalRun).where(
        EvalRun.company_id == company_id, EvalRun.suite_key == suite_key)
        .order_by(EvalRun.created_at))
    return list(q.scalars())


@router.get("/evals/summary")
async def eval_summary(company_id: str, suite_key: str,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """按模型档聚合胜率与 token 比；分母为 0 时 win_rate 返回 null。"""
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)
    rows = await _suite_rows(db, company_id, suite_key)
    # 分数已在落库时算过，这里只做聚合
    by_tier: dict[str, dict] = {}
    for r in rows:
        t = by_tier.setdefault(r.model_tier, {"cases": 0, "multi_better": 0,
                                              "tie": 0, "baseline_better": 0,
                                              "multi_tokens": 0, "baseline_tokens": 0})
        t["cases"] += 1
        if r.verdict in t:
            t[r.verdict] += 1
        sc = r.scores or {}
        t["multi_tokens"] += int(sc.get("multi_tokens") or 0)
        t["baseline_tokens"] += int(sc.get("baseline_tokens") or 0)
    for t in by_tier.values():
        decided = t["multi_better"] + t["baseline_better"]
        t["win_rate"] = (t["multi_better"] / decided) if decided else None
        t["token_ratio"] = (round(t["multi_tokens"] / t["baseline_tokens"], 3)
                            if t["baseline_tokens"] else None)
    return {"suite_key": suite_key, "total": len(rows), "by_tier": by_tier,
            "disclaimer": "胜率只统计有多 Agent/基线明确胜负的 case；"
                          "tie 不计入分母。token 比为多 Agent 总 token / 基线总 token。"}


@router.get("/evals/{rid}")
async def get_eval(rid: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    run = await db.get(EvalRun, rid)
    if not run:
        raise HTTPException(404, "Eval run not found")
    await assert_company_capability(db, user.id, run.company_id, Capability.VIEW)
    return _row_dict(run)
