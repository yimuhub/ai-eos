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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent, AgentStatus
from app.models.company import Company, Department
from app.models.expert_template import ExpertTemplate
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
    p = os.path.join(_presets_dir(), f"{pid}.json")
    if not os.path.isfile(p):
        raise HTTPException(404, f"组织形态预设 {pid} 不存在")
    return json.load(open(p, encoding="utf-8"))


@router.get("")
async def list_presets(user=Depends(get_current_user)):
    """8 个行业预设：只给摘要，预览用 GET /org-presets/{pid}。"""
    d = _presets_dir()
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".json"):
            continue
        obj = json.load(open(os.path.join(d, f), encoding="utf-8"))
        posts = sum(len(dp.get("agents", [])) for dp in obj.get("departments", []))
        out.append({
            "id": obj.get("id", f[:-5]),
            "industry": obj.get("industry", f[:-5]),
            "emoji": obj.get("emoji", "🏢"),
            "department_count": len(obj.get("departments", [])),
            "agent_count": posts,
        })
    return out


@router.get("/{pid}")
async def preset_detail(pid: str, user=Depends(get_current_user)):
    """预设详情：完整部门树 + 每岗位推荐专家 slug（前端预览 + 勾选）。"""
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
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
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
            agent = Agent(
                company_id=data.company_id,
                department_id=d.id,
                name=aname,
                role=a.get("role"),
                description=tpl.description if tpl else None,
                system_prompt=tpl.system_prompt if tpl else None,
                expert_template_id=tpl.id if tpl else None,
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
