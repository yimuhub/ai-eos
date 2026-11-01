"""CEO 周考：监督官自动建人、四维评分、人工覆盖、幂等与租户隔离。

模型层全部替换成确定性返回，验的是**考务编排与安全边界**，不消耗额度。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select

from app import exam_engine as exam
from app.ceo_engine import CeoError
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.exam import CeoExam
from app.models.scheduler import compute_next_run
from app.schemas.responses_exam import (ExamOut, ExamOverviewOut,
                                        ExamScheduleOut)


def _assert_keys(body: dict, model) -> None:
    assert set(body) == set(model.model_fields), (
        f"{model.__name__} 键集合不一致：多出 "
        f"{sorted(set(body) - set(model.model_fields))}，缺少 "
        f"{sorted(set(model.model_fields) - set(body))}")


async def _seed_ceo(company_id: str, user_id: str) -> str:
    async with AsyncSessionLocal() as s:
        ceo = Agent(id=str(uuid.uuid4()), company_id=company_id, name="王总",
                    role="总经理", is_ceo=True, status=AgentStatus.IDLE,
                    created_by_user_id=user_id)
        s.add(ceo)
        await s.commit()
        return ceo.id


def _fake_llm(monkeypatch, *, fail_answers: bool = False) -> list[str]:
    calls: list[str] = []

    async def _fake(db, agent, company_id, system, prompt, max_tokens=1600):
        calls.append(system)
        if system in (exam.SUPERVISOR_SYSTEM, exam.GRADER_SYSTEM):
            if "请出" in prompt:
                return ({
                    "focus": "成本纪律与失败归因",
                    "questions": [
                        {"dimension": "personality",
                         "question": "快与稳冲突时你怎么选？",
                         "rubric": "明确选择并说明代价", "max_score": 10},
                        {"dimension": "cost",
                         "question": "下周 Token 预算怎么拆？",
                         "rubric": "有数字与止损规则", "max_score": 10},
                        {"dimension": "decision",
                         "question": "连续失败三次怎么办？",
                         "rubric": "先归因再决定", "max_score": 10},
                        {"dimension": "execution",
                         "question": "怎么验证产出真的能用？",
                         "rubric": "有验收清单", "max_score": 10},
                    ],
                }, {"tokens": 100, "cost": 0.001})
            return ({
                "grades": [
                    {"id": "q1", "score": 9, "comment": "选择明确"},
                    {"id": "q2", "score": 6, "comment": "数字缺失"},
                    {"id": "q3", "score": 8, "comment": "归因清晰"},
                    {"id": "q4", "score": 7, "comment": "清单可用"},
                ],
                "personality": {
                    "type": "进取型", "risk_posture": "适度进取",
                    "decision_style": "先归因再行动",
                    "traits": ["敢试", "重复盘"],
                    "evidence": ["第三题先归因"], "confidence": "high",
                },
                "summary": "进取但成本纪律需补强",
                "recommendations": ["给成本设硬阈值"],
            }, {"tokens": 100, "cost": 0.001})
        if fail_answers:
            return None, {"tokens": 0, "cost": 0.0}
        return ({
            "answers": [
                {"id": "q1", "answer": "选稳，先小步验证再加速"},
                {"id": "q2", "answer": "下周总量 120 万 Token"},
                {"id": "q3", "answer": "先归因，同类失败两次就换流程"},
                {"id": "q4", "answer": "按验收清单逐条复算"},
            ],
        }, {"tokens": 100, "cost": 0.001})

    monkeypatch.setattr(exam, "_llm_json", _fake)
    return calls


async def _run(company_id: str, user_id: str | None = None) -> dict:
    async with AsyncSessionLocal() as s:
        return await exam.run_exam(s, company_id, user_id=user_id,
                                   source="manual")


# ---------------------------------------------------------------------------
# 周期与调度
# ---------------------------------------------------------------------------

def test_week_key_and_weekly_schedule():
    week_start, week_end, key = exam.week_key(datetime(2026, 11, 1, 15, 30))
    assert week_start.weekday() == 0 and key == "2026-W44"
    assert (week_end - week_start).days == 7

    now = datetime(2026, 11, 4, 12, 0)  # 周三
    nxt = compute_next_run("weekly", {"time": "08:50", "weekday": 0}, now)
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (8, 50)
    assert (nxt.date() - now.date()).days == 5


# ---------------------------------------------------------------------------
# 考务编排
# ---------------------------------------------------------------------------

async def test_run_exam_creates_supervisor_grades_and_is_idempotent(
        tenants, monkeypatch):
    co = tenants.alice_co["id"]
    user = tenants.alice["id"]
    await _seed_ceo(co, user)
    calls = _fake_llm(monkeypatch)

    rec = await _run(co, user)
    assert rec["status"] == "GRADED"
    assert rec["reused"] is False
    assert rec["overall"] == 75.0 and rec["grade"] == "C"
    assert rec["scores"] == {"personality": 90.0, "cost": 60.0,
                             "decision": 80.0, "execution": 70.0}
    assert rec["personality"]["type"] == "进取型"
    assert rec["personality"]["source"] == "supervisor"
    assert [q["id"] for q in rec["questions"]] == ["q1", "q2", "q3", "q4"]
    assert all(a["answered"] for a in rec["answers"])
    assert rec["tokens_used"] == 300
    assert rec["cost"] == 0.003
    assert calls == [exam.SUPERVISOR_SYSTEM, exam.ANSWER_SYSTEM,
                     exam.GRADER_SYSTEM]

    async with AsyncSessionLocal() as s:
        supervisors = (await s.execute(
            select(func.count()).select_from(Agent)
            .where(Agent.company_id == co,
                   Agent.name == exam.SUPERVISOR_NAME))).scalar_one()
        assert supervisors == 1
        assert rec["supervisor_agent_id"]

    again = await _run(co, user)
    assert again["reused"] is True and again["id"] == rec["id"]

    # 周考结果会成为周度深度复盘的输入
    from app import self_evolution as evo
    async with AsyncSessionLocal() as s:
        ws, we, _ = evo.period_bounds("weekly")
        metrics = await evo.collect_metrics(s, co, ws, we)
    assert metrics["exam"]["overall"] == 75.0


async def test_exam_skips_without_fake_scores_when_ceo_model_unavailable(
        tenants, monkeypatch):
    co = tenants.alice_co["id"]
    user = tenants.alice["id"]
    await _seed_ceo(co, user)
    _fake_llm(monkeypatch, fail_answers=True)

    rec = await _run(co, user)
    assert rec["status"] == "SKIPPED"
    assert rec["overall"] is None and rec["grade"] is None
    assert "CEO 模型不可用" in (rec["error"] or "")
    assert rec["questions"], "监督官不可用时仍应保留确定性题面"
    assert rec["answers"] is None or rec["answers"] == [] or not any(
        a["answered"] for a in rec["answers"])


async def test_owner_override_recomputes_and_keeps_audit(tenants,
                                                         monkeypatch):
    co = tenants.alice_co["id"]
    user = tenants.alice["id"]
    await _seed_ceo(co, user)
    _fake_llm(monkeypatch)
    rec = await _run(co, user)

    async with AsyncSessionLocal() as s:
        row = await s.get(CeoExam, rec["id"])
        out = await exam.override_exam(
            s, row, user_id=user,
            grades=[{"id": "q2", "score": 10, "comment": "老板补认可"}],
            personality={"type": "稳健型"},
            note="成本方案我认可")

    assert out["status"] == "OVERRIDDEN"
    assert out["overall"] == 85.0 and out["grade"] == "B"
    assert out["personality"]["type"] == "稳健型"
    assert out["personality"]["source"] == "owner"
    assert out["override"]["previous"]["overall"] == 75.0
    assert out["override"]["note"] == "成本方案我认可"

    async with AsyncSessionLocal() as s:
        row = await s.get(CeoExam, rec["id"])
        assert row.override["by"] == user
        assert row.grades[1]["score"] == 10.0


async def test_run_exam_requires_a_ceo(tenants):
    co = tenants.alice_co["id"]
    try:
        await _run(co, tenants.alice["id"])
    except CeoError as exc:
        assert exc.status_code == 409 and "CEO" in exc.message
    else:  # pragma: no cover - 没有 CEO 必须报错，而不是悄悄造一场考试
        raise AssertionError("没有 CEO 时周考应当拒绝执行")

    async with AsyncSessionLocal() as s:
        count = (await s.execute(
            select(func.count()).select_from(CeoExam)
            .where(CeoExam.company_id == co))).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# HTTP 契约与租户隔离
# ---------------------------------------------------------------------------

async def test_exam_http_contract_and_tenant_isolation(tenants, monkeypatch,
                                                       client):
    alice, bob = tenants.alice, tenants.bob
    co = tenants.alice_co["id"]
    await _seed_ceo(co, alice["id"])
    _fake_llm(monkeypatch)
    rec = await _run(co, alice["id"])

    r = await client.get(f"/exams?company_id={co}", headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == set(ExamOverviewOut.model_fields)
    _assert_keys(body["latest"], ExamOut)
    _assert_keys(body["exams"][0], ExamOut)
    assert body["supervisor"]["name"] == exam.SUPERVISOR_NAME
    for dim in body["dimensions"]:
        assert set(dim) == {"key", "label"}

    r = await client.get(f"/exams/{rec['id']}", headers=alice["headers"])
    assert r.status_code == 200
    _assert_keys(r.json(), ExamOut)

    r = await client.get(f"/exams/{rec['id']}", headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post("/exams/run", json={"company_id": co},
                          headers=bob["headers"])
    assert r.status_code == 404
    r = await client.post(f"/exams/{rec['id']}/override",
                          json={"overall": 10}, headers=bob["headers"])
    assert r.status_code == 404
    r = await client.get(f"/exams?company_id={co}", headers=bob["headers"])
    assert r.status_code == 404

    r = await client.post("/exams/schedules", json={"company_id": co},
                          headers=alice["headers"])
    assert r.status_code == 200
    assert len(r.json()["schedules"]) == 1
    _assert_keys(r.json()["schedules"][0], ExamScheduleOut)
    assert r.json()["created"]["ceo_exam_weekly"]["created"] is True
    r = await client.post("/exams/schedules", json={"company_id": co},
                          headers=alice["headers"])
    assert r.json()["created"]["ceo_exam_weekly"]["created"] is False
