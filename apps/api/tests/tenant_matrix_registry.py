"""A1「自动权限矩阵」资源注册表。

设计目标
--------
把「外部租户拿不到、属主拿得到」从零散的手写用例，变成一张**可扩展的表**：

* 每个 :class:`ResourceSpec` 描述一个资源族，并用真实接口（owner 的 token）
  创建一个资源实例；
* 每个 :class:`EndpointSpec` 描述该资源族里 1~3 个「按资源 id 读取/修改/删除」
  的端点，以及属主预期状态码；
* ``test_tenant_matrix.py`` 只负责遍历这张表并做三类断言：
  属主正向对照、外部租户 404/403、写端点未改动属主数据。

约定
----
* ``path_template`` 里可用的占位符：``{id}``（资源 id）、``{company_id}``
  （属主公司 id），以及 ``create()`` 返回的 body 里任意字符串字段
  （例如记忆族的 ``{agent_id}``）；
* ``create()`` 用 owner 的 headers 真实调用创建接口，返回
  ``(resource_id, body)``；``body["_marker"]`` 是一个本族唯一的随机标记，
  测试用它断言越权响应里没有泄露资源内容；
* ``json`` / ``query`` / ``observe_path`` 里的字符串同样会用上述占位符填充；
* ``observe_path`` 是属主可读的「同一资源」读取端点，写端点用它做
  「越权前后内容不变」的对照；列表型响应按资源 id 过滤后再比较；
* 需要 LLM / 后台线程 / 定时器才有意义的端点不放进 ``endpoints``，
  统一登记在 ``SKIPPED_SURFACES`` 并写明原因。

注册表只描述**已经能同步完成**的 CRUD / 查询端点，测试稳定、可重复。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

CreateFn = Callable[["MatrixCtx"], Awaitable[tuple[str, dict]]]


@dataclass
class MatrixCtx:
    """创建资源所需的上下文：全部走真实 ASGI 客户端与真实鉴权。"""

    client: httpx.AsyncClient
    owner: dict                  # tenants.alice（含 headers）
    owner_company: dict          # tenants.alice_co
    attacker: dict               # tenants.bob
    attacker_company: dict       # tenants.bob_co

    @property
    def owner_headers(self) -> dict:
        return self.owner["headers"]

    @property
    def attacker_headers(self) -> dict:
        return self.attacker["headers"]

    @property
    def owner_company_id(self) -> str:
        return self.owner_company["id"]

    @property
    def attacker_company_id(self) -> str:
        return self.attacker_company["id"]


@dataclass(frozen=True)
class EndpointSpec:
    """一个「按资源 id 操作」的端点。"""

    method: str
    path_template: str
    ok_status: int = 200
    #: 请求体模板；属主正向调用时使用（写端点必填）
    json: dict | None = None
    #: 额外查询参数模板（如 memory 族的 agent_id）
    query: dict | None = None
    #: 端点是否接受 company_id 查询参数：是则额外覆盖
    #: 「用 bob 自己的 company_id」与「用 alice 的 company_id」两种攻击变体
    company_query: bool = False
    #: 属主读取同一资源的路径（写端点做「未被改动」对照）
    observe_path: str | None = None
    #: 属主正向读响应里是否必须出现 marker（防止端点返回空壳还判绿）
    expect_marker: bool = False
    note: str = ""


@dataclass(frozen=True)
class ResourceSpec:
    """一个资源族：create + 按 id 的端点集合。"""

    name: str
    create: CreateFn
    endpoints: tuple[EndpointSpec, ...]
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# 创建辅助
# ---------------------------------------------------------------------------
def new_marker(prefix: str = "MATRIX") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _assert_created(resp: httpx.Response, what: str, expected: int | tuple = 201) -> dict:
    ok = resp.status_code in expected if isinstance(expected, tuple) else resp.status_code == expected
    if not ok:
        raise AssertionError(
            f"[registry] 创建{what}失败：期望 {expected}，实得 {resp.status_code}，"
            f"响应={resp.text[:300]!r}")
    try:
        return resp.json()
    except ValueError:
        return {}


async def _make_agent(ctx: MatrixCtx, name: str) -> str:
    r = await ctx.client.post("/agents", json={
        "company_id": ctx.owner_company_id, "name": name, "role": "矩阵测试",
    }, headers=ctx.owner_headers)
    return _assert_created(r, "Agent")["id"]


# ---------------------------------------------------------------------------
# 各资源族的 create
# ---------------------------------------------------------------------------
async def _create_agent(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("AGENT")
    body = _assert_created(await ctx.client.post("/agents", json={
        "company_id": ctx.owner_company_id,
        "name": f"矩阵员工-{marker}",
        "role": "矩阵测试",
        "description": f"desc-{marker}",
    }, headers=ctx.owner_headers), "Agent")
    return body["id"], {**body, "_marker": marker}


async def _create_company(ctx: MatrixCtx) -> tuple[str, dict]:
    # 公司由 tenants fixture 建好；这里直接复用 alice 的公司。
    return ctx.owner_company_id, {
        **ctx.owner_company, "_marker": ctx.owner_company.get("name", ""),
    }


async def _create_department(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("DEPT")
    r = await ctx.client.post("/departments", params={
        "cid": ctx.owner_company_id,
    }, json={"name": f"矩阵部门-{marker}"}, headers=ctx.owner_headers)
    body = _assert_created(r, "部门")
    return body["id"], {**body, "_marker": marker}


async def _create_project(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("PROJ")
    body = _assert_created(await ctx.client.post("/projects", json={
        "company_id": ctx.owner_company_id,
        "name": f"矩阵项目-{marker}",
    }, headers=ctx.owner_headers), "项目")
    return body["id"], {**body, "_marker": marker}


async def _create_task(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("TASK")
    body = _assert_created(await ctx.client.post("/tasks", json={
        "company_id": ctx.owner_company_id,
        "title": f"矩阵任务-{marker}",
    }, headers=ctx.owner_headers), "任务")
    return body["id"], {**body, "_marker": marker}


async def _create_meeting(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("MEET")
    agent_id = await _make_agent(ctx, f"会议员工-{marker}")
    body = _assert_created(await ctx.client.post("/meetings", json={
        "company_id": ctx.owner_company_id,
        "title": f"矩阵会议-{marker}",
        "participant_ids": [agent_id],
    }, headers=ctx.owner_headers), "会议")
    return body["id"], {**body, "_marker": marker, "agent_id": agent_id}


async def _create_chat_group(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("GROUP")
    agent_id = await _make_agent(ctx, f"群聊员工-{marker}")
    body = _assert_created(await ctx.client.post("/chat-groups", json={
        "company_id": ctx.owner_company_id,
        "name": f"矩阵群聊-{marker}",
        "agent_ids": [agent_id],
    }, headers=ctx.owner_headers), "群聊")
    return body["id"], {**body, "_marker": marker, "agent_id": agent_id}


async def _create_kb2_doc(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("KB2DOC")
    agent_id = await _make_agent(ctx, f"知识库员工-{marker}")
    # 该端点不返回文档 id，只能建完再从列表里按标题找回。
    r = await ctx.client.post(f"/kb2/agents/{agent_id}/documents", json={
        "title": f"矩阵文档-{marker}",
        "content": f"私人知识库正文 {marker}",
    }, headers=ctx.owner_headers)
    _assert_created(r, "kb2 文档", expected=(200, 201))

    r = await ctx.client.get(f"/kb2/agents/{agent_id}/documents",
                             headers=ctx.owner_headers)
    _assert_created(r, "kb2 文档列表", expected=200)
    rows = r.json()
    match = next((d for d in rows if marker in (d.get("title") or "")), None)
    if match is None:
        raise AssertionError(f"[registry] 建完 kb2 文档后在列表里找不到：{r.text[:300]!r}")
    return match["id"], {"_marker": marker, "agent_id": agent_id}


async def _create_knowledge_doc(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("KBDOC")
    body = _assert_created(await ctx.client.post(
        "/knowledge/documents",
        params={"company_id": ctx.owner_company_id},
        json={"title": f"矩阵知识-{marker}", "content": f"知识正文 {marker}"},
        headers=ctx.owner_headers,
    ), "知识文档", expected=(200, 201))
    return body["id"], {**body, "_marker": marker}


async def _create_asset(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("ASSET")
    body = _assert_created(await ctx.client.post(
        "/assets/upload",
        params={"company_id": ctx.owner_company_id},
        files={"file": ("matrix.txt", f"资产正文 {marker}".encode(), "text/plain")},
        headers=ctx.owner_headers,
    ), "资产", expected=201)
    return body["id"], {**body, "_marker": marker}


async def _create_artifact(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("ART")
    body = _assert_created(await ctx.client.post(
        "/artifacts",
        params={"company_id": ctx.owner_company_id},
        json={"name": f"矩阵成果-{marker}", "content": f"成果正文 {marker}"},
        headers=ctx.owner_headers,
    ), "成果", expected=(200, 201))
    return body["id"], {**body, "_marker": marker}


async def _create_workflow(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("WF")
    body = _assert_created(await ctx.client.post(
        "/workflows",
        params={"company_id": ctx.owner_company_id},
        json={
            "name": f"矩阵工作流-{marker}",
            "nodes": [
                {"node_key": "start", "node_type": "START", "name": "开始"},
                {"node_key": "end", "node_type": "END", "name": "结束"},
            ],
            "edges": [{"from_node": "start", "to_node": "end"}],
        },
        headers=ctx.owner_headers,
    ), "工作流", expected=(200, 201))
    return body["id"], {**body, "_marker": marker}


async def _create_member(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("MEMBER")
    suffix = uuid.uuid4().hex[:10]
    body = _assert_created(await ctx.client.post(
        f"/companies/{ctx.owner_company_id}/members",
        json={
            "email": f"matrix_{suffix}@example.com",
            "username": f"mx{suffix}",
            "password": "Test1234pass",
            "display_name": f"矩阵成员-{marker}",
            "role": "member",
        },
        headers=ctx.owner_headers,
    ), "公司成员", expected=201)
    return body["id"], {**body, "_marker": marker}


async def _create_memory(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("MEM")
    agent_id = await _make_agent(ctx, f"记忆员工-{marker}")
    body = _assert_created(await ctx.client.post(
        "/memory/remember",
        params={"company_id": ctx.owner_company_id},
        json={
            "agent_id": agent_id,
            "memory_type": "EPISODIC",
            "content": f"矩阵记忆正文 {marker}",
        },
        headers=ctx.owner_headers,
    ), "记忆", expected=200)
    if not body.get("id"):
        raise AssertionError(f"[registry] 记忆未被存入：{body!r}")
    return body["id"], {
        "id": body["id"], "memory_id": body["id"],
        "agent_id": agent_id, "_marker": marker,
    }


async def _create_automation(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("AUTO")
    body = _assert_created(await ctx.client.post(
        "/automations",
        params={"company_id": ctx.owner_company_id},
        json={"name": f"矩阵自动化-{marker}", "steps": []},
        headers=ctx.owner_headers,
    ), "自动化", expected=201)
    return body["id"], {**body, "_marker": marker}


async def _create_approval_request(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("APPROVAL")
    body = _assert_created(await ctx.client.post(
        "/intervention/requests",
        params={"company_id": ctx.owner_company_id},
        json={
            "action_type": "task_create",
            "payload": {"reason": marker},
            "reason": f"矩阵审批 {marker}",
        },
        headers=ctx.owner_headers,
    ), "审批请求", expected=200)
    rid = body.get("request_id")
    if not rid:
        raise AssertionError(
            f"[registry] 审批请求未落库（策略把 task_create 判为自动放行？）：{body!r}")
    return rid, {"_marker": marker}


async def _create_doc_style(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("STYLE")
    body = _assert_created(await ctx.client.post(
        "/doc-styles",
        params={"company_id": ctx.owner_company_id},
        json={"name": f"矩阵风格-{marker}", "hint": f"hint-{marker}"},
        headers=ctx.owner_headers,
    ), "文档风格", expected=(200, 201))
    return body["id"], {**body, "_marker": marker}


async def _create_budget(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("BUDGET")
    body = _assert_created(await ctx.client.post(
        "/token-budgets",
        params={"company_id": ctx.owner_company_id},
        json={"limit_tokens": 1000, "period": "day", "note": f"note-{marker}"},
        headers=ctx.owner_headers,
    ), "token 预算", expected=(200, 201))
    return body["id"], {**body, "_marker": marker}


async def _create_schedule(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("SCHED")
    # 用 daily 而不是 interval：interval <=30s 会把 next_run_at 设成 now，
    # 一旦测试进程里存在调度循环就会真实执行动作，观察值不再稳定。
    body = _assert_created(await ctx.client.post(
        "/schedules",
        params={"company_id": ctx.owner_company_id},
        json={
            "name": f"矩阵调度-{marker}",
            "trigger_type": "daily",
            "time": "03:00",
            "action_type": "snapshot_performance",
        },
        headers=ctx.owner_headers,
    ), "调度", expected=200)
    return body["id"], {**body, "_marker": marker}


async def _create_mailbox_message(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("MAIL")
    agent_id = await _make_agent(ctx, f"邮箱员工-{marker}")
    alias = f"matrix-{uuid.uuid4().hex[:10]}@aieos.local"
    mailbox = _assert_created(await ctx.client.post("/mailboxes", json={
        "agent_id": agent_id, "alias": alias,
    }, headers=ctx.owner_headers), "邮箱", expected=200)
    body = _assert_created(await ctx.client.post("/mailboxes/messages", json={
        "mailbox_id": mailbox["id"],
        "subject": f"矩阵来信-{marker}",
        "body": f"邮件正文 {marker}",
        "from_addr": "client@example.com",
    }, headers=ctx.owner_headers), "邮件", expected=200)
    return body["id"], {
        **body, "mailbox_id": mailbox["id"], "agent_id": agent_id,
        "_marker": marker,
    }


async def _create_learning_record(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("LEARN")
    agent_id = await _make_agent(ctx, f"学习员工-{marker}")
    body = _assert_created(await ctx.client.post(
        f"/learning/agents/{agent_id}/learning-records",
        json={"goal": f"矩阵学习目标-{marker}", "plan": {"steps": 3}},
        headers=ctx.owner_headers,
    ), "学习记录", expected=201)
    return body["id"], {
        "id": body["id"], "agent_id": agent_id, "_marker": marker,
    }


async def _create_growth_plan(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("PLAN")
    agent_id = await _make_agent(ctx, f"计划员工-{marker}")
    r = await ctx.client.post(
        "/growth-center/plans",
        params={"company_id": ctx.owner_company_id},
        json={"title": f"矩阵学习计划-{marker}", "agent_ids": [agent_id],
              "target_skills": ["矩阵技能"]},
        headers=ctx.owner_headers,
    )
    _assert_created(r, "学习计划", expected=201)
    # 创建接口只返回数量，回列表里按标题找回 plan_id。
    r = await ctx.client.get(
        "/growth-center/plans",
        params={"company_id": ctx.owner_company_id},
        headers=ctx.owner_headers)
    _assert_created(r, "学习计划列表", expected=200)
    match = next((p for p in r.json()
                  if marker in (p.get("title") or "")), None)
    if match is None:
        raise AssertionError(f"[registry] 建完学习计划后找不到：{r.text[:300]!r}")
    return match["id"], {
        "id": match["id"], "agent_id": agent_id, "_marker": marker,
    }


async def _create_custom_skill(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("SKILL")
    body = _assert_created(await ctx.client.post(
        "/skills/custom",
        params={"company_id": ctx.owner_company_id},
        json={"name": f"矩阵技能-{marker}", "description": f"desc-{marker}",
              "scope": "exclusive"},
        headers=ctx.owner_headers,
    ), "自定义技能", expected=201)
    return body["id"], {**body, "_marker": marker}


async def _create_agent_review(ctx: MatrixCtx) -> tuple[str, dict]:
    marker = new_marker("REVIEW")
    agent_id = await _make_agent(ctx, f"考核员工-{marker}")
    _assert_created(await ctx.client.post(
        f"/reviews/agents/{agent_id}",
        json={"source": "USER", "score": 88,
              "comment": f"矩阵考核意见 {marker}"},
        headers=ctx.owner_headers,
    ), "考核", expected=201)
    # 本族的「资源 id」是 Agent：端点按 agent_id 列出其全部考核记录。
    return agent_id, {"id": agent_id, "agent_id": agent_id, "_marker": marker}


# ---------------------------------------------------------------------------
# 资源族注册表
#
# 每个族只放 1~3 个「按 id」端点。刻意排除：
#   - 触发 LLM / 后台执行器的端点（/meetings/{id}/start、/workflows/{id}/run…）
#   - 需要异步产物才有意义的端点（会议纪要、作战室小结、交付 zip）
# ---------------------------------------------------------------------------
RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        name="agents",
        create=_create_agent,
        endpoints=(
            EndpointSpec("GET", "/agents/{id}", 200, expect_marker=True),
            EndpointSpec("PATCH", "/agents/{id}", 200,
                         json={"description": "matrix-updated"},
                         observe_path="/agents/{id}"),
            EndpointSpec("DELETE", "/agents/{id}", 204,
                         observe_path="/agents/{id}"),
        ),
    ),
    ResourceSpec(
        name="delivery_agent_folder",
        create=_create_agent,
        endpoints=(
            # 曾因 import 不存在的 agent_chat._owned 对属主也 500；
            # 单独成族，避免挤占 agents 族的 3 个端点上限。
            EndpointSpec("GET", "/delivery/agents/{id}/folder", 200,
                         expect_marker=True),
        ),
    ),
    ResourceSpec(
        name="companies",
        create=_create_company,
        endpoints=(
            EndpointSpec("GET", "/companies/{company_id}", 200, expect_marker=True),
            EndpointSpec("PATCH", "/companies/{company_id}", 200,
                         json={"name": "A 公司（矩阵改名）"},
                         observe_path="/companies/{company_id}"),
            EndpointSpec("GET", "/companies/{company_id}/departments", 200),
        ),
    ),
    ResourceSpec(
        name="departments",
        create=_create_department,
        endpoints=(
            EndpointSpec("PATCH", "/departments/{id}", 200,
                         json={"name": "矩阵部门（改名）"},
                         observe_path="/companies/{company_id}/departments"),
            EndpointSpec("DELETE", "/departments/{id}", 204,
                         observe_path="/companies/{company_id}/departments"),
        ),
    ),
    ResourceSpec(
        name="projects",
        create=_create_project,
        endpoints=(
            EndpointSpec("GET", "/projects/{id}", 200, expect_marker=True),
            EndpointSpec("PATCH", "/projects/{id}", 200,
                         json={"description": "matrix-updated"},
                         observe_path="/projects/{id}"),
            EndpointSpec("DELETE", "/projects/{id}", 204,
                         observe_path="/projects/{id}"),
        ),
    ),
    ResourceSpec(
        name="tasks",
        create=_create_task,
        endpoints=(
            EndpointSpec("GET", "/tasks/{id}", 200, expect_marker=True),
            EndpointSpec("PATCH", "/tasks/{id}", 200,
                         json={"description": "matrix-updated"},
                         observe_path="/tasks/{id}"),
        ),
    ),
    ResourceSpec(
        name="meetings",
        create=_create_meeting,
        endpoints=(
            EndpointSpec("GET", "/meetings/{id}", 200, expect_marker=True),
        ),
    ),
    ResourceSpec(
        name="chat_groups",
        create=_create_chat_group,
        endpoints=(
            EndpointSpec("PATCH", "/chat-groups/{id}", 200,
                         json={"name": "矩阵群聊（改名）"},
                         observe_path="/chat-groups?company_id={company_id}"),
            EndpointSpec("DELETE", "/chat-groups/{id}", 204,
                         observe_path="/chat-groups?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="kb2_documents",
        create=_create_kb2_doc,
        endpoints=(
            EndpointSpec("PATCH", "/kb2/documents/{id}", 200,
                         json={"title": "矩阵文档（改名）", "content": "matrix-updated"},
                         observe_path="/kb2/agents/{agent_id}/documents"),
            EndpointSpec("DELETE", "/kb2/documents/{id}", 200,
                         observe_path="/kb2/agents/{agent_id}/documents"),
        ),
    ),
    ResourceSpec(
        name="knowledge_documents",
        create=_create_knowledge_doc,
        endpoints=(
            EndpointSpec("GET", "/knowledge/documents/{id}", 200,
                         expect_marker=True,
                         observe_path="/knowledge/documents/{id}"),
            EndpointSpec("DELETE", "/knowledge/documents/{id}", 200,
                         observe_path="/knowledge/documents/{id}"),
        ),
    ),
    ResourceSpec(
        name="assets",
        create=_create_asset,
        endpoints=(
            EndpointSpec("GET", "/assets/{id}", 200, expect_marker=True),
            EndpointSpec("GET", "/assets/{id}/download", 200, expect_marker=True),
            # 交付中心下载走的是另一套 scope 实现，同样按资源 id 鉴权
            EndpointSpec("GET", "/delivery/download/{id}", 200, expect_marker=True),
        ),
    ),
    ResourceSpec(
        name="artifacts",
        create=_create_artifact,
        endpoints=(
            EndpointSpec("GET", "/artifacts/{id}", 200, expect_marker=True),
            EndpointSpec("DELETE", "/artifacts/{id}", 200,
                         observe_path="/artifacts/{id}"),
        ),
    ),
    ResourceSpec(
        name="workflows",
        create=_create_workflow,
        endpoints=(
            EndpointSpec("GET", "/workflows/{id}", 200, expect_marker=True),
            EndpointSpec("DELETE", "/workflows/{id}", 204,
                         observe_path="/workflows/{id}"),
        ),
    ),
    ResourceSpec(
        name="company_members",
        create=_create_member,
        endpoints=(
            EndpointSpec("PATCH", "/companies/{company_id}/members/{id}", 200,
                         json={"role": "viewer"},
                         observe_path="/companies/{company_id}/members"),
            EndpointSpec("DELETE", "/companies/{company_id}/members/{id}", 204,
                         observe_path="/companies/{company_id}/members"),
        ),
    ),
    ResourceSpec(
        name="agent_memories",
        create=_create_memory,
        endpoints=(
            # company_id 是查询参数：分别覆盖 bob 自己的公司与 alice 的公司
            EndpointSpec("GET", "/memory/recall", 200,
                         query={"agent_id": "{agent_id}"},
                         company_query=True,
                         observe_path=("/memory/recall?company_id={company_id}"
                                       "&agent_id={agent_id}"),
                         expect_marker=True),
            EndpointSpec("POST", "/memory/remember", 200,
                         json={"agent_id": "{agent_id}",
                               "memory_type": "EPISODIC",
                               "content": "matrix-updated"},
                         company_query=True,
                         observe_path=("/memory/recall?company_id={company_id}"
                                       "&agent_id={agent_id}")),
            EndpointSpec("DELETE", "/memory/{agent_id}/{id}", 200,
                         observe_path=("/memory/recall?company_id={company_id}"
                                       "&agent_id={agent_id}")),
        ),
    ),
    ResourceSpec(
        name="automations",
        create=_create_automation,
        endpoints=(
            EndpointSpec("PATCH", "/automations/{id}", 200,
                         json={"action": "pause"},
                         observe_path="/automations?company_id={company_id}"),
            EndpointSpec("POST", "/automations/{id}/enable", 200,
                         observe_path="/automations?company_id={company_id}"),
            EndpointSpec("GET", "/automations/{id}/runs", 200),
        ),
    ),
    ResourceSpec(
        name="approvals",
        create=_create_approval_request,
        endpoints=(
            EndpointSpec("POST", "/intervention/requests/{id}/decide", 200,
                         json={"decision": "approve"},
                         observe_path="/intervention/requests?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="doc_styles",
        create=_create_doc_style,
        endpoints=(
            EndpointSpec("PATCH", "/doc-styles/{id}", 200,
                         json={"name": "矩阵风格（改名）", "hint": "matrix-updated"},
                         company_query=True,
                         observe_path="/doc-styles?company_id={company_id}"),
            EndpointSpec("DELETE", "/doc-styles/{id}", 200,
                         company_query=True,
                         observe_path="/doc-styles?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="token_budgets",
        create=_create_budget,
        endpoints=(
            EndpointSpec("PUT", "/token-budgets/{id}", 200,
                         json={"limit_tokens": 2000},
                         company_query=True,
                         observe_path="/token-budgets?company_id={company_id}"),
            EndpointSpec("DELETE", "/token-budgets/{id}", 200,
                         company_query=True,
                         observe_path="/token-budgets?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="schedules",
        create=_create_schedule,
        endpoints=(
            EndpointSpec("POST", "/schedules/{id}/pause", 200,
                         observe_path="/schedules?company_id={company_id}"),
            EndpointSpec("POST", "/schedules/{id}/resume", 200,
                         observe_path="/schedules?company_id={company_id}"),
            # 运行历史没有 company_id 可挂 router 级门禁，必须按 schedule
            # 反查公司；此前任何登录用户拿 sid 就能读到别家调度历史。
            EndpointSpec("GET", "/schedules/{id}/runs", 200),
        ),
    ),
    ResourceSpec(
        name="mailbox_messages",
        create=_create_mailbox_message,
        endpoints=(
            EndpointSpec("POST", "/mailboxes/messages/{id}/read", 200,
                         observe_path="/mailboxes/messages?mailbox_id={mailbox_id}"),
            EndpointSpec("POST", "/mailboxes/messages/{id}/reply", 200,
                         json={"body": "矩阵回复"},
                         observe_path="/mailboxes/messages?mailbox_id={mailbox_id}"),
        ),
    ),
    ResourceSpec(
        name="learning_records",
        create=_create_learning_record,
        endpoints=(
            EndpointSpec("PATCH", "/learning/learning-records/{id}", 200,
                         json={"progress": 50},
                         observe_path="/learning/agents/{agent_id}/profile"),
        ),
    ),
    ResourceSpec(
        name="growth_center_plans",
        create=_create_growth_plan,
        endpoints=(
            # company_id 是查询参数：覆盖「bob 自己的公司」与「alice 的公司」
            EndpointSpec("PATCH", "/growth-center/plans/{id}", 200,
                         query={"status": "completed"},
                         company_query=True,
                         observe_path="/growth-center/plans?company_id={company_id}"),
            EndpointSpec("DELETE", "/growth-center/plans/{id}", 200,
                         company_query=True,
                         observe_path="/growth-center/plans?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="custom_skills",
        create=_create_custom_skill,
        endpoints=(
            EndpointSpec("PATCH", "/skills/{id}/scope", 200,
                         query={"scope": "public"},
                         company_query=True,
                         observe_path="/skills?company_id={company_id}"),
        ),
    ),
    ResourceSpec(
        name="agent_reviews",
        create=_create_agent_review,
        endpoints=(
            # 列表端点只带 agent_id，router 级 require_company_scope 直接跳过：
            # 必须由端点自己按 Agent 反查公司，否则能看到别家员工的考核分。
            EndpointSpec("GET", "/reviews/agents/{id}", 200,
                         expect_marker=True),
        ),
    ),
)


# ---------------------------------------------------------------------------
# 明确跳过、且写明原因的接口面（不是「忘了测」，是「同步跑不稳/无意义」）
# ---------------------------------------------------------------------------
SKIPPED_SURFACES: tuple[tuple[str, str], ...] = (
    ("war_room", "作战室会话的 /start /say /finalize 依赖 LLM 与后台推进，"
                 "同步断言只能测到「已受理」，覆盖不到越权读写结果"),
    ("meeting_minutes", "会议纪要 / action-items 要等 /meetings/{id}/start 跑完 LLM 轮次，"
                        "产物异步生成；改用会议本体 GET 覆盖同一张表的读隔离"),
    ("workflow_execution", "POST /workflows/{id}/run 返回 202 后由后台线程推进执行，"
                           "执行记录断言依赖时序；只测定义（workflows 族）"),
    ("automation_run", "POST /automations/{id}/run 会真实执行步骤，"
                       "TEST 模式也依赖引擎实现细节；只测定义与运行历史"),
    ("delivery_agent_folder", "GET /delivery/agents/{aid}/folder 运行时 import "
                              "agent_chat._owned，但该函数已不存在，属主调用也会 500；"
                              "这不是租户隔离问题，已单独报告，矩阵不收录"),
    ("performance_reflection", "这两族的越权回归已由 tests/test_tenant_hardening_round5.py "
                               "逐条钉死，矩阵不重复"),
    ("training_sessions", "POST /training/sessions 创建后立即调用 LLM 提取学习点，"
                          "无模型密钥时属主正向对照就是 502，不满足「同步可重复」；"
                          "其材料上传与按 id 读取已有独立归属用例覆盖"),
)


def cases() -> list[tuple[ResourceSpec, EndpointSpec]]:
    """展开成 (资源族, 端点) 列表，供 pytest 参数化。"""
    return [(spec, ep) for spec in RESOURCE_SPECS for ep in spec.endpoints]


__all__: list[str] = [
    "EndpointSpec",
    "MatrixCtx",
    "RESOURCE_SPECS",
    "ResourceSpec",
    "SKIPPED_SURFACES",
    "cases",
    "new_marker",
]

# 让静态检查（pyflakes）看到 typing 别名确实被使用
_ = (Any, Awaitable, Callable, CreateFn)
