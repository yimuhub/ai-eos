"""任务链的工具调用：模型必须能真的取数，而不是凭记忆编。

改造前 `run_agent_on_task` 只调一次 LLM、`tools=None`：
* 模型无法搜索/抓取，"自动抓取数据"全靠编；
* 能力包提示词却写着会 `browser_fetch`（一个不存在的工具）。

现在钉住：
1. 工具定义通过 `tools=` 真正下发；
2. 返回结构化 tool_calls 时执行、回填、再问；
3. 只会以纯文本输出 `<tool_call>` 的弱模型同样能触发工具；
4. 工具失败不拖垮任务，但会记在执行记录的 `tools_used` 里。
"""
from __future__ import annotations

import json

import pytest

from app.agent_runtime import run_agent_on_task
from app.db.session import AsyncSessionLocal
from app.model_gateway import ModelGateway
from app.models.agent import Agent
from app.models.task import AgentExecution, Task
from app.tools import (TOOL_NAMES, execute_tool, parse_text_tool_calls,
                       strip_text_tool_calls)


# ---------------------------------------------------------------------------
# 1) 纯函数：文本形式 tool_call 的解析
# ---------------------------------------------------------------------------
def test_parse_text_tool_calls():
    text = ('先查一下。<tool_call><function=web_search>'
            '{"query":"中国 AI 市场规模"}</function></tool_call>')
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "web_search"
    assert json.loads(calls[0]["function"]["arguments"])["query"] == "中国 AI 市场规模"
    assert strip_text_tool_calls(text).strip() == "先查一下。"


def test_parse_text_tool_calls_noop_without_marker():
    assert parse_text_tool_calls("普通回答") == []
    assert strip_text_tool_calls("普通回答") == "普通回答"


# ---------------------------------------------------------------------------
# 2) 执行器：参数缺失/未知工具/危险地址都返回结构化错误
# ---------------------------------------------------------------------------
async def test_execute_tool_unknown_and_missing_args():
    assert (await execute_tool("rm_rf", {}))["error"].startswith("未知工具")
    assert "query" in (await execute_tool("web_search", {}))["error"]
    assert "url" in (await execute_tool("web_fetch", {}))["error"]


async def test_web_fetch_blocks_internal_addresses():
    """工具参数来自模型（不可信），内网地址必须被拦。"""
    res = await execute_tool("web_fetch", {"url": "http://127.0.0.1:8000/admin"},
                             company_id="c1", agent_id="a1")
    assert "error" in res
    assert "text" not in res


def test_tool_schema_names_are_real():
    """"会浏览器"这类宣传必须对应真实存在的工具名。"""
    assert {"web_search", "web_fetch"} <= TOOL_NAMES


# ---------------------------------------------------------------------------
# 3) 端到端：结构化 tool_calls 会被执行并回填
# ---------------------------------------------------------------------------
async def _seed_agent_task(company_id, user_id, title="调研 AI 市场"):
    async with AsyncSessionLocal() as s:
        a = Agent(id="agent_tool_" + company_id[:6], company_id=company_id,
                  name="调研员", role="市场调研", status="IDLE")
        s.add(a)
        await s.flush()
        t = Task(company_id=company_id, title=title, description="给出结论",
                 assigned_agent_id=a.id, created_by=user_id)
        s.add(t)
        await s.commit()
        return a.id, t.id


async def test_tool_loop_executes_and_records_trace(client, tenants, monkeypatch):
    alice, co = tenants.alice, tenants.alice_co
    agent_id, task_id = await _seed_agent_task(co["id"], alice["id"])

    seen_tools_arg: list = []

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        seen_tools_arg.append(tools)
        if len(seen_tools_arg) == 1:
            return {"content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "AI 市场"})}}]}
        # 第二轮：确认工具结果已经作为 tool 消息进入上下文
        assert any(m.get("role") == "tool" for m in messages)
        return {"content": "据搜索结果显示，市场规模约 1200 亿元。", "usage": {}}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)
    # 不让真实网络介入：搜索工具返回固定结果
    async def fake_skill(slug, payload):
        return {"result": {"query": payload["query"], "results": [
            {"title": "报告", "href": "https://example.com", "body": "1200 亿元"}]}}

    monkeypatch.setattr("app.skill_system.execute_skill", fake_skill)

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_id)
        task = await s.get(Task, task_id)
        execution = await run_agent_on_task(s, agent, task)
        await s.commit()   # 成功路径由调用方（后台执行器）提交

    assert seen_tools_arg[0], "工具定义必须真正下发给模型"
    assert {t["function"]["name"] for t in seen_tools_arg[0]} >= {"web_search", "web_fetch"}
    assert "1200 亿元" in execution.output

    async with AsyncSessionLocal() as s:
        e = await s.get(AgentExecution, execution.id)
        trace = (e.metadata_json or {}).get("tools_used")
    assert trace == [{"name": "web_search", "ok": True, "arg": "AI 市场"}]


