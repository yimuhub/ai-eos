"""CEO 分级自我进化：周期、幂等、参数硬边界、月度拍板、人格注入与租户隔离。

这些用例不调真实模型：叙事层被替换成确定性返回，验的是**编排与安全边界**。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app import self_evolution as evo
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.evolution import CeoPlaybook, EvolutionRecord
from app.models.scheduler import compute_next_run
from app.models.task import AgentExecution, Task, TaskStatus
from app.schemas.responses_evolution import (EvolutionOverviewOut,
                                             EvolutionRecordOut,
                                             EvolutionScheduleOut,
                                             PlaybookOut)


def _assert_keys(body: dict, model) -> None:
    assert set(body) == set(model.model_fields), (
        f"{model.__name__} 键集合不一致：多出 "
        f"{sorted(set(body) - set(model.model_fields))}，缺少 "
        f"{sorted(set(model.model_fields) - set(body))}")


async def _seed(company_id: str, user_id: str, *, failed: int = 1,
                retried: int = 0) -> dict:
    """建一个 CEO + 若干任务，制造可供进化的真实运行数据。"""
    async with AsyncSessionLocal() as s:
        ceo = Agent(id=str(uuid.uuid4()), company_id=company_id, name="王总",
                    role="总经理", is_ceo=True, status=AgentStatus.IDLE,
                    created_by_user_id=user_id)
        s.add(ceo)
        await s.flush()
        done_ids = []
        for i in range(3):
            t = Task(id=str(uuid.uuid4()), company_id=company_id,
                     title=f"完成的任务{i}", status=TaskStatus.COMPLETED,
                     assigned_agent_id=ceo.id, created_by=user_id)
            s.add(t)
            done_ids.append(t.id)
        for i in range(failed):
            s.add(Task(id=str(uuid.uuid4()), company_id=company_id,
                       title=f"失败的任务{i}", status=TaskStatus.FAILED,
                       assigned_agent_id=ceo.id, created_by=user_id))
        # 先 flush 任务：执行记录带 task_id 外键，SQLite 下必须让任务先落库
        await s.flush()
        for tid in done_ids[:retried]:
            s.add(AgentExecution(id=str(uuid.uuid4()), agent_id=ceo.id,
                                 task_id=tid, status="failed"))
            s.add(AgentExecution(id=str(uuid.uuid4()), agent_id=ceo.id,
                                 task_id=tid, status="completed"))
        await s.commit()
        return {"ceo_id": ceo.id}


async def _run(company_id: str, level: str, user_id: str | None = None) -> dict:
    async with AsyncSessionLocal() as s:
        return await evo.run_evolution(s, company_id, level, user_id=user_id,
                                       source="manual")


def _no_llm(monkeypatch):
    async def _fake(db, company_id, prompt, max_tokens=1600):
        return None, {"tokens": 0, "cost": 0.0}
    monkeypatch.setattr(evo, "_llm_json", _fake)


# ---------------------------------------------------------------------------
# 周期与触发器
# ---------------------------------------------------------------------------

def test_period_bounds_are_calendar_aligned():
    now = datetime(2026, 11, 1, 15, 30)  # 周日
    ds, de, dk = evo.period_bounds("daily", now)
    assert (ds.hour, ds.minute) == (0, 0) and dk == "2026-11-01"
    assert (de - ds).days == 1

    ws, we, wk = evo.period_bounds("weekly", now)
    assert ws.weekday() == 0 and ws.day == 26 and wk == "2026-W44"
    assert (we - ws).days == 7

    ms, me, mk = evo.period_bounds("monthly", now)
    assert (ms.day, ms.hour) == (1, 0) and mk == "2026-11"
    assert (me - ms).days == 30


def test_compute_next_run_supports_weekly_and_monthly():
    after = datetime(2026, 11, 1, 12, 0)  # 周日
    nxt = compute_next_run("weekly", {"weekday": 0, "time": "09:30"}, after)
    assert nxt == datetime(2026, 11, 2, 9, 30)
    nxt = compute_next_run("monthly", {"day": 1, "time": "10:00"}, after)
    assert nxt == datetime(2026, 12, 1, 10, 0)
    # 2 月没有 31 号：夹到 28 号，避免某些月份永远不触发
    nxt = compute_next_run("monthly", {"day": 31, "time": "08:00"},
                           datetime(2026, 2, 1, 9, 0))
    assert nxt == datetime(2026, 2, 28, 8, 0)


# ---------------------------------------------------------------------------
# 进化主流程
# ---------------------------------------------------------------------------

async def test_evolution_skips_when_there_is_no_activity(tenants):
    co = tenants.alice_co["id"]
    async with AsyncSessionLocal() as s:
        rec = await evo.run_evolution(s, co, "daily")
    assert rec["status"] == "SKIPPED" and "没有" in (rec["error"] or "")
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(CeoPlaybook))).scalars().first() is None


async def test_daily_evolution_is_idempotent_and_versions_playbook(
        tenants, monkeypatch):
    _no_llm(monkeypatch)
    co = tenants.alice_co["id"]
    await _seed(co, tenants.alice["id"], failed=1, retried=1)

    first = await _run(co, "daily", tenants.alice["id"])
    assert first["status"] == "APPLIED"
    assert first["playbook_version"] == 1
    assert first["assessment"]["score"] < 100
    assert first["actions"][0]["type"] == "playbook"

    second = await _run(co, "daily", tenants.alice["id"])
    assert second["reused"] is True and second["id"] == first["id"]

    async with AsyncSessionLocal() as s:
        playbooks = (await s.execute(select(CeoPlaybook))).scalars().all()
        assert len(playbooks) == 1, "同一周期重复触发不应多出版本"
        pb = playbooks[0]
        assert pb.active and pb.version == 1
        assert pb.principles, "确定性兜底也必须产出可执行原则"
        assert pb.source == "daily"
        assert pb.adjustments["quality_bar"] == 80


async def test_weekly_evolution_clamps_out_of_bounds_adjustments(
        tenants, monkeypatch):
    co = tenants.alice_co["id"]
    await _seed(co, tenants.alice["id"], failed=1)
    _no_llm(monkeypatch)
    await _run(co, "daily", tenants.alice["id"])

    async def _fake(db, company_id, prompt, max_tokens=1600):
        return ({
            "persona": {"risk_posture": "进取型", "decision_style": "先试小样",
                        "traits": ["快", "省"]},
            "principles": ["先复用验证过的流程", "失败两次就换方法"],
            "adjustments": {"quality_bar": 10, "cost_guard_ratio": 9,
                            "parallel_cap": 99, "retry_budget": -3},
            "summary": "本周学会先复用再试错",
        }, {"tokens": 1200, "cost": 0.03})
    monkeypatch.setattr(evo, "_llm_json", _fake)

    rec = await _run(co, "weekly", tenants.alice["id"])
    assert rec["status"] == "APPLIED" and rec["playbook_version"] == 2
    assert rec["tokens_used"] == 1200 and rec["cost"] == 0.03

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(CeoPlaybook)
                                .order_by(CeoPlaybook.version))).scalars().all()
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].active is False and rows[1].active is True
        pb = rows[1]
        assert pb.adjustments["quality_bar"] == 60
        assert pb.adjustments["cost_guard_ratio"] == 1.5
        assert pb.adjustments["parallel_cap"] == 6
        assert pb.adjustments["retry_budget"] == 0
        assert pb.persona["risk_posture"] == "进取型"
        assert pb.principles[0] == "先复用验证过的流程"


async def test_monthly_high_risk_actions_wait_for_owner(tenants, monkeypatch):
    co = tenants.alice_co["id"]
    await _seed(co, tenants.alice["id"], failed=1)

    async def _fake(db, company_id, prompt, max_tokens=1600):
        return ({
            "actions": [
                {"type": "workflow", "title": "固化失败任务工作流",
                 "detail": "把失败最多的任务沉淀成流程", "risk": "low"},
                {"type": "org", "title": "调整编制",
                 "detail": "补一个专职岗位", "risk": "high"},
            ],
            "summary": "本月先固化流程，再谈组织",
        }, {"tokens": 800, "cost": 0.02})
    monkeypatch.setattr(evo, "_llm_json", _fake)

    rec = await _run(co, "monthly", tenants.alice["id"])
    assert rec["status"] == "PENDING_APPROVAL"
    by_risk = {a["risk"]: a for a in rec["actions"]}
    assert by_risk["low"]["applied"] is True
    assert by_risk["high"]["applied"] is False

    high_id = by_risk["high"]["id"]
    async with AsyncSessionLocal() as s:
        row = await s.get(EvolutionRecord, rec["id"])
        decided = await evo.decide_action(s, row, high_id, approve=True,
                                          user_id=tenants.alice["id"],
                                          note="同意补岗")
    assert decided["status"] == "APPLIED"
    assert all(a["applied"] for a in decided["actions"])
    assert decided["playbook_version"] == 1

    async with AsyncSessionLocal() as s:
        pb = (await s.execute(select(CeoPlaybook)
                              .where(CeoPlaybook.active.is_(True)))).scalars().first()
        assert pb is not None
        assert pb.adjustments["monthly_focus"] == ["固化失败任务工作流", "调整编制"]


async def test_emergency_stop_skips_evolution(tenants):
    from app.models.p3_governance import AutonomousSettings
    co = tenants.alice_co["id"]
    await _seed(co, tenants.alice["id"])
    async with AsyncSessionLocal() as s:
        s.add(AutonomousSettings(id=str(uuid.uuid4()), company_id=co,
                                 scope_type="COMPANY", level=2,
                                 emergency_stopped=True))
        await s.commit()
    rec = await _run(co, "daily", tenants.alice["id"])
    assert rec["status"] == "SKIPPED" and "紧急停止" in (rec["error"] or "")


# ---------------------------------------------------------------------------
# 人格注入 CEO 拆解
# ---------------------------------------------------------------------------

async def test_playbook_is_injected_into_ceo_plan_prompt(tenants, monkeypatch):
    from app import ceo_engine

    co = tenants.alice_co["id"]
    await _seed(co, tenants.alice["id"])
    async with AsyncSessionLocal() as s:
        s.add(CeoPlaybook(
            id=str(uuid.uuid4()), company_id=co, version=3, active=True,
            source="weekly", persona={"risk_posture": "稳健型"},
            principles=["先复用验证过的流程"], adjustments={}))
        await s.commit()

    captured: list[list[dict]] = []

    async def _fake_chat(db, agent, messages, max_tokens=1600):
        captured.append(messages)
        return ('{"title":"测试","objective":"o","deliverable":"d",'
                '"redlines":[],"acceptance":[],"terminology":{},'
                '"subtasks":[{"title":"写方案","description":"产出测试方案"}],'
                '"new_agents":[]}')

    monkeypatch.setattr(ceo_engine, "_chat", _fake_chat)

    async with AsyncSessionLocal() as s:
        await ceo_engine.create_mission(
            s, company_id=co, user_id=tenants.alice["id"],
            instruction="做一份测试方案")
    joined = "\n".join(m["content"] for m in captured[0])
    assert "第 3 版" in joined and "先复用验证过的流程" in joined, (
        "运行人格没有进入 CEO 拆解提示词——进化的结果就不会影响行为")


# ---------------------------------------------------------------------------
# HTTP 契约与租户隔离
# ---------------------------------------------------------------------------

async def test_evolution_http_contract_and_tenant_isolation(
        tenants, monkeypatch, client):
    _no_llm(monkeypatch)
    alice, bob = tenants.alice, tenants.bob
    co = tenants.alice_co["id"]
    await _seed(co, alice["id"], failed=1)

    # 触发一次进化（直接调引擎，HTTP 只验读取与隔离，避免重复消耗）
    rec = await _run(co, "daily", alice["id"])

    r = await client.get(f"/evolution?company_id={co}",
                         headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == set(EvolutionOverviewOut.model_fields)
    _assert_keys(body["playbook"], PlaybookOut)
    _assert_keys(body["records"][0], EvolutionRecordOut)
    for sched in body["schedules"]:
        _assert_keys(sched, EvolutionScheduleOut)

    r = await client.get(f"/evolution/{rec['id']}", headers=alice["headers"])
    assert r.status_code == 200
    _assert_keys(r.json(), EvolutionRecordOut)

    # 别的租户：知道 id 也读不到；对别人的公司触发进化直接 404
    r = await client.get(f"/evolution/{rec['id']}", headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post("/evolution/run",
                          json={"company_id": co, "level": "daily"},
                          headers=bob["headers"])
    assert r.status_code == 404
    r = await client.get(f"/evolution?company_id={co}", headers=bob["headers"])
    assert r.status_code == 404

    # 调度幂等：第一次建 3 条，第二次 created 全为 False
    r = await client.post("/evolution/schedules",
                          json={"company_id": co, "level": "daily"},
                          headers=alice["headers"])
    assert r.status_code == 200 and len(r.json()["schedules"]) == 3
    assert sum(1 for v in r.json()["created"].values() if v["created"]) == 3
    r = await client.post("/evolution/schedules",
                          json={"company_id": co, "level": "daily"},
                          headers=alice["headers"])
    assert not any(v["created"] for v in r.json()["created"].values())
