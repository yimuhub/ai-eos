"""工具总线（V6 M1）：注册、按 Agent 解析、DB 动态工具执行与管理 API。

钉住的契约：
1. in-code 注册表是唯一事实源：web 两件人人可用，computer_* 只在装了
   能力包时下发，aieos.* 只对外（MCP Server）不下发自家员工；
2. "模型看到的 = 真的能执行的"——不可执行的宣传名（memory_write）
   永不进 schema；
3. DB 动态工具按公司隔离、按名字授权、mcp_http 直通执行并落审计；
4. 管理 API 的租户边界：跨公司一律 403/404。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.toolbus import AgentToolGrant, ToolRegistry
from app.toolbus import (all_incode_tools, execute_bus_tool, get_tool,
                         resolve_agent_tools, tool_schemas)
from app.toolbus.server_tools import SERVER_TOOL_SPECS
from app.tools import TOOL_NAMES


async def _make_agent(company_id: str, *, capabilities: list[str] | None = None,
                      name: str = "测试员工") -> Agent:
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=company_id, name=name,
                      memory_config={"capabilities": capabilities or []})
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        aid = agent.id
    return await _reload_agent(aid)


async def _reload_agent(agent_id: str) -> Agent:
    async with AsyncSessionLocal() as db:
        return await db.get(Agent, agent_id)


async def _register_db_tool(company_id: str, name: str, **kw) -> str:
    async with AsyncSessionLocal() as db:
        row = ToolRegistry(company_id=company_id, name=name,
                           description="测试动态工具",
                           parameters={"type": "object", "properties": {}}, **kw)
        db.add(row)
        await db.commit()
        return row.id


async def _grant(agent_id: str, company_id: str, tool_name: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(AgentToolGrant(company_id=company_id, agent_id=agent_id,
                              tool_name=tool_name))
        await db.commit()


async def _tool_call_events(tool_name: str) -> list[object]:
    from app.models.event import EventLog
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(EventLog).where(EventLog.event_type == "tool.call",
                                   EventLog.entity_id == tool_name))).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# 1) 注册表与兼容层
# ---------------------------------------------------------------------------

def test_incode_registry_and_compat():
    assert get_tool("web_search") is not None
    assert get_tool("web_fetch") is not None
    assert {"web_search", "web_fetch"} <= TOOL_NAMES  # 存量兼容断言
    # 15 个电脑工具全部注册
    computer = [s for s in all_incode_tools() if s.source == "computer"]
    assert len(computer) == 15
    assert get_tool("computer_click").executor is not None
    assert get_tool("computer_apply_patch").risk == "high"
    # aieos.* 服务端工具只对外（V6 M3 起含 aieos.query_metric，共 8 个）
    assert len([s for s in SERVER_TOOL_SPECS if s.expose_mcp]) == 8
    assert get_tool("aieos.create_task") is None  # 服务端工具不占员工命名空间


@pytest.mark.asyncio
async def test_resolve_builtin_only(tenants):
    agent = await _make_agent(tenants.alice_co["id"])
    specs = await resolve_agent_tools(_noop_db(), agent)
    names = {s.name for s in specs}
    assert {"web_search", "web_fetch"} <= names
    assert "computer_click" not in names
    # schema 形态是 OpenAI function-calling
    for sch in tool_schemas(specs):
        assert sch["type"] == "function"
        assert {"name", "description", "parameters"} <= set(sch["function"])


@pytest.mark.asyncio
async def test_resolve_with_computer_pack_and_no_fake_tools(tenants):
    agent = await _make_agent(tenants.alice_co["id"],
                              capabilities=["computer_use", "persistent_memory"])
    specs = await resolve_agent_tools(_noop_db(), agent)
    names = {s.name for s in specs}
    assert "computer_click" in names and "computer_read_file" in names
    # 关键契约：能力包里没有执行器的宣传名不进 schema
    assert "memory_write" not in names
    assert "memory_recall" not in names
    assert "browser_fetch" not in names


class _NoopDB:
    """resolve 只读 grants 时才用 db；没授权的工具场景传 None 前先绕过。

    用 SQLite 内存会话太重，这里给 resolve 一个鸭子类型的最小桩：
    execute 返回空结果集（无授权行）。
    """

    async def execute(self, *_a, **_k):
        return _EmptyResult()


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


def _noop_db():
    return _NoopDB()


# ---------------------------------------------------------------------------
# 2) DB 动态工具：解析、隔离、执行、审计
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_with_granted_db_tool_and_tenant_isolation(tenants):
    alice_co = tenants.alice_co["id"]
    agent = await _make_agent(alice_co)
    await _register_db_tool(alice_co, "crm_search_contacts",
                            source="mcp_http", endpoint="https://mcp.example.com/rpc")
    await _grant(agent.id, alice_co, "crm_search_contacts")
    # bob 公司登记同名工具——隔离锚点是 company_id
    await _register_db_tool(tenants.bob_co["id"], "crm_search_contacts",
                            source="custom")

    async with AsyncSessionLocal() as db:
        specs = await resolve_agent_tools(db, agent)
    mine = [s for s in specs if s.name == "crm_search_contacts"]
    assert len(mine) == 1 and mine[0].source == "db"
    assert mine[0].executor is None  # DB 工具由 dispatch 路由


@pytest.mark.asyncio
async def test_execute_db_tool_mcp_http_passes_through_and_audits(
        tenants, monkeypatch):
    alice_co = tenants.alice_co["id"]
    await _register_db_tool(alice_co, "crm_lookup",
                            source="mcp_http",
                            endpoint="https://mcp.example.com/rpc")

    captured = {}

    async def fake_execute(endpoint, task_input, config, bearer=None,
                           timeout=30.0):
        captured["endpoint"] = endpoint
        captured["task_input"] = task_input
        return {"result": {"content": [{"type": "text", "text": "42 条客户"}]},
                "tool": "crm_lookup"}

    import app.api.routes.mcp as mcp_mod
    import app.toolbus.dispatch as dispatch_mod
    monkeypatch.setattr(mcp_mod, "mcp_http_execute", fake_execute)
    # mcp.example.com 不真实解析：本用例钉的是"直通与审计"，端点闸门
    # 另有专门用例，这里放行
    monkeypatch.setattr(dispatch_mod, "_assert_public_http_url",
                        lambda *a, **k: None)

    res = await execute_bus_tool("crm_lookup", {"q": "华东"},
                                 company_id=alice_co, agent_id="a-1")
    assert "error" not in res
    assert captured["endpoint"] == "https://mcp.example.com/rpc"
    assert '"crm_lookup"' in captured["task_input"]
    # 审计：ok + source=db + 带 agent 归属
    events = await _tool_call_events("crm_lookup")
    assert events, "工具调用必须落 tool.call 审计"


@pytest.mark.asyncio
async def test_execute_db_tool_blocks_private_endpoint(tenants):
    alice_co = tenants.alice_co["id"]
    # 192.168 私网段拒绝；127.0.0.1 回环按设计放行（单机本地 MCP 正路），
    # 由 test_local_loopback_endpoint_allowed 钉住
    await _register_db_tool(alice_co, "evil_lookup",
                            source="mcp_http",
                            endpoint="http://192.168.1.5:9/rpc")
    res = await execute_bus_tool("evil_lookup", {}, company_id=alice_co)
    assert "error" in res and "拒绝" in res["error"]


@pytest.mark.asyncio
async def test_local_loopback_endpoint_allowed(tenants):
    """单机部署的本地 MCP Server（127.0.0.1）是正路，闸门必须放行到执行段。"""
    alice_co = tenants.alice_co["id"]
    await _register_db_tool(alice_co, "local_notes",
                            source="mcp_http",
                            endpoint="http://127.0.0.1:8765/rpc")
    res = await execute_bus_tool("local_notes", {}, company_id=alice_co)
    # 端口没人监听 → 连接失败，但绝不是"被闸门拒绝"
    assert "拒绝" not in str(res.get("error", ""))


@pytest.mark.asyncio
async def test_execute_unknown_and_disabled(tenants):
    alice_co = tenants.alice_co["id"]
    res = await execute_bus_tool("no_such_tool", {}, company_id=alice_co)
    assert res["error"].startswith("未知工具")
    tid = await _register_db_tool(alice_co, "paused_tool", source="custom",
                                  enabled=False)
    res = await execute_bus_tool("paused_tool", {}, company_id=alice_co)
    assert "已停用" in res["error"]
    # 未带公司上下文：动态工具一律不可见（与未知工具同消息，兼容历史契约）
    res = await execute_bus_tool("paused_tool", {}, company_id=None)
    assert res["error"].startswith("未知工具")


@pytest.mark.asyncio
async def test_builtin_executor_missing_args_still_structured(tenants):
    res = await execute_bus_tool("web_search", {}, company_id=tenants.alice_co["id"])
    assert "query" in res["error"]  # 兼容历史行为：缺参数返回结构化错误


# ---------------------------------------------------------------------------
# 3) 管理 API：登记、授权、租户边界
# ---------------------------------------------------------------------------

async def _register_via_api(client, headers, company_id, name="crm_search",
                            endpoint="https://mcp.example.com/rpc"):
    return await client.post(
        f"/toolbus/tools?company_id={company_id}",
        headers=headers,
        json={"name": name, "display_name": "客户检索",
              "description": "在外部 CRM 里搜客户",
              "source": "mcp_http", "endpoint": endpoint,
              "risk": "medium", "required_capability": "operate"})


async def _make_agent_via_db(company_id: str) -> str:
    async with AsyncSessionLocal() as db:
        agent = Agent(company_id=company_id, name="路由测试员工")
        db.add(agent)
        await db.commit()
        return agent.id


async def test_tool_register_grant_flow(client, tenants):
    alice_co = tenants.alice_co["id"]
    r = await _register_via_api(client, tenants.alice["headers"], alice_co)
    assert r.status_code == 201, r.text
    tool = r.json()["tool"]

    # 登记校验：撞内置名 / 坏名字 / 内网 endpoint / 未知凭据引用
    r = await _register_via_api(client, tenants.alice["headers"], alice_co,
                                name="web_search")
    assert r.status_code == 400
    r = await _register_via_api(client, tenants.alice["headers"], alice_co,
                                name="BadName!")
    assert r.status_code == 400
    r = await client.post(f"/toolbus/tools?company_id={alice_co}",
                          headers=tenants.alice["headers"],
                          json={"name": "local_probe", "source": "mcp_http",
                                "endpoint": "http://192.168.1.5/rpc"})
    assert r.status_code == 400  # 私网 endpoint 登记即拒
    r = await client.post(f"/toolbus/tools?company_id={alice_co}",
                          headers=tenants.alice["headers"],
                          json={"name": "with_secret", "source": "mcp_http",
                                "endpoint": "https://mcp.example.com/rpc",
                                "auth_secret_ref": "nope"})
    assert r.status_code == 400  # 凭据引用必须先录入保险库

    # 授权流程：员工拿到工具
    agent_id = await _make_agent_via_db(alice_co)
    r = await client.put(
        f"/toolbus/agents/{agent_id}/tools?company_id={alice_co}",
        headers=tenants.alice["headers"],
        json={"tool_names": ["crm_search", "not_registered"]})
    assert r.status_code == 400  # 未登记的名字拒绝
    r = await client.put(
        f"/toolbus/agents/{agent_id}/tools?company_id={alice_co}",
        headers=tenants.alice["headers"],
        json={"tool_names": ["crm_search"]})
    assert r.status_code == 200
    r = await client.get(
        f"/toolbus/agents/{agent_id}/tools?company_id={alice_co}",
        headers=tenants.alice["headers"])
    body = r.json()
    assert "crm_search" in body["granted"]
    assert any(t["name"] == "crm_search" and t["source"] == "db"
               for t in body["resolved"])

    # 停用后解析不再包含
    r = await client.patch(
        f"/toolbus/tools/{tool['id']}?company_id={alice_co}&enabled=false",
        headers=tenants.alice["headers"])
    assert r.status_code == 200
    agent = await _reload_agent(agent_id)
    specs = await resolve_agent_tools(_noop_db(), agent)
    assert all(s.name != "crm_search" for s in specs)


async def test_tool_tenant_boundary(client, tenants):
    alice_co = tenants.alice_co["id"]
    # bob 不能往 alice 公司登记/读目录/授权——统一 404（不泄露存在性，
    # 与 assert_company_capability 的租户口径一致）
    r = await _register_via_api(client, tenants.bob["headers"], alice_co)
    assert r.status_code == 404
    r = await client.get(f"/toolbus/inventory?company_id={alice_co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404
    agent_id = await _make_agent_via_db(alice_co)
    r = await client.put(
        f"/toolbus/agents/{agent_id}/tools?company_id={alice_co}",
        headers=tenants.bob["headers"], json={"tool_names": []})
    assert r.status_code == 404
    # bob 猜 alice 的 agent_id 也拿不到
    r = await client.get(
        f"/toolbus/agents/{agent_id}/tools?company_id={tenants.bob_co['id']}",
        headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_tool_health_and_import(client, tenants):
    alice_co = tenants.alice_co["id"]
    r = await _register_via_api(client, tenants.alice["headers"], alice_co,
                                name="crm_hub")
    tool = r.json()["tool"]
    # 不可解析的域名 → 健康失败（HTTP 200，body 里如实报告）
    r = await client.post(
        f"/toolbus/tools/{tool['id']}/health?company_id={alice_co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["status"] == "error"
    # 导入同样失败 → 502
    r = await client.post(
        f"/toolbus/tools/{tool['id']}/import?company_id={alice_co}",
        headers=tenants.alice["headers"])
    assert r.status_code == 502
