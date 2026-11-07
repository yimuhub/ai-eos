"""V5.0 行业组织形态预设：一键引入部门树 + 配备专家的智能体。

/org-presets:
  GET  /org-presets              预设列表（8 个行业，含部门/岗位/推荐专家数）
  GET  /org-presets/{pid}       预设详情（可预览）
  POST /org-presets/{pid}/apply 一键引入：建部门树 + 批量建数字员工（绑定专家库专家）

设计：
- 预设文件在 content/org_presets/*.json（纯数据，可随时加行业）
- apply 幂等友好：部门按名去重、员工按 (部门+姓名) 去重，
  已存在的跳过并计入 skipped，不破坏已有组织
- 岗位 → 专家绑定：按 expert_slug 查 enabled 的 expert_templates，
  取 system_prompt 作人设，写 agents.expert_template_id
"""
import json
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.agent import Agent, AgentStatus
from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.company import Department
from app.models.expert_template import ExpertTemplate
from app.models.task import Task, TaskPriority, TaskStatus
from app.services.event_system import log_event

router = APIRouter(prefix="/org-presets", tags=["org-presets"])


def _presets_dir() -> str:
    """找 content/org_presets，逐级向上探测（与 experts.py 同风格）。"""
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        cand = os.path.join(cur, "content", "org_presets")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "..", "..", "content", "org_presets")


def _load_preset(pid: str) -> dict:
    # 公开端点：pid 直接拼进文件路径，必须限制字符集，
    # 否则 ../ 之类的输入会把读文件能力暴露到 content 目录之外。
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
        raise HTTPException(404, f"组织形态预设 {pid} 不存在")
    p = os.path.join(_presets_dir(), f"{pid}.json")
    if not os.path.isfile(p):
        raise HTTPException(404, f"组织形态预设 {pid} 不存在")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


@router.get("")
async def list_presets():
    """行业预设列表：只给摘要，预览用 GET /org-presets/{pid}。

    **免登录**：预设是随包发布的静态 JSON（content/org_presets），
    不含任何公司数据。安装向导要在还没有账号的第 3 步实时预览组织树，
    所以这两个读接口不挂鉴权；写入路径 `/apply` 仍然要 MANAGE。
    """
    d = _presets_dir()
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".json"):
            continue
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            obj = json.load(fh)
        posts = sum(len(dp.get("agents", [])) for dp in obj.get("departments", []))
        out.append({
            "id": obj.get("id", f[:-5]),
            "industry": obj.get("industry", f[:-5]),
            "emoji": obj.get("emoji", "🏢"),
            "kind": obj.get("kind", "industry_preset"),
            "department_count": len(obj.get("departments", [])),
            "agent_count": posts,
            "deliverable_template_count": len(
                obj.get("deliverable_templates") or []),
            "task_template_count": len(obj.get("task_templates") or []),
        })
    return out


@router.get("/{pid}")
async def preset_detail(pid: str):
    """预设详情：完整部门树 + 每岗位推荐专家 slug（前端预览 + 勾选）。

    与列表一样免登录，用途是安装向导的组织预览。
    """
    return _load_preset(pid)


class ApplyIn(BaseModel):
    company_id: str
    # 可选勾选引入：只建这些部门（null=全部）
    departments: list[str] | None = None
    model_id: str | None = None  # 统一给新员工的执行模型
    dry_run: bool = False        # True=只预览将建什么，不落库


