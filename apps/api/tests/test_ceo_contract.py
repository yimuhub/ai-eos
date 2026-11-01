"""超级 Agent（CEO）契约 + 核心行为护栏。

三类硬要求，任何一条退化都必须让这个文件变红：

1. **契约**：16 个 /ceo 端点全部声明响应模型，且实测键集合与模型字段相等
   （全部 `extra="allow"`，判断错时多余键会原样透出而不是被静默裁掉）。
2. **闸门**：2/3/10 三档并发峰值不超限；连续限流 3 次自动降到保底 2，
   冷却到期后恢复。
3. **回收**：确定性偏离不花模型钱就能发现；偏离/失败各补跑一次，
   补跑成功覆盖原结果；报告归档 + 站内通知必达。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.background_jobs as bg
from app import ceo_engine
from app.api.routes import ceo as ceo_routes
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentModelConfig, Model, ModelProviderType
from app.models.asset import Asset
from app.models.ceo import CeoMission
from app.models.notification import Notification
from app.models.task import Task, TaskStatus
from app.schemas import responses_ceo
from app.services.ceo_concurrency import (DEFAULT_PARALLEL, DynamicGate,
                                          HARD_CAP, MIN_PARALLEL, clamp_requested,
                                          failure_kind, provider_cap_of)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------

def _plan(subtask_count: int, names: list[str]) -> dict:
    return {
        "title": "季度增长方案",
        "objective": "把 Q3 线索量提升 30%",
        "deliverable": "一份可执行的增长方案，文件名 growth.md",
        "redlines": ["不得编造数据", "不得对外发送"],
        "acceptance": ["每个结论有出处", "给出预算与负责人"],
        "terminology": {"线索": "留下联系方式的有效咨询"},
        "subtasks": [
            {"title": f"子任务{i}",
             "description": f"负责第 {i} 部分，边界写清楚",
             "agent_name": names[i % len(names)]}
            for i in range(1, subtask_count + 1)
        ],
    }


def _install_fake_chat(monkeypatch, plan: dict, reviews: list[dict] | None = None):
    """替换模型调用：拆解返回固定计划，审查返回指定结论，汇总返回固定报告。"""

    async def _fake(db, agent, messages, max_tokens=1600):
        text = messages[-1]["content"]
        if "老板原话：" in text:
            return json.dumps(plan, ensure_ascii=False)
        if "逐条判断产出是否偏离准绳" in text:
            return json.dumps({"reviews": reviews or []}, ensure_ascii=False)
        return "## 结论\n方案已就绪，可直接执行。\n\n## 风险与建议\n暂无。"

    monkeypatch.setattr(ceo_engine, "_chat", _fake)


@pytest.fixture
def queued_jobs(monkeypatch):
    """后台投递只记录不执行：测试不启动 worker 线程，避免与清理用例抢库。"""
    jobs: list = []
    monkeypatch.setattr(bg, "submit_async",
                        lambda job, *a, **kw: jobs.append(job) or "job-id")
    return jobs


async def _agent(client, user, company_id: str, name: str, role: str = "专家") -> str:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name, "role": role},
        headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _company_with_agents(client, make_user, make_company,
                               names: tuple[str, ...] = ("王总", "小李", "小张")):
    user = await make_user()
    co = await make_company(user)
    ids = {}
    for i, name in enumerate(names):
        ids[name] = await _agent(client, user, co["id"], name,
                                 "总经理" if i == 0 else "专家")
    r = await client.post("/ceo/config", json={
        "company_id": co["id"], "agent_id": ids[names[0]]},
        headers=user["headers"])
    assert r.status_code == 200, r.text
    return user, co, ids


def _assert_keys(body: dict, model, where: str = "") -> None:
    assert set(body) == set(model.model_fields), (
        f"{where or model.__name__} 键集合不一致："
        f"多出 {sorted(set(body) - set(model.model_fields))}，"
        f"缺少 {sorted(set(model.model_fields) - set(body))}")


# ---------------------------------------------------------------------------
# 1. 静态护栏：路由与响应模型
# ---------------------------------------------------------------------------

async def test_every_ceo_route_declares_a_response_model():
    routes = [r for r in ceo_routes.router.routes]
    assert len(routes) == 16, f"CEO 端点数量变了：{len(routes)}"
    bare = [f"{sorted(r.methods)} {r.path}" for r in routes
            if getattr(r, "response_model", None) is None]
    assert not bare, f"这些 CEO 端点返回裸 dict：{bare}"


async def test_every_ceo_model_allows_extra_keys():
    offenders = [
        name for name, obj in vars(responses_ceo).items()
        if isinstance(obj, type) and obj.__module__ == responses_ceo.__name__
        and hasattr(obj, "model_fields")
        and obj.model_config.get("extra") != "allow"]
    assert not offenders, f"响应模型没有 extra='allow'：{offenders}"


# ---------------------------------------------------------------------------
# 2. 并发闸门（不经数据库，纯行为）
# ---------------------------------------------------------------------------

async def _peak_parallel(count: int, limit: int) -> int:
    gate = DynamicGate(limit)
    active = peak = 0

    async def worker():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*[worker() for _ in range(count)])
    assert gate.active == 0, "全部执行完后仍占着许可，会拖死后续子任务"
    return peak


@pytest.mark.parametrize("limit", [2, 3, 10])
async def test_gate_never_exceeds_requested_parallelism(limit):
    peak = await _peak_parallel(count=12, limit=limit)
    assert peak == limit, f"并发峰值 {peak} 与设定 {limit} 不符"
    assert peak <= HARD_CAP


async def test_requested_parallel_is_clamped_to_2_10():
    assert clamp_requested(None) == DEFAULT_PARALLEL == 3
    assert clamp_requested(1) == MIN_PARALLEL == 2
    assert clamp_requested(0) == MIN_PARALLEL
    assert clamp_requested(11) == HARD_CAP == 10
    assert clamp_requested("not-a-number") == DEFAULT_PARALLEL
    assert provider_cap_of("anthropic") == 4
    assert provider_cap_of("openai_compat") == 8
    assert provider_cap_of("gemini") == 3
    assert provider_cap_of(None) == MIN_PARALLEL, "未知供应商必须按保底 2"


async def test_failure_kind_recognises_throttle_and_timeout():
    assert failure_kind(RuntimeError("HTTP 429 Too Many Requests")) == "throttle"
    assert failure_kind(RuntimeError("rate limit exceeded")) == "throttle"
    assert failure_kind(asyncio.TimeoutError()) == "timeout"
    assert failure_kind(RuntimeError("connection reset")) == "other"


async def test_gate_degrades_after_three_throttles_then_restores():
    gate = DynamicGate(10, base_limit=10)
    assert await gate.note_failure("throttle") is None
    assert await gate.note_failure("timeout") is None
    info = await gate.note_failure("throttle")
    assert info is not None, "连续 3 次限流/超时没有触发降档"
    assert info["limit"] == MIN_PARALLEL
    snap = gate.snapshot()
    assert snap["degraded"] is True and "限流" in snap["degrade_reason"]

    # 冷却到期：下一次取许可时恢复原并发
    gate._degraded_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with gate.slot():
        snap = gate.snapshot()
    assert snap["degraded"] is False and snap["limit"] == 10
    assert gate.degrade_reason is None


async def test_gate_ignores_non_throttle_failures():
    gate = DynamicGate(5, base_limit=5)
    for _ in range(5):
        assert await gate.note_failure("other") is None
    assert gate.limit == 5 and gate.snapshot()["degraded"] is False


# ---------------------------------------------------------------------------
# 3. 端点契约：立锚 → 预览 → 开工 → 操作
# ---------------------------------------------------------------------------

async def test_mission_preview_keeps_more_than_five_subtasks(
        client, make_user, make_company, monkeypatch, queued_jobs):
    plan = _plan(8, ["王总", "小李", "小张"])
    _install_fake_chat(monkeypatch, plan)
    user, co, ids = await _company_with_agents(client, make_user, make_company)

    cfg = await client.get("/ceo/config", params={"company_id": co["id"]},
                           headers=user["headers"])
    assert cfg.status_code == 200, cfg.text
    _assert_keys(cfg.json(), responses_ceo.CeoConfigOut, "GET /ceo/config")
    assert cfg.json()["has_ceo"] is True
    assert len(cfg.json()["candidates"]) == 3
    _assert_keys(cfg.json()["candidates"][0], responses_ceo.CeoAgentOptionOut)

    r = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "把 Q3 线索量提升 30%",
        "max_parallel": 5}, headers=user["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    _assert_keys(body, responses_ceo.CeoMissionPreviewOut, "POST /ceo/missions")
    assert body["subtask_count"] == 8, "旧 orchestrator 的 5 条上限又回来了"
    assert len(body["subtasks"]) == 8
    _assert_keys(body["subtasks"][0], responses_ceo.CeoPlannedSubtaskOut)
    assert body["redlines"] == ["不得编造数据", "不得对外发送"]
    assert body["terminology"]["线索"].startswith("留下联系方式")
    assert body["max_parallel"] == 5
    assert body["provider_cap"] == MIN_PARALLEL, "未绑定模型应走保底档"
    assert body["effective_parallel"] == MIN_PARALLEL
    assert body["anchor_version"] == 1
    mission_id = body["mission_id"]

    detail = await client.get(f"/ceo/missions/{mission_id}", headers=user["headers"])
    assert detail.status_code == 200, detail.text
    _assert_keys(detail.json(), responses_ceo.CeoMissionDetailOut, "GET mission")
    assert detail.json()["slots"] == [], "预览阶段不应凭空造出子任务"

    listed = await client.get("/ceo/missions", params={"company_id": co["id"]},
                              headers=user["headers"])
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    _assert_keys(listed.json()[0], responses_ceo.CeoMissionListItemOut)

    # 中途追加要求：锚版本 +1，只增不改
    ins = await client.post(f"/ceo/missions/{mission_id}/instruction",
                            json={"text": "把预算压到 1 万以内"},
                            headers=user["headers"])
    assert ins.status_code == 200, ins.text
    _assert_keys(ins.json(), responses_ceo.CeoInstructionOut)
    assert ins.json()["anchor_version"] == 2
    assert ins.json()["extra_instructions"] == ["把预算压到 1 万以内"]

    # 开工：建子任务 + 提交后台（这里只记录不执行）
    launch = await client.post(f"/ceo/missions/{mission_id}/launch", json={},
                               headers=user["headers"])
    assert launch.status_code == 202, launch.text
    _assert_keys(launch.json(), responses_ceo.CeoLaunchOut)
    assert launch.json()["status"] == "RUNNING"
    assert launch.json()["subtask_count"] == 8
    assert len(queued_jobs) == 1

    async with AsyncSessionLocal() as s:
        tasks = (await s.execute(
            select(Task).where(Task.mission_id == mission_id))).scalars().all()
        mission = await s.get(CeoMission, mission_id)
    assert len(tasks) == 8 and all(t.assigned_agent_id for t in tasks)
    assert mission.extra_instructions == ["把预算压到 1 万以内"]
    assert mission.anchor_version == 2

    paused = await client.post(f"/ceo/missions/{mission_id}/pause",
                               headers=user["headers"])
    assert paused.status_code == 200, paused.text
    _assert_keys(paused.json(), responses_ceo.CeoStatusOut)
    assert paused.json()["status"] == "PAUSED"

    resumed = await client.post(f"/ceo/missions/{mission_id}/resume",
                                headers=user["headers"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"
    assert len(queued_jobs) == 2

    cancelled = await client.post(f"/ceo/missions/{mission_id}/cancel",
                                  headers=user["headers"])
    assert cancelled.status_code == 200, cancelled.text
    _assert_keys(cancelled.json(), responses_ceo.CeoCancelOut)
    assert cancelled.json()["cancelled_tasks"] == 8
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Task).where(Task.mission_id == mission_id))).scalars().all()
    assert all(t.status == TaskStatus.CANCELLED for t in rows)

    reclaim = await client.post(f"/ceo/missions/{mission_id}/reclaim",
                                headers=user["headers"])
    assert reclaim.status_code == 409, "已取消的统筹不应该再补跑"


async def test_launch_requires_planning_state(client, make_user, make_company,
                                              monkeypatch, queued_jobs):
    _install_fake_chat(monkeypatch, _plan(2, ["王总", "小李"]))
    user, co, _ = await _company_with_agents(client, make_user, make_company)
    r = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "写一份增长方案"},
        headers=user["headers"])
    mid = r.json()["mission_id"]
    assert (await client.post(f"/ceo/missions/{mid}/launch", json={},
                              headers=user["headers"])).status_code == 202
    again = await client.post(f"/ceo/missions/{mid}/launch", json={},
                              headers=user["headers"])
    assert again.status_code == 409


async def test_assigning_ceo_enables_daily_digest_by_default(client, make_user,
                                                             make_company):
    user, co, _ = await _company_with_agents(client, make_user, make_company)
    from app.models.scheduler import AutomationSchedule

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AutomationSchedule).where(
            AutomationSchedule.company_id == co["id"],
            AutomationSchedule.action_type == "ceo_digest"))).scalars().all()
    assert len(rows) == 1, "指定 CEO 后没有自动开通日报调度"
    assert rows[0].trigger_config == {"time": "09:00"}
    assert str(rows[0].status) == "ACTIVE"


# ---------------------------------------------------------------------------
# 4. 供应商档位：界面可点 10，服务端按档位封顶
# ---------------------------------------------------------------------------

async def test_capacity_caps_requested_parallel_by_provider_tier(
        client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    cases = {"openai_compat": 8, "anthropic": 4, "gemini": 3, "unknown": 2}
    per_agent = {}
    for provider in cases:
        per_agent[provider] = await _agent(client, user, co["id"],
                                           f"负责人-{provider}")

    async with AsyncSessionLocal() as s:
        for provider, agent_id in per_agent.items():
            if provider == "unknown":
                continue
            model = Model(company_id=co["id"], name=f"m-{provider}",
                          provider_type=ModelProviderType(provider),
                          model_id="test-model")
            s.add(model)
            await s.flush()
            s.add(AgentModelConfig(agent_id=agent_id, role="primary",
                                   model_id=model.id))
        await s.commit()

    for provider, expected_cap in cases.items():
        r = await client.get("/ceo/capacity", params={
            "company_id": co["id"], "coordinator_agent_id": per_agent[provider],
            "requested": 10}, headers=user["headers"])
        assert r.status_code == 200, r.text
        body = r.json()
        _assert_keys(body, responses_ceo.CeoCapacityOut, f"capacity {provider}")
        assert body["requested"] == 10
        assert body["provider_cap"] == expected_cap, f"{provider} 档位不对"
        assert MIN_PARALLEL <= body["effective"] <= expected_cap
        assert body["effective"] == min(10, expected_cap, body["worker_free"])


# ---------------------------------------------------------------------------
# 5. 真实子任务收口：短产出判偏离 + 补跑一次 + 报告归档 + 通知
# ---------------------------------------------------------------------------

GOOD_OUTPUT = "## 结论\n线索量可提升 32%，依据是近三个月的渠道转化数据与预算测算。"


async def test_execute_subtask_marks_short_output_off_track(
        make_user, make_company, monkeypatch):
    user = await make_user()
    co = await make_company(user)
    async with AsyncSessionLocal() as s:
        a = Agent(company_id=co["id"], name="小李")
        s.add(a)
        await s.flush()
        t = Task(company_id=co["id"], title="写结论", status=TaskStatus.TODO,
                 assigned_agent_id=a.id, created_by=user["id"])
        s.add(t)
        await s.commit()
        task_id, agent_id = t.id, a.id

    async def _fake_run(db, agent, task, actor_type="agent", extra_system=None):
        assert extra_system and "准绳" in extra_system[0], "锚没有传给子 Agent"
        task.status = TaskStatus.RUNNING
        task.result = "好的"
        await db.commit()

    monkeypatch.setattr("app.agent_runtime.run_agent_on_task", _fake_run)
    out = await ceo_engine.execute_subtask(task_id, agent_id, "准绳：不得编造数据")
    assert out["status"] == "OFF_TRACK"
    assert "过短" in out["off_track_reason"]

    async with AsyncSessionLocal() as s:
        t = await s.get(Task, task_id)
    assert t.status == TaskStatus.COMPLETED
    assert (t.current_stage or "").startswith("off_track")


async def test_mission_runs_retries_off_track_once_and_delivers_report(
        make_user, make_company, client, monkeypatch, queued_jobs):
    plan = _plan(2, ["王总", "小李"])
    _install_fake_chat(monkeypatch, plan, reviews=[
        {"title": "子任务1", "on_track": True, "reason": "符合"},
        {"title": "子任务2", "on_track": False, "reason": "答非所问，缺少预算"},
    ])
    user, co, ids = await _company_with_agents(client, make_user, make_company)
    created = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "把 Q3 线索量提升 30%"},
        headers=user["headers"])
    mission_id = created.json()["mission_id"]

    async with AsyncSessionLocal() as s:
        launched = await ceo_engine.launch_mission(s, mission_id, submit=False)
    assert launched["status"] == "RUNNING"

    calls: list[str] = []

    async def _fake_exec(task_id, agent_id, anchor):
        calls.append(task_id)
        async with AsyncSessionLocal() as s:
            t = await s.get(Task, task_id)
            a = await s.get(Agent, agent_id)
            if t.title.startswith("补跑："):
                t.status = TaskStatus.COMPLETED
                t.result = GOOD_OUTPUT
                await s.commit()
                return {"task_id": task_id, "agent_id": agent_id,
                        "title": t.title, "agent_name": a.name,
                        "status": "COMPLETED", "output": GOOD_OUTPUT,
                        "off_track_reason": None}
            if t.title == "子任务2":
                t.status = TaskStatus.COMPLETED
                t.current_stage = "off_track: 答非所问"
                t.result = "偏题内容"
                await s.commit()
                return {"task_id": task_id, "agent_id": agent_id,
                        "title": t.title, "agent_name": a.name,
                        "status": "OFF_TRACK", "output": "偏题内容",
                        "off_track_reason": "答非所问"}
            t.status = TaskStatus.COMPLETED
            t.result = GOOD_OUTPUT
            await s.commit()
            return {"task_id": task_id, "agent_id": agent_id,
                    "title": t.title, "agent_name": a.name,
                    "status": "COMPLETED", "output": GOOD_OUTPUT,
                    "off_track_reason": None}

    monkeypatch.setattr(ceo_engine, "execute_subtask", _fake_exec)

    async with AsyncSessionLocal() as s:
        result = await ceo_engine.run_mission(s, mission_id)

    assert result["status"] == "COMPLETED", result
    assert len(calls) == 3, f"应执行 2 条原始 + 1 条补跑，实际 {len(calls)}"
    assert calls[:2] != calls[2:], "补跑没有真正执行"

    async with AsyncSessionLocal() as s:
        m = await s.get(CeoMission, mission_id)
        parent = await s.get(Task, m.parent_task_id)
        assets = (await s.execute(
            select(Asset).where(Asset.company_id == co["id"]))).scalars().all()
        notes = (await s.execute(
            select(Notification).where(Notification.company_id == co["id"],
                                       Notification.type == "ceo.mission_done"))
        ).scalars().all()
        retry_rows = (await s.execute(
            select(Task).where(Task.mission_id == mission_id,
                               Task.title.like("补跑：%")))).scalars().all()

    assert m.status == "COMPLETED"
    assert m.retry_count == 1
    assert m.completed_count == 2, "补跑成功后应覆盖原偏离结果"
    assert m.stats["retried"] == 1
    assert parent.status == TaskStatus.COMPLETED
    assert parent.result == m.report
    assert m.report and "## 结论" in m.report
    assert len(retry_rows) == 1, "补跑不能重复创建"
    assert any("CEO汇总报告" in a.name for a in assets), "报告没有归档到成果中心"
    assert m.report_artifact_id
    assert len(notes) == 1 and notes[0].link.startswith("/ceo?mission=")
    assert notes[0].user_id == user["id"]


async def test_resume_includes_subtasks_finished_before_pause(
        make_user, make_company, client, monkeypatch, queued_jobs):
    plan = _plan(2, ["王总", "小李"])
    seen = {"summary": ""}

    async def _fake_chat(db, agent, messages, max_tokens=1600):
        text = messages[-1]["content"]
        if "老板原话：" in text:
            return json.dumps(plan, ensure_ascii=False)
        if "逐条判断产出是否偏离准绳" in text:
            return '{"reviews": []}'
        seen["summary"] = text
        return "## 结论\n两段都已完成。"

    monkeypatch.setattr(ceo_engine, "_chat", _fake_chat)
    user, co, _ = await _company_with_agents(client, make_user, make_company)
    created = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "把 Q3 线索量提升 30%"},
        headers=user["headers"])
    mid = created.json()["mission_id"]
    async with AsyncSessionLocal() as s:
        await ceo_engine.launch_mission(s, mid, submit=False)

    first_half = "前半段产出：渠道数据与预算测算，结论是提升 32%。"
    second_half = "后半段产出：执行排期与负责人分工，按周交付。"
    async with AsyncSessionLocal() as s:
        tasks = (await s.execute(select(Task).where(Task.mission_id == mid)
                                 .order_by(Task.created_at))).scalars().all()
        done, pending = tasks[0], tasks[1]
        done.status = TaskStatus.COMPLETED
        done.result = first_half
        await s.commit()
        pending_id = pending.id

    async def _fake_exec(task_id, agent_id, anchor):
        assert task_id == pending_id
        async with AsyncSessionLocal() as s:
            t = await s.get(Task, task_id)
            a = await s.get(Agent, agent_id)
            t.status = TaskStatus.COMPLETED
            t.result = second_half
            await s.commit()
            return {"task_id": task_id, "agent_id": agent_id,
                    "title": t.title, "agent_name": a.name,
                    "status": "COMPLETED", "output": second_half}

    monkeypatch.setattr(ceo_engine, "execute_subtask", _fake_exec)

    async with AsyncSessionLocal() as s:
        result = await ceo_engine.run_mission(s, mid)

    assert result["status"] == "COMPLETED"
    async with AsyncSessionLocal() as s:
        m = await s.get(CeoMission, mid)
    assert m.completed_count == 2, "暂停前已交付的子任务没有被算进最终报告"
    assert first_half in seen["summary"], "汇总提示词里缺了暂停前的产出"
    assert second_half in seen["summary"]


async def test_reclaim_reruns_after_completion(make_user, make_company, client,
                                               monkeypatch, queued_jobs):
    _install_fake_chat(monkeypatch, _plan(2, ["王总", "小李"]))
    user, co, _ = await _company_with_agents(client, make_user, make_company)
    created = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "写一份增长方案"},
        headers=user["headers"])
    mid = created.json()["mission_id"]

    async with AsyncSessionLocal() as s:
        m = await s.get(CeoMission, mid)
        m.status = "COMPLETED"
        m.retry_count = 1
        t = Task(company_id=co["id"], parent_task_id=m.parent_task_id,
                 mission_id=mid, title="子任务1", status=TaskStatus.FAILED,
                 current_stage="执行失败", assigned_agent_id=m.coordinator_agent_id,
                 created_by=user["id"])
        s.add(t)
        await s.commit()

    r = await client.post(f"/ceo/missions/{mid}/reclaim", headers=user["headers"])
    assert r.status_code == 202, r.text
    _assert_keys(r.json(), responses_ceo.CeoRetryOut)
    assert r.json()["retry_tasks"] == 1
    assert r.json()["status"] == "RUNNING"
    assert len(queued_jobs) == 1


# ---------------------------------------------------------------------------
# 6. 日报
# ---------------------------------------------------------------------------

async def test_digest_endpoints_and_idempotent_schedule(client, make_user,
                                                        make_company):
    user = await make_user()
    co = await make_company(user)

    got = await client.get("/ceo/digest", params={"company_id": co["id"]},
                           headers=user["headers"])
    assert got.status_code == 200, got.text
    _assert_keys(got.json(), responses_ceo.CeoDigestOut, "GET /ceo/digest")
    assert "CEO 日报" in got.json()["markdown"]

    ran = await client.post("/ceo/digest/run", params={"company_id": co["id"]},
                            headers=user["headers"])
    assert ran.status_code == 200, ran.text
    _assert_keys(ran.json(), responses_ceo.CeoDigestOut, "POST /ceo/digest/run")
    async with AsyncSessionLocal() as s:
        notes = (await s.execute(
            select(Notification).where(Notification.company_id == co["id"],
                                       Notification.type == "ceo.digest"))
        ).scalars().all()
    assert len(notes) == 1, "日报没有推送给老板"

    first = await client.post("/ceo/digest/schedule", json={
        "company_id": co["id"], "time": "09:00"}, headers=user["headers"])
    second = await client.post("/ceo/digest/schedule", json={
        "company_id": co["id"], "time": "08:30"}, headers=user["headers"])
    assert first.status_code == second.status_code == 200
    _assert_keys(first.json(), responses_ceo.CeoDigestScheduleOut)
    assert first.json()["created"] is True
    assert second.json()["created"] is False, "重复设置会产生两条日报调度"
    assert second.json()["schedule_id"] == first.json()["schedule_id"]
    assert first.json()["trigger_config"]["time"] == "09:00"

    bad = await client.post("/ceo/digest/schedule", json={
        "company_id": co["id"], "time": "9:00"}, headers=user["headers"])
    assert bad.status_code == 400


async def test_scheduler_executes_ceo_digest_action(client, make_user,
                                                    make_company):
    user = await make_user()
    co = await make_company(user)
    await client.post("/ceo/digest/schedule", json={
        "company_id": co["id"], "time": "09:00"}, headers=user["headers"])

    from app.models.scheduler import AutomationSchedule
    from app import scheduler_engine

    async with AsyncSessionLocal() as s:
        sched = (await s.execute(select(AutomationSchedule).where(
            AutomationSchedule.company_id == co["id"]))).scalars().first()
        assert sched.action_type == "ceo_digest"
        out = await scheduler_engine.execute_action(s, sched)
    assert "generated_at" in out, "ceo_digest 没有被调度器识别"


async def test_viewer_can_read_but_cannot_operate_mission(
        client, make_user, make_company, monkeypatch, queued_jobs):
    _install_fake_chat(monkeypatch, _plan(2, ["王总", "小李"]))
    user, co, _ = await _company_with_agents(client, make_user, make_company)
    created = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "写一份增长方案"},
        headers=user["headers"])
    mid = created.json()["mission_id"]

    viewer = await make_user()
    from app.models.membership import CompanyMember
    async with AsyncSessionLocal() as s:
        s.add(CompanyMember(company_id=co["id"], user_id=viewer["id"],
                            role="viewer", status="active"))
        await s.commit()

    read = await client.get(f"/ceo/missions/{mid}", headers=viewer["headers"])
    assert read.status_code == 200, "只读成员应当能看统筹进度"

    for method, url, payload in [
        ("post", f"/ceo/missions/{mid}/launch", {}),
        ("post", f"/ceo/missions/{mid}/pause", None),
        ("post", f"/ceo/missions/{mid}/cancel", None),
        ("post", f"/ceo/missions/{mid}/instruction", {"text": "改一下"}),
    ]:
        kwargs = {} if payload is None else {"json": payload}
        resp = await getattr(client, method)(url, headers=viewer["headers"],
                                             **kwargs)
        assert resp.status_code == 404, (
            f"只读成员能执行写操作 {url}（{resp.status_code}）")


# ---------------------------------------------------------------------------
# 7. 跨租户：任何 mission 端点都不能被外人碰到
# ---------------------------------------------------------------------------

async def test_cross_tenant_mission_access_is_404(tenants, client, monkeypatch,
                                                  queued_jobs):
    _install_fake_chat(monkeypatch, _plan(2, ["王总", "小李"]))
    alice, co = tenants.alice, tenants.alice_co
    agent = await _agent(client, alice, co["id"], "王总", "总经理")
    r = await client.post("/ceo/config", json={"company_id": co["id"],
                                                "agent_id": agent},
                          headers=alice["headers"])
    assert r.status_code == 200, r.text
    created = await client.post("/ceo/missions", json={
        "company_id": co["id"], "instruction": "写一份增长方案"},
        headers=alice["headers"])
    mid = created.json()["mission_id"]

    endpoints = [
        ("get", f"/ceo/missions/{mid}", None),
        ("get", f"/ceo/missions/{mid}/report", None),
        ("post", f"/ceo/missions/{mid}/pause", None),
        ("post", f"/ceo/missions/{mid}/resume", None),
        ("post", f"/ceo/missions/{mid}/cancel", None),
        ("post", f"/ceo/missions/{mid}/reclaim", None),
        ("post", f"/ceo/missions/{mid}/instruction", {"text": "改一下"}),
        ("post", f"/ceo/missions/{mid}/launch", {}),
    ]
    for method, url, payload in endpoints:
        kwargs = {} if payload is None else {"json": payload}
        resp = await getattr(client, method)(
            url, headers=tenants.bob["headers"], **kwargs)
        assert resp.status_code == 404, f"{method} {url} 泄露了他人统筹：{resp.status_code}"

    listed = await client.get("/ceo/missions", params={"company_id": co["id"]},
                              headers=tenants.bob["headers"])
    assert listed.status_code == 404
    cap = await client.get("/ceo/capacity", params={"company_id": co["id"]},
                           headers=tenants.bob["headers"])
    assert cap.status_code == 404
