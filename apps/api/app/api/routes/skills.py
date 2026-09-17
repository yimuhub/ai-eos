"""Skill Center — list skills, install/uninstall on agents."""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.skill_system import BUILTIN_SKILLS
from app.models.agent import Agent
from app.models.company import Company
from app.models.skill import AgentSkill, SkillStatus, Skill as SkillRow

router = APIRouter(prefix="/skills", tags=["skills"])

_seeded = False


async def ensure_skills_seeded(db: AsyncSession):
    """Sync builtin skill manifests into DB once."""
    global _seeded
    if _seeded:
        return
    for cls in BUILTIN_SKILLS:
        inst = cls()
        exists = await db.execute(select(SkillRow).where(SkillRow.slug == inst.id))
        if not exists.scalar_one_or_none():
            db.add(SkillRow(slug=inst.id, name=inst.name, description=inst.description,
                            version=inst.version, input_schema=inst.input_schema,
                            output_schema=inst.output_schema, permissions=inst.permissions))
    await db.commit()
    _seeded = True


@router.get("")
async def list_skills(company_id: str = Query(None),
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    await ensure_skills_seeded(db)
    q = await db.execute(select(SkillRow).distinct(SkillRow.id).where(
        (SkillRow.company_id.is_(None)) |
        (SkillRow.company_id == company_id) if company_id else True))
    skills = []
    for s in q.scalars():
        d = {c.key: getattr(s, c.key) for c in s.__table__.columns}
        d["source_label"] = {"builtin": "系统内置", "custom": "自主沉淀",
                             "imported": "外部导入"}.get(d.get("source"), d.get("source"))
        skills.append(d)
    return skills


class CustomSkillIn(BaseModel):
    name: str
    description: str | None = None
    scope: str = "exclusive"       # exclusive=私有 | public=公共
    instruction: str | None = None  # 技能方法论/执行步骤（长期总结）


@router.post("/custom", status_code=201)
async def create_custom_skill(data: CustomSkillIn,
                              company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """创建自定义技能（如公司专属文档风格技能）。"""
    import uuid as _uuid
    from app.models.skill import Skill as SkillRow
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "company not found")
    slug = f"custom_{_uuid.uuid4().hex[:10]}"
    row = SkillRow(slug=slug, name=data.name, description=data.description,
                   version="1.0.0", status=SkillStatus.ENABLED,
                   scope=data.scope, company_id=company_id, source="custom",
                   input_schema={"instruction": data.instruction or ""})
    db.add(row)
    await db.commit()
    return {"id": row.id, "slug": slug, "name": data.name, "scope": data.scope}


class ImportIn(BaseModel):
    name: str
    description: str | None = None
    instruction: str | None = None
    version: str = "1.0.0"
    scope: str = "exclusive"          # exclusive=私有专属 public=公共共享
    attachment_ids: list[str] = []    # 关联附件资产id
    force: bool = False               # 安全审核未通过时强制导入


DANGEROUS_PATTERNS = [
    "rm -rf", "curl *| bash", "curl *| sh", "wget *| sh", "eval(",
    "exec(", "os.system", "subprocess", "__import__", "base64 -d",
    "chmod 777", "/etc/passwd", ".ssh", "crypto", "bitcoin", "keylogger",
]


def security_scan(text: str) -> dict:
    """skill 内容安全审核：命中危险模式则标记。"""
    low = (text or "").lower()
    hits = [pat.replace("*", "") for pat in DANGEROUS_PATTERNS
            if ("*" in pat and pat.replace(" *", " ") in low) or ("*" not in pat and pat in low)]
    return {"safe": len(hits) == 0, "hits": hits,
            "level": "pass" if not hits else ("high" if len(hits) >= 3 else "warn")}


@router.post("/import", status_code=201)
async def import_skill(data: ImportIn, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """导入外部 skill 文件（JSON 定义）。含安全审核，危险内容需 force。"""
    import uuid as _uuid
    from app.models.skill import Skill as SkillRow
    scan = security_scan((data.instruction or "") + " " + (data.description or ""))
    if not scan["safe"] and not data.force:
        raise HTTPException(422, detail={
            "message": "安全审核未通过，检测到危险内容（可用 force=true 强制导入）",
            "security_scan": scan})
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "company not found")
    slug = f"imp_{_uuid.uuid4().hex[:10]}"
    row = SkillRow(slug=slug, name=data.name, description=data.description,
                   version=data.version, status=SkillStatus.ENABLED,
                   scope=data.scope if data.scope in ("exclusive", "public") else "exclusive",
                   company_id=company_id, source="imported",
                   input_schema={"instruction": data.instruction or "",
                                 "attachment_ids": data.attachment_ids,
                                 "security_scan": scan})
    db.add(row)
    await db.commit()
    return {"id": row.id, "slug": slug, "name": data.name,
            "scope": row.scope, "security_scan": scan}


# ---------- Skill 市场（对接公开 skill 仓库） ----------
MARKET_REPOS = [
    {"name": "anthropics/skills", "desc": "Anthropic 官方 Agent Skills",
     "api": "https://api.github.com/repos/anthropics/skills/contents/skills",
     "raw": "https://raw.githubusercontent.com/anthropics/skills/main/{path}"},
    {"name": "awesome-claude-code", "desc": "社区精选 Claude 技能合集",
     "api": "https://api.github.com/repos/hesreallyhim/awesome-claude-code/contents/skills",
     "raw": "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/skills/{path}"},
]


@router.get("/market/search")
async def market_search(q: str = "", limit: int = Query(20)):
    """搜索公开 skill 市场上的技能（GitHub 公开仓库目录）。"""
    import urllib.request as _u
    results = []
    for repo in MARKET_REPOS:
        try:
            req = _u.Request(repo["api"], headers={"User-Agent": "ai-eos", "Accept": "application/vnd.github+json"})
            with _u.urlopen(req, timeout=10) as resp:
                items = json.loads(resp.read())
            for it in items if isinstance(items, list) else []:
                if it.get("type") != "dir":
                    continue
                name = it.get("name", "")
                if q and q.lower() not in name.lower():
                    continue
                results.append({
                    "name": name, "repo": repo["name"], "path": it.get("path"),
                    "url": it.get("html_url"), "source_market": repo["desc"],
                    "raw_url": repo["raw"].format(path=f"{it.get('path')}/SKILL.md"),
                })
                if len(results) >= limit:
                    break
        except Exception:
            continue
    return {"results": results, "total": len(results)}


@router.post("/market/fetch-preview")
async def market_fetch_preview(data: dict):
    """拉取市场 skill 的 SKILL.md 内容做导入前预览 + 安全审核。"""
    raw_url = data.get("raw_url") or ""
    if not raw_url.startswith("https://raw.githubusercontent.com"):
        raise HTTPException(400, "仅支持 GitHub raw 地址")
    import urllib.request as _u
    try:
        req = _u.Request(raw_url, headers={"User-Agent": "ai-eos"})
        with _u.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(502, f"拉取失败: {e}")
    scan = security_scan(content)
    return {"content": content[:8000], "security_scan": scan}


@router.post("/market/install-from-market")
async def install_from_market(data: dict, company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """从市场导入 skill：拉取内容 → 安全审核 → 入库。高危内容拒绝入库。"""
    body_name = data.get("name") or "未命名技能"
    raw_url = data.get("raw_url") or ""
    scope = data.get("scope", "exclusive")
    force = bool(data.get("force"))
    if not raw_url.startswith("https://raw.githubusercontent.com"):
        raise HTTPException(400, "仅支持 GitHub raw 地址")
    import urllib.request as _u
    try:
        req = _u.Request(raw_url, headers={"User-Agent": "ai-eos"})
        with _u.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(502, f"拉取失败: {e}")
    scan = security_scan(content)
    if not scan["safe"] and not force:
        raise HTTPException(422, detail={
            "message": "安全审核未通过，检测到危险内容（可用 force=true 强制导入）",
            "security_scan": scan})
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "company not found")
    import uuid as _uuid
    from app.models.skill import Skill as SkillRow
    slug = f"mkt_{_uuid.uuid4().hex[:10]}"
    row = SkillRow(slug=slug, name=body_name,
                   description=(data.get("description") or content[:120]),
                   version="1.0.0", status=SkillStatus.ENABLED,
                   scope=scope if scope in ("exclusive", "public") else "exclusive",
                   company_id=company_id, source="imported",
                   input_schema={"instruction": content[:6000],
                                 "market_url": raw_url,
                                 "security_scan": scan})
    db.add(row)
    await db.commit()
    return {"id": row.id, "slug": slug, "name": body_name, "security_scan": scan}


@router.patch("/{skill_id}/scope")
async def set_scope(skill_id: str, scope: str = Query(...),
                    db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """设置技能分类：public 公共 / exclusive 专属。"""
    from app.models.skill import Skill as SkillRow
    if scope not in ("public", "exclusive"):
        raise HTTPException(400, "scope must be public or exclusive")
    row = await db.get(SkillRow, skill_id)
    if not row:
        raise HTTPException(404)
    row.scope = scope
    await db.commit()
    return {"ok": True, "slug": row.slug, "scope": row.scope}


class InstallIn(BaseModel):
    agent_id: str


def _verify_agent_owner(agent_row: Agent, company_map: dict):
    pass


async def _agent_owned(db: AsyncSession, user_id: str, agent_id: str) -> Agent:
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    c = await db.get(Company, a.company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Agent not found")
    return a


@router.post("/{skill_slug}/install", status_code=201)
async def install_skill(skill_slug: str, data: InstallIn,
                        db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    await ensure_skills_seeded(db)
    await _agent_owned(db, user.id, data.agent_id)
    sq = await db.execute(select(SkillRow).where(SkillRow.slug == skill_slug))
    skill = sq.scalar_one_or_none()
    if not skill:
        raise HTTPException(404, "Skill not found")
    exists = await db.execute(
        select(AgentSkill).where(AgentSkill.agent_id == data.agent_id,
                                 AgentSkill.skill_id == skill.id))
    if exists.scalar_one_or_none():
        return {"detail": "already installed"}
    db.add(AgentSkill(agent_id=data.agent_id, skill_id=skill.id))
    await db.commit()
    return {"detail": "installed"}


@router.post("/{skill_slug}/uninstall", status_code=200)
async def uninstall_skill(skill_slug: str, data: InstallIn,
                          db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    await _agent_owned(db, user.id, data.agent_id)
    sq = await db.execute(select(SkillRow).where(SkillRow.slug == skill_slug))
    skill = sq.scalar_one_or_none()
    if not skill:
        raise HTTPException(404, "Skill not found")
    eq = await db.execute(
        select(AgentSkill).where(AgentSkill.agent_id == data.agent_id,
                                 AgentSkill.skill_id == skill.id))
    row = eq.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"detail": "uninstalled"}