@router.post("/{pid}/apply")
async def apply_preset(pid: str, data: ApplyIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """一键引入组织形态：部门树 + 配备专家的智能体（幂等，可 dry_run 预览）。"""
    # 一键建部门树 + 批量建员工属于组织结构变更：按 MANAGE 收口
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    preset = _load_preset(pid)

    only = set(data.departments) if data.departments else None
    depts = [dp for dp in preset.get("departments", [])
             if only is None or dp.get("name") in only]

    # 预取专家（按 slug，enabled）。
    # DB 的 source_slug 带部门前缀（如 specialized-customer-success-manager），
    # 预设里写的是基 slug（customer-success-manager）——内存精确匹配：
    # 完全相等 或 以「-基slug」结尾。专家库规模有限，全量拉取无性能压力，
    # 且彻底避开 LIKE 的 %/_ 通配符转义坑。
    slugs = sorted({a.get("expert_slug")
                    for dp in depts for a in dp.get("agents", [])
                    if a.get("expert_slug")})
    slug_map: dict[str, ExpertTemplate] = {}
    if slugs:
        all_experts = (await db.execute(
            select(ExpertTemplate).where(
                ExpertTemplate.company_id == data.company_id,
                ExpertTemplate.enabled.is_(True)))).scalars().all()
        for s in slugs:
            for r in all_experts:
                if r.source_slug == s or r.source_slug.endswith("-" + s):
                    slug_map[s] = r
                    break

    # 部门按名去重（已存在则复用）
    existing = (await db.execute(
        select(Department).where(Department.company_id == data.company_id)
    )).scalars().all()
    dept_by_name = {d.name: d for d in existing}
    # 已存在的「部门id→已有员工名」，仅老部门需要去重（新部门必然无同名员工）
    names_in = {str(d.id): set() for d in existing}
    for a in (await db.execute(
        select(Agent).where(Agent.company_id == data.company_id)
    )).scalars().all():
        if str(a.department_id) in names_in:
            names_in[str(a.department_id)].add(a.name)

    created_d = created_a = skipped_d = skipped_a = 0
    plan = []
    new_agents = []
    hired = []  # 需计数雇佣的专家

    # 阶段 1：部门树。id 是 Python 侧 uuid default（flush 后才生成），
    # 新部门必须 db.add + flush 才能拿到真实 id 给员工挂。
    for dp in depts:
        dname = (dp.get("name") or "").strip()
        if not dname:
            continue
        if dname in dept_by_name:
            skipped_d += 1
        else:
            d = Department(company_id=data.company_id, name=dname,
                           description=dp.get("desc"))
            db.add(d)
            dept_by_name[dname] = d
            created_d += 1
    if not data.dry_run and created_d:
        await db.flush()  # 让新部门拿到真实 id

    # 阶段 1.5：套餐席位预检。
    # 员工此刻还没 db.add，COUNT 只包含既有在编员工；这里用与下方完全相同的
    # 去重口径预演一遍将新建的数量。不预检的话，「一键建公司」就是套餐席位
    # 的合法绕过口——批量导入 56 个岗位和逐个点 56 次没有区别。
    if not data.dry_run:
        seen = {k: set(v) for k, v in names_in.items()}
        planned = 0
        for dp in depts:
            dname = (dp.get("name") or "").strip()
            d = dept_by_name.get(dname) if dname else None
            if not d:
                continue
            known = seen.setdefault(str(d.id), set()) if d.id else set()
            for a in dp.get("agents", []):
                aname = (a.get("name") or "").strip()
                if not aname:
                    continue
                if d.id and aname in known:
                    continue
                known.add(aname)
                planned += 1
        if planned:
            from app.services import subscription_service
            await subscription_service.enforce_seat_limit(
                db, data.company_id, adding=planned, label="导入组织预设")

    # 阶段 2：岗位 → 配备专家的智能体（部门 id 此刻已就位）
    for dp in depts:
        dname = (dp.get("name") or "").strip()
        if not dname:
            continue
        d = dept_by_name.get(dname)
        if not d:
            continue
        for a in dp.get("agents", []):
            aname = (a.get("name") or "").strip()
            if not aname:
                continue
            tpl = slug_map.get(a.get("expert_slug") or "")
            if d.id and aname in names_in.get(str(d.id), set()):
                skipped_a += 1
                plan.append({"type": "agent", "name": aname, "dept": dname,
                             "status": "skipped"})
                continue
            # V5.6 大厂仿：预设岗位自带 skills/职责/人设时，
            # 无专家匹配也把"说明+技能包"落到员工身上（不裸奔）
            skills = a.get("skills") or []
            desc = a.get("duties") or (tpl.description if tpl else None)
            sp = (a.get("personality") or a.get("system_prompt")
                  or (tpl.system_prompt if tpl else None))
            memory_cfg: dict | None = None
            if skills:
                memory_cfg = {"skills": skills}
                if a.get("expert_slug"):
                    memory_cfg["expert_slug_hint"] = a["expert_slug"]
            agent = Agent(
                company_id=data.company_id,
                department_id=d.id,
                name=aname,
                role=a.get("role"),
                description=desc,
                system_prompt=sp,
                expert_template_id=tpl.id if tpl else None,
                memory_config=memory_cfg,
                status=AgentStatus.IDLE,
            )
            db.add(agent)
            if d.id:
                names_in.setdefault(str(d.id), set()).add(aname)
            created_a += 1
            new_agents.append(agent)
            if tpl:
                hired.append(tpl.id)
            plan.append({"type": "agent", "name": aname, "dept": dname,
                        "expert": tpl.source_slug if tpl else None,
                        "skills": skills or None,
                        "status": "created"})

    if not data.dry_run:
        await db.flush()
        if data.model_id:
            from app.models.agent import AgentModelConfig as AMC
            for agent in new_agents:
                db.add(AMC(agent_id=agent.id, role="primary", model_id=data.model_id))
        # 雇佣计数（按专家去重 +1）
        for eid in dict.fromkeys(hired):
            tpl = await db.get(ExpertTemplate, eid)
            if tpl:
                tpl.hired_count = (tpl.hired_count or 0) + 1
        await log_event(db, "org_preset.applied", "organization", None,
                        "user", user.id,
                        {"preset": pid, "departments": created_d,
                         "agents": created_a})
        await db.commit()
    return {
        "preset": pid, "industry": preset.get("industry"),
        "created_departments": created_d, "created_agents": created_a,
        "skipped_departments": skipped_d, "skipped_agents": skipped_a,
        "dry_run": data.dry_run, "plan": plan,
        "note": "预览模式（dry_run）" if data.dry_run
        else "组织形态已落地：部门 + 配备专家的智能体，可直接开干",
    }


class ScaffoldIn(BaseModel):
    company_id: str
    install_deliverables: bool = True
    install_tasks: bool = True
    dry_run: bool = False


@router.post("/{pid}/scaffold")
async def scaffold_preset(pid: str, data: ScaffoldIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """按行业包铺设交付物模板与任务模板（幂等，可 dry_run 预览）。

    交付物：每条模板建一个 DRAFT 成果 + v1 内容（标题 + 大纲章节占位）；
    任务：每条模板建一个 TODO 任务。同名成果 / 同标题未完成任务一律跳过，
    重复调用不会产生重复数据。
    """
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
    preset = _load_preset(pid)
    deliverable_templates = preset.get("deliverable_templates") or []
    task_templates = preset.get("task_templates") or []

    created = {"deliverables": 0, "tasks": 0}
    skipped = {"deliverables": 0, "tasks": 0}

    if data.install_deliverables and deliverable_templates:
        existing_names = set((await db.execute(
            select(Artifact.name).where(
                Artifact.company_id == data.company_id))).scalars().all())
        for tpl in deliverable_templates:
            name = (tpl.get("name") or "").strip()
            if not name:
                continue
            if name in existing_names:
                skipped["deliverables"] += 1
                continue
            # 同一批模板内重名也按"已存在"处理，避免 dry_run 与落库计数不一致
            existing_names.add(name)
            created["deliverables"] += 1
            if data.dry_run:
                continue
            outline = [str(s).strip() for s in (tpl.get("outline") or [])
                       if str(s).strip()]
            content = f"# {name}\n\n" + "".join(
                f"## {section}\n\n" for section in outline)
            art = Artifact(
                id=str(uuid.uuid4()),
                company_id=data.company_id,
                name=name,
                artifact_type=(tpl.get("artifact_type") or "document"),
                current_version=1,
                status=ArtifactStatus.DRAFT,
                created_by=user.id,
            )
            db.add(art)
            db.add(ArtifactVersion(
                id=str(uuid.uuid4()), artifact_id=art.id, version=1,
                content=content, change_note="行业包模板",
                status=ArtifactStatus.DRAFT, submitted_by=user.id))

    if data.install_tasks and task_templates:
        # "未完成"= 除 COMPLETED 外的任何状态；已完成任务不阻止再次铺设
        existing_titles = set((await db.execute(
            select(Task.title).where(
                Task.company_id == data.company_id,
                Task.status != TaskStatus.COMPLETED))).scalars().all())
        for tpl in task_templates:
            title = (tpl.get("title") or "").strip()
            if not title:
                continue
            if title in existing_titles:
                skipped["tasks"] += 1
                continue
            existing_titles.add(title)
            created["tasks"] += 1
            if data.dry_run:
                continue
            raw_priority = str(tpl.get("priority") or "NORMAL").upper()
            try:
                priority = TaskPriority(raw_priority)
            except ValueError:
                priority = TaskPriority.NORMAL
            db.add(Task(
                company_id=data.company_id,
                title=title,
                description=tpl.get("description"),
                status=TaskStatus.TODO,
                priority=priority,
                created_by=user.id,
            ))

    if not data.dry_run and (created["deliverables"] or created["tasks"]):
        await db.commit()

    return {
        "preset": pid,
        "company_id": data.company_id,
        "created": created,
        "skipped": skipped,
        "dry_run": data.dry_run,
    }

