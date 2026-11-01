"""B1 互联网/SaaS 行业包：模板结构 + 列表计数 + 幂等铺设 + 租户隔离。"""
from __future__ import annotations

import json
import pathlib

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.task import Task, TaskPriority, TaskStatus

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PRESET_PATH = REPO_ROOT / "content" / "org_presets" / "saas.json"


def _load() -> dict:
    return json.loads(PRESET_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) 静态结构：3 条交付物模板 + 10 条任务模板
# ---------------------------------------------------------------------------
def test_saas_preset_departments_structure():
    preset = _load()
    assert preset["id"] == "saas"
    assert preset["industry"] == "互联网/SaaS"
    departments = preset["departments"]
    assert 4 <= len(departments) <= 5

    names = [dp["name"] for dp in departments]
    assert len(names) == len(set(names))
    for dp in departments:
        agents = dp["agents"]
        assert 2 <= len(agents) <= 3, dp["name"]
        agent_names = [a["name"] for a in agents]
        assert len(agent_names) == len(set(agent_names)), dp["name"]
        for agent in agents:
            assert agent["name"].strip()
            assert agent["role"].strip()


def test_saas_deliverable_templates_are_exactly_three():
    templates = _load()["deliverable_templates"]
    assert len(templates) == 3
    keys = [t["key"] for t in templates]
    assert len(keys) == len(set(keys))
    for tpl in templates:
        assert tpl["key"].strip()
        assert tpl["name"].strip()
        assert tpl["artifact_type"].strip()
        outline = tpl["outline"]
        assert isinstance(outline, list) and len(outline) >= 3
        assert all(isinstance(s, str) and s.strip() for s in outline)


def test_saas_task_templates_are_exactly_ten_and_executable():
    preset = _load()
    tasks = preset["task_templates"]
    assert len(tasks) == 10
    keys = [t["key"] for t in tasks]
    titles = [t["title"] for t in tasks]
    assert len(keys) == len(set(keys))
    assert len(titles) == len(set(titles))

    dept_roles: dict[str, set[str]] = {}
    for dp in preset["departments"]:
        dept_roles[dp["name"]] = {a["name"] for a in dp["agents"]}

    for tpl in tasks:
        assert tpl["key"].strip()
        title = tpl["title"].strip()
        assert len(title) >= 10, f"任务标题太泛：{title}"
        assert tpl["description"].strip()
        assert TaskPriority(tpl["priority"]) in TaskPriority
        assert tpl["department"] in dept_roles
        assert tpl["role"] in dept_roles[tpl["department"]]


# ---------------------------------------------------------------------------
# 2) 列表摘要带模板计数，详情原样返回新字段
# ---------------------------------------------------------------------------
async def test_list_and_detail_expose_template_counts(client):
    r = await client.get("/org-presets")
    assert r.status_code == 200, r.text
    saas = next(item for item in r.json() if item["id"] == "saas")
    assert saas["deliverable_template_count"] == 3
    assert saas["task_template_count"] == 10

    detail = await client.get("/org-presets/saas")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert len(body["deliverable_templates"]) == 3
    assert len(body["task_templates"]) == 10


# ---------------------------------------------------------------------------
# 3) scaffold：dry_run 不落库、真实铺设幂等、跨租户 404
# ---------------------------------------------------------------------------
async def _counts(company_id: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as s:
        deliverables = int((await s.execute(
            select(func.count()).select_from(Artifact).where(
                Artifact.company_id == company_id))).scalar() or 0)
        tasks = int((await s.execute(
            select(func.count()).select_from(Task).where(
                Task.company_id == company_id))).scalar() or 0)
    return deliverables, tasks


async def test_scaffold_dry_run_writes_nothing(client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)

    r = await client.post("/org-presets/saas/scaffold", json={
        "company_id": company["id"], "dry_run": True,
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["created"] == {"deliverables": 3, "tasks": 10}
    assert body["skipped"] == {"deliverables": 0, "tasks": 0}
    assert await _counts(company["id"]) == (0, 0)


async def test_scaffold_creates_then_skips_on_second_run(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)

    first = await client.post("/org-presets/saas/scaffold", json={
        "company_id": company["id"],
    }, headers=owner["headers"])
    assert first.status_code == 200, first.text
    assert first.json()["created"] == {"deliverables": 3, "tasks": 10}
    assert first.json()["skipped"] == {"deliverables": 0, "tasks": 0}
    assert await _counts(company["id"]) == (3, 10)

    async with AsyncSessionLocal() as s:
        artifacts = (await s.execute(
            select(Artifact).where(
                Artifact.company_id == company["id"]))).scalars().all()
        assert all(a.current_version == 1 for a in artifacts)
        assert all(a.status == ArtifactStatus.DRAFT for a in artifacts)
        assert all(a.created_by == owner["id"] for a in artifacts)
        assert {a.name for a in artifacts} == {
            t["name"] for t in _load()["deliverable_templates"]}
        versions = (await s.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id.in_([a.id for a in artifacts]))
        )).scalars().all()
        assert len(versions) == 3
        for version in versions:
            assert version.version == 1
            assert version.content.startswith("# ")
            assert "\n## " in version.content

        tasks = (await s.execute(
            select(Task).where(
                Task.company_id == company["id"]))).scalars().all()
        assert all(t.status == TaskStatus.TODO for t in tasks)
        assert all(t.created_by == owner["id"] for t in tasks)
        assert all(t.priority in TaskPriority for t in tasks)

    second = await client.post("/org-presets/saas/scaffold", json={
        "company_id": company["id"],
    }, headers=owner["headers"])
    assert second.status_code == 200, second.text
    assert second.json()["created"] == {"deliverables": 0, "tasks": 0}
    assert second.json()["skipped"] == {"deliverables": 3, "tasks": 10}
    assert await _counts(company["id"]) == (3, 10)


async def test_scaffold_cross_tenant_is_404(client, tenants):
    r = await client.post("/org-presets/saas/scaffold", json={
        "company_id": tenants.alice_co["id"],
    }, headers=tenants.bob["headers"])
    assert r.status_code == 404
    assert await _counts(tenants.alice_co["id"]) == (0, 0)