async def test_text_form_tool_call_also_runs(client, tenants, monkeypatch):
    """弱模型把 tool_call 写成文本时，工具依然要被执行。"""
    alice, co = tenants.alice, tenants.alice_co
    agent_id, task_id = await _seed_agent_task(co["id"], alice["id"], "查数据")
    calls = []

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": ('<tool_call><function=web_search>'
                                '{"query":"数据"}</function></tool_call>')}
        return {"content": "完成。"}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)

    async def fake_skill(slug, payload):
        raise RuntimeError("搜索后端全挂")

    monkeypatch.setattr("app.skill_system.execute_skill", fake_skill)

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_id)
        task = await s.get(Task, task_id)
        execution = await run_agent_on_task(s, agent, task)
        await s.commit()

    # 工具失败也不能让任务失败
    assert execution.status == "completed"
    assert "<tool_call>" not in (execution.output or "")
    async with AsyncSessionLocal() as s:
        e = await s.get(AgentExecution, execution.id)
        trace = (e.metadata_json or {}).get("tools_used")
    assert trace and trace[0]["ok"] is False


# ---------------------------------------------------------------------------
# 4) 工具轮用尽：绝不能把「空正文」记成已完成
# ---------------------------------------------------------------------------
def _search_call(n: int) -> dict:
    return {"id": f"call_{n}", "type": "function",
            "function": {"name": "web_search",
                         "arguments": json.dumps({"query": f"AI {n}"})}}


async def _fake_search(slug, payload):
    return {"result": {"query": payload["query"], "results": []}}


async def test_tool_budget_exhausted_forces_text_answer(client, tenants, monkeypatch):
    """4 轮工具吃完后模型还在要工具：必须摘掉工具再问一次正文。

    线上实测：本地网关模型连搜 4 轮后正文为空，任务照样被标成
    COMPLETED——派活的人看到状态变绿，点进去什么都没有。
    """
    alice, co = tenants.alice, tenants.alice_co
    agent_id, task_id = await _seed_agent_task(co["id"], alice["id"], "查数据")
    seen_tools: list = []
    rounds = {"n": 0}

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        seen_tools.append(tools)
        if tools is not None:
            rounds["n"] += 1
            return {"content": "", "tool_calls": [_search_call(rounds["n"])]}
        assert messages[-1]["role"] == "user", "收尾必须追加一次明确指令"
        return {"content": "基于已检索到的信息：市场约 1200 亿元。"}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)
    monkeypatch.setattr("app.skill_system.execute_skill", _fake_search)

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_id)
        task = await s.get(Task, task_id)
        execution = await run_agent_on_task(s, agent, task)
        await s.commit()

    assert seen_tools[-1] is None, "收尾必须不带工具再问一次"
    assert execution.status == "completed"
    assert "1200 亿元" in execution.output


async def test_empty_output_still_fails_instead_of_silent_success(
        client, tenants, monkeypatch):
    """连收尾都拿不到正文时按失败上报，不写空成果。"""
    alice, co = tenants.alice, tenants.alice_co
    agent_id, task_id = await _seed_agent_task(co["id"], alice["id"], "查数据")
    rounds = {"n": 0}

    async def fake_chat(self, model_ids, messages, tools=None, config=None):
        if tools is not None:
            rounds["n"] += 1
            return {"content": "", "tool_calls": [_search_call(rounds["n"])]}
        return {"content": "   "}

    monkeypatch.setattr(ModelGateway, "chat_with_agent_models", fake_chat)
    monkeypatch.setattr("app.skill_system.execute_skill", _fake_search)

    async with AsyncSessionLocal() as s:
        agent = await s.get(Agent, agent_id)
        task = await s.get(Task, task_id)
        with pytest.raises(RuntimeError, match="没有返回正文"):
            await run_agent_on_task(s, agent, task)
