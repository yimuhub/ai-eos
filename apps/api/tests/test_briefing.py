"""CEO 晨间简报：数字必须对得上时区，字段必须齐。

原实现用本地 `datetime.now()` 去比 naive UTC 时间戳，"昨日/今日"窗口
在 UTC+8 下整体偏移 8 小时：早上打开简报时，凌晨干完的活不算在昨天，
前晚的活却混进来。今天成本也按错误的起点统计。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes.briefing import today_start_utc
from app.db.session import AsyncSessionLocal
from app.models.task import Task, TaskStatus
from app.models.token_usage import TokenUsage


def test_today_start_is_local_midnight_in_utc():
    start = today_start_utc()
    assert start.tzinfo is None, "存库口径必须是 naive UTC"
    local = datetime.now().astimezone()
    # 本地 0 点换算成 UTC 后，与"现在"的差应等于今天已过去的时间
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - start)
    assert abs(elapsed.total_seconds() - (
        local.hour * 3600 + local.minute * 60 + local.second)) < 5


async def test_briefing_counts_only_today_cost(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(company_id=co["id"], model_id="m1", scenario="TASK",
                         input_tokens=100, output_tokens=50,
                         total_tokens=150, total_cost=1.5,
                         created_at=now_utc))
        # 48 小时前：无论当前几点，都必定早于"本地今天 0 点"
        s.add(TokenUsage(company_id=co["id"], model_id="m1", scenario="TASK",
                         input_tokens=900, output_tokens=100,
                         total_tokens=1000, total_cost=9.0,
                         created_at=now_utc - timedelta(hours=48)))
        await s.commit()

    r = await client.get(f"/briefing?company_id={co['id']}", headers=alice["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_company"] is True
    assert body["cost_today"]["tokens"] == 150, body["cost_today"]
    assert body["cost_today"]["cost"] == 1.5


async def test_briefing_surfaces_failed_and_stuck_tasks(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as s:
        s.add(Task(company_id=co["id"], title="报销自动化", status=TaskStatus.FAILED,
                   created_by=alice["id"], updated_at=now_utc))
        s.add(Task(company_id=co["id"], title="竞品分析", status=TaskStatus.RUNNING,
                   created_by=alice["id"],
                   updated_at=now_utc - timedelta(hours=5)))
        await s.commit()

    r = await client.get(f"/briefing?company_id={co['id']}", headers=alice["headers"])
    assert r.status_code == 200, r.text
    kinds = [i["kind"] for i in r.json()["needs_you"]["items"]]
    assert "失败任务" in kinds
    assert "疑似卡住" in kinds


async def test_briefing_requires_company_ownership(client, tenants):
    r = await client.get(f"/briefing?company_id={tenants.bob_co['id']}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 404
