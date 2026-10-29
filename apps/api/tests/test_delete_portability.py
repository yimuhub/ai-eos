"""默认 SQLite 部署下删除功能的可用性护栏。

回归背景（都是真实故障）：

* 5 处 ``DELETE ... USING`` 是 PostgreSQL/MySQL 语法，SQLite 直接
  ``near "using": syntax error``；异常又被包成"约束冲突"，用户看到的是
  误导性的外键错误，任务/员工/项目批量删除与离职交接全部失败。
* 批量删除在 ``async with db.begin_nested()`` 的 except 里调用
  ``await db.rollback()``：回滚的是整个外层事务，把此前成功的删除一起
  撤销，但 ``deleted`` 列表仍把它们报成成功。
* offboarding 用 SQLite 专属的 ``randomblob()/hex()`` 生成记忆 id，
  PostgreSQL 上必然失败。
* 列表接口存在 N+1（agents 逐个查模型绑定、projects 逐项目两次 count）。

因此这里的断言不止看 200/返回结构，而是**直接查库确认返回值是否真实**。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.agent import (Agent, AgentModelConfig, Model,
                              ModelProviderType)
from app.models.company import Company
from app.models.memory import AgentMemory, MemoryType
from app.models.project import Project
from app.models.task import (AgentExecution, ExecutionCheckpoint, Task,
                             TaskPriority, TaskStatus)
from app.models.workflow import WorkflowDefinition, WorkflowStatus

# ---------------------------------------------------------------------------
# 直接查库的小工具（每次新开会话，保证看到的是端点提交后的真实状态）
# ---------------------------------------------------------------------------
async def _fetch(model_cls, pk):
    async with AsyncSessionLocal() as db:
        return await db.get(model_cls, pk)


async def _count(stmt) -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(stmt)).scalar() or 0


async def _bindings_of(agent_id: str) -> list[AgentModelConfig]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == agent_id))
        return list(rows.scalars())


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------
async def _create_agent(client, headers, company_id, name="员工",
                        department_id=None) -> dict:
    r = await client.post("/agents", json={
        "company_id": company_id, "name": name,
        "department_id": department_id}, headers=headers)
    assert r.status_code == 201, f"建员工失败: {r.status_code} {r.text}"
    return r.json()


async def _create_task(client, headers, company_id, title="任务",
                       parent_task_id=None) -> dict:
    payload = {"company_id": company_id, "title": title}
    if parent_task_id:
        payload["parent_task_id"] = parent_task_id
    r = await client.post("/tasks", json=payload, headers=headers)
    assert r.status_code == 201, f"建任务失败: {r.status_code} {r.text}"
    return r.json()


async def _create_project(client, headers, company_id, name="项目") -> dict:
    r = await client.post("/projects", json={
        "name": name, "company_id": company_id}, headers=headers)
    assert r.status_code == 201, f"建项目失败: {r.status_code} {r.text}"
    return r.json()


async def _seed_execution(agent_id: str, task_id: str,
                          project_id: str | None = None):
    """造一条执行记录 + 检查点，用于验证 USING→IN 的跨方言删除。"""
    async with AsyncSessionLocal() as db:
        ex = AgentExecution(agent_id=agent_id, task_id=task_id,
                            project_id=project_id, status="completed")
        db.add(ex)
        await db.flush()
        cp = ExecutionCheckpoint(execution_id=ex.id,
                                 checkpoint_type="planning")
        db.add(cp)
        await db.commit()
        return ex.id, cp.id


async def _seed_memory(agent_id: str, company_id: str,
                       context_ref: str | None = None,
                       content: str = "记忆原文") -> str:
    mid = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(AgentMemory(id=mid, agent_id=agent_id, company_id=company_id,
                           memory_type=MemoryType.EPISODIC, content=content,
                           importance=0.6, context_ref=context_ref))
        await db.commit()
    return mid


# ---------------------------------------------------------------------------
# 1) 任务批量删除
# ---------------------------------------------------------------------------
async def test_task_batch_delete_works_on_sqlite(client, make_user,
                                                 make_company):
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    agent = await _create_agent(client, user["headers"], cid)
    t1 = await _create_task(client, user["headers"], cid, "任务一")
    t2 = await _create_task(client, user["headers"], cid, "任务二")

    ex_id, cp_id = await _seed_execution(agent["id"], t1["id"])
    mem_id = await _seed_memory(agent["id"], cid, context_ref=t1["id"])

    r = await client.post("/task-dispatch/delete", json={
        "task_ids": [t1["id"], t2["id"]]}, headers=user["headers"])
    assert r.status_code == 200, f"批量删除任务失败: {r.status_code} {r.text}"
    body = r.json()
    assert body["deleted_count"] == 2, body
    assert {x["id"] for x in body["deleted"]} == {t1["id"], t2["id"]}
    assert body["failed"] == [], body

    # 返回值必须真实：deleted 里的任务与关联执行/检查点/记忆都要查不到
    assert await _fetch(Task, t1["id"]) is None
    assert await _fetch(Task, t2["id"]) is None
    assert await _fetch(AgentExecution, ex_id) is None
    assert await _fetch(ExecutionCheckpoint, cp_id) is None
    assert await _fetch(AgentMemory, mem_id) is None


async def test_task_batch_delete_failure_keeps_earlier_success(
        client, make_user, make_company):
    """savepoint 失败不能撤销此前成功项（防 db.rollback() 回归）。"""
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    agent = await _create_agent(client, user["headers"], cid)

    good = await _create_task(client, user["headers"], cid, "应删成功")
    bad = await _create_task(client, user["headers"], cid, "应删失败")
    # 子任务 + 执行记录：删父任务时子任务被直接 DELETE，
    # 其执行记录仍引用子任务 → 外键冲突，制造一条真实的失败。
    child = await _create_task(client, user["headers"], cid, "子任务",
                               parent_task_id=bad["id"])
    child_ex_id, _ = await _seed_execution(agent["id"], child["id"])

    r = await client.post("/task-dispatch/delete", json={
        "task_ids": [good["id"], bad["id"]]}, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body["deleted"]] == [good["id"]], body
    assert body["deleted_count"] == 1, body
    assert any(x["id"] == bad["id"] for x in body["failed"]), body

    # 关键断言：成功项真的被删掉；失败项及其子任务原样保留
    assert await _fetch(Task, good["id"]) is None, "此前成功删除的任务被回滚了"
    assert await _fetch(Task, bad["id"]) is not None
    assert await _fetch(Task, child["id"]) is not None
    assert await _fetch(AgentExecution, child_ex_id) is not None


# ---------------------------------------------------------------------------
# 2) 员工列表 N+1 + 批量删除
# ---------------------------------------------------------------------------
async def test_agents_list_groups_model_configs(client, make_user,
                                                make_company):
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    a1 = await _create_agent(client, user["headers"], cid, "甲")
    a2 = await _create_agent(client, user["headers"], cid, "乙")

    async with AsyncSessionLocal() as db:
        m = Model(company_id=cid, name="测试模型",
                  provider_type=ModelProviderType.OPENAI_COMPAT,
                  model_id="test-model")
        db.add(m)
        await db.flush()
        db.add_all([
            AgentModelConfig(agent_id=a1["id"], role="primary", model_id=m.id),
            AgentModelConfig(agent_id=a1["id"], role="fallback", model_id=m.id),
            AgentModelConfig(agent_id=a2["id"], role="primary", model_id=m.id),
        ])
        await db.commit()

    r = await client.get("/agents", params={"company_id": cid},
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    by_id = {a["id"]: a for a in r.json()}
    assert {(x["role"], x["model_id"]) for x in
            by_id[a1["id"]]["model_configs"]} == {("primary", m.id),
                                                  ("fallback", m.id)}
    assert {(x["role"], x["model_id"]) for x in
            by_id[a2["id"]]["model_configs"]} == {("primary", m.id)}


async def test_agent_batch_delete_works_and_counts_are_true(
        client, make_user, make_company):
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    a1 = await _create_agent(client, user["headers"], cid, "甲")
    a2 = await _create_agent(client, user["headers"], cid, "乙")
    missing = str(uuid.uuid4())

    r = await client.post("/agents/batch-delete", json={
        "agent_ids": [a1["id"], missing, a2["id"]]}, headers=user["headers"])
    assert r.status_code == 200, f"批量删除员工失败: {r.status_code} {r.text}"
    body = r.json()
    assert set(body["deleted"]) == {a1["id"], a2["id"]}, body
    assert body["deleted_count"] == 2, body
    assert [x["id"] for x in body["failed"]] == [missing], body

    assert await _fetch(Agent, a1["id"]) is None
    assert await _fetch(Agent, a2["id"]) is None
    assert await _bindings_of(a1["id"]) == []


# ---------------------------------------------------------------------------
# 3) 项目列表聚合 + 批量删除
# ---------------------------------------------------------------------------
async def test_projects_list_progress_and_batch_delete(client, make_user,
                                                       make_company):
    user = await make_user()
    company = await make_company(user)
    cid, uid = company["id"], user["id"]
    p1 = await _create_project(client, user["headers"], cid, "项目一")
    p2 = await _create_project(client, user["headers"], cid, "项目二")

    async with AsyncSessionLocal() as db:
        db.add_all([
            Task(company_id=cid, project_id=p1["id"], title="完成",
                 status=TaskStatus.COMPLETED, priority=TaskPriority.NORMAL,
                 created_by=uid),
            Task(company_id=cid, project_id=p1["id"], title="待办",
                 status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                 created_by=uid),
            Task(company_id=cid, project_id=p2["id"], title="完成",
                 status=TaskStatus.COMPLETED, priority=TaskPriority.NORMAL,
                 created_by=uid),
        ])
        await db.commit()

    r = await client.get("/projects", params={"company_id": cid},
                         headers=user["headers"])
    assert r.status_code == 200, r.text
    by_id = {p["id"]: p for p in r.json()}
    assert (by_id[p1["id"]]["task_total"],
            by_id[p1["id"]]["task_completed"],
            by_id[p1["id"]]["progress"]) == (2, 1, 50)
    assert (by_id[p2["id"]]["task_total"],
            by_id[p2["id"]]["task_completed"],
            by_id[p2["id"]]["progress"]) == (1, 1, 100)

    # 批量删项目：p1 成功；p2 被工作流定义引用（无级联）而失败。
    # 顺序把成功项放前面，专门验证失败不会回滚已成功项。
    async with AsyncSessionLocal() as db:
        db.add(WorkflowDefinition(company_id=cid, project_id=p2["id"],
                                  name="阻断删除的工作流",
                                  status=WorkflowStatus.DRAFT,
                                  created_by=uid))
        await db.commit()

    r = await client.post("/projects/batch-delete", json={
        "project_ids": [p1["id"], p2["id"]]}, headers=user["headers"])
    assert r.status_code == 200, f"批量删除项目失败: {r.status_code} {r.text}"
    body = r.json()
    assert [x["id"] for x in body["deleted"]] == [p1["id"]], body
    assert body["deleted_count"] == 1, body
    assert any(x["id"] == p2["id"] for x in body["failed"]), body

    assert await _fetch(Project, p1["id"]) is None, "成功删除的项目被回滚了"
    assert await _fetch(Project, p2["id"]) is not None


async def test_project_delete_via_lifecycle_erases_checkpoints(
        client, make_user, make_company):
    """项目生命周期 delete 分支同样不能再出现 DELETE ... USING。"""
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    agent = await _create_agent(client, user["headers"], cid)
    project = await _create_project(client, user["headers"], cid)
    task = await _create_task(client, user["headers"], cid, "项目任务")
    # 任务通过 ORM 挂到项目上，并造执行记录 + 检查点
    async with AsyncSessionLocal() as db:
        t = await db.get(Task, task["id"])
        t.project_id = project["id"]
        await db.commit()
    ex_id, cp_id = await _seed_execution(agent["id"], task["id"],
                                         project_id=project["id"])

    r = await client.post(f"/projects/{project['id']}/lifecycle",
                          json={"action": "delete"}, headers=user["headers"])
    assert r.status_code == 200, f"生命周期删除失败: {r.status_code} {r.text}"
    assert await _fetch(Project, project["id"]) is None
    assert await _fetch(Task, task["id"]) is None
    assert await _fetch(AgentExecution, ex_id) is None
    assert await _fetch(ExecutionCheckpoint, cp_id) is None


# ---------------------------------------------------------------------------
# 4) 部门批量删除
# ---------------------------------------------------------------------------
async def test_department_batch_delete_detaches_agents(client, make_user,
                                                       make_company):
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]

    r = await client.post(f"/departments?cid={cid}",
                          json={"name": "研发部"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    dept = r.json()
    agent = await _create_agent(client, user["headers"], cid, "研发员工",
                                department_id=dept["id"])
    missing = str(uuid.uuid4())

    r = await client.post("/departments/batch-delete", json={
        "department_ids": [dept["id"], missing]}, headers=user["headers"])
    assert r.status_code == 200, f"批量删除部门失败: {r.status_code} {r.text}"
    body = r.json()
    assert [x["id"] for x in body["deleted"]] == [dept["id"]], body
    assert body["deleted_count"] == 1, body
    assert [x["id"] for x in body["failed"]] == [missing], body

    from app.models.company import Department
    assert await _fetch(Department, dept["id"]) is None
    refreshed = await _fetch(Agent, agent["id"])
    assert refreshed is not None and refreshed.department_id is None


# ---------------------------------------------------------------------------
# 5) 离职交接（randomblob -> uuid4）
# ---------------------------------------------------------------------------
async def test_offboarding_memory_inheritance_on_sqlite(client, make_user,
                                                        make_company):
    user = await make_user()
    company = await make_company(user)
    cid = company["id"]
    successor = await _create_agent(client, user["headers"], cid, "接班人")
    leaver = await _create_agent(client, user["headers"], cid, "离职者")
    src_mem_id = await _seed_memory(leaver["id"], cid, content="客户偏好周报")

    r = await client.post("/agents/offboarding/execute", json={
        "agent_ids": [leaver["id"]],
        "successor_id": successor["id"]}, headers=user["headers"])
    assert r.status_code == 200, f"离职交接失败: {r.status_code} {r.text}"
    body = r.json()
    assert body["offboarded_count"] == 1, body
    assert [x["id"] for x in body["offboarded"]] == [leaver["id"]], body
    assert body["failed"] == [], body

    assert await _fetch(Agent, leaver["id"]) is None
    assert await _fetch(AgentMemory, src_mem_id) is None

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(AgentMemory).where(
            AgentMemory.agent_id == successor["id"]))).scalars().all()
    assert len(rows) == 1, f"记忆未继承，实际 {len(rows)} 条"
    inherited = rows[0]
    assert inherited.content == "[来自「离职者」的传承] 客户偏好周报"
    # id 由 Python uuid.uuid4() 生成：可解析、非 SQLite 专属 hex(randomblob)
    assert len(inherited.id) == 36
    uuid.UUID(inherited.id)
    assert inherited.id != src_mem_id


# ---------------------------------------------------------------------------
# 6) 公司批量删除：每家公司独立事务，deleted 只在提交后计数
# ---------------------------------------------------------------------------
async def test_company_manage_batch_delete_counts_only_committed(
        client, make_user, make_company):
    user = await make_user()
    c1 = await make_company(user, name="甲集团")
    c2 = await make_company(user, name="乙集团")
    missing = str(uuid.uuid4())

    r = await client.post("/company-manage/batch-delete", json={
        "company_ids": [c1["id"], missing, c2["id"]]},
        headers=user["headers"])
    assert r.status_code == 200, f"批量删除公司失败: {r.status_code} {r.text}"
    body = r.json()
    assert body["deleted_count"] == 2, body
    assert [x["id"] for x in body["failed"]] == [missing], body

    assert await _fetch(Company, c1["id"]) is None
    assert await _fetch(Company, c2["id"]) is None
    assert await _count(select(func.count()).select_from(Company)) == 0
