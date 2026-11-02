"""Skill Center — list skills, install/uninstall on agents."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability, require_company_scope
from app.core.logging_config import get_logger
from app.core.scope import build_ctx, ensure_agent_access
from app.db.session import get_db
from app.skill_provisioning import (ensure_builtin_skills, fetch_market_skill,
                                    security_scan, search_market_skills)
from app.models.agent import Agent
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.skill import AgentSkill, SkillStatus, Skill as SkillRow
from app.models.user import User
from app.schemas.responses import (CustomSkillOut, SkillImportOut,
                                   SkillInstallOut, SkillMarketInstallOut,
                                   SkillMarketSearchOut, SkillOut,
                                   SkillPreviewOut, SkillScopeOut)

logger = get_logger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"],
                   dependencies=[Depends(require_company_scope())])


async def ensure_skills_seeded(db: AsyncSession):
    """Sync builtin skill manifests into DB.

    不能再用进程级 `_seeded` 短路：数据库可能被独立于本进程地重建
    （测试逐例清表、换库恢复），标志位一旦残留就会让后续请求
    对着空表报 "Skill not found"。幂等 upsert 的查询成本远低于
    "技能神秘消失"的排查成本。
    """
    await ensure_builtin_skills(db)
    await db.commit()


def _accessible_company_ids(user_id: str):
    """调用者可访问的公司：owner 事实源 ∪ active 成员行。"""
    owned = select(Company.id).join(
        User, User.id == Company.owner_id).where(User.id == user_id)
    member = select(CompanyMember.company_id).where(
        CompanyMember.user_id == user_id,
        CompanyMember.status == "active")
    return select(Company.id).where(
        or_(Company.id.in_(owned), Company.id.in_(member)))


@router.get("", response_model=list[SkillOut])
async def list_skills(company_id: str = Query(None),
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    await ensure_skills_seeded(db)
    # 内置技能（company_id 为空）全局可见；公司自定义技能只对该公司成员可见。
    # 此前不传 company_id 会把所有公司的自定义技能一起返回。
    cond = SkillRow.company_id.is_(None)
    if company_id:
        await build_ctx(db, user, company_id)  # 非成员 → 404
        cond = or_(cond, SkillRow.company_id == company_id)
    else:
        cids = (await db.execute(
            _accessible_company_ids(user.id))).scalars().all()
        if cids:
            cond = or_(cond, SkillRow.company_id.in_(cids))
    q = await db.execute(select(SkillRow).where(cond))
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


@router.post("/custom", status_code=201,
             response_model=CustomSkillOut)
async def create_custom_skill(data: CustomSkillIn,
                              company_id: str = Query(...),
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """创建自定义技能（如公司专属文档风格技能）。"""
    import uuid as _uuid
    from app.models.skill import Skill as SkillRow
    # 建技能是日常写操作（OPERATE）：成员可以，只读不行
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)
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


@router.post("/import", status_code=201,
             response_model=SkillImportOut)
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
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)
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


# ---------- Skill 市场（对接公开 skill 仓库，实现在 skill_provisioning） ----------

@router.get("/market/search", response_model=SkillMarketSearchOut)
async def market_search(q: str = "", limit: int = Query(20, le=50)):
    """搜索公开 skill 市场上的技能（GitHub 公开仓库目录）。"""
    results = await search_market_skills(q, limit=limit)
    return {"results": results, "total": len(results)}


@router.post("/market/fetch-preview", response_model=SkillPreviewOut)
async def market_fetch_preview(data: dict,
                               user=Depends(get_current_user)):
    """拉取市场 skill 的 SKILL.md 内容做导入前预览 + 安全审核。

    必须登录：这是一个"服务端替任意人出站抓取"的口子，匿名可调用等于
    把服务器当免费代理（也可被用来放大对 GitHub 的请求）。
    """
    raw_url = data.get("raw_url") or ""
    if not raw_url.startswith("https://raw.githubusercontent.com"):
        raise HTTPException(400, "仅支持 GitHub raw 地址")
    try:
        content = await fetch_market_skill(raw_url)
    except Exception as e:
        logger.warning("skill 市场预览拉取失败 %s：%s: %s", raw_url,
                       type(e).__name__, e)
        raise HTTPException(502, f"拉取失败: {e}")
    scan = security_scan(content)
    return {"content": content[:8000], "security_scan": scan}


@router.post("/market/install-from-market",
             response_model=SkillMarketInstallOut)
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
    try:
        content = await fetch_market_skill(raw_url)
    except Exception as e:
        logger.warning("skill 市场安装拉取失败 %s：%s: %s", raw_url,
                       type(e).__name__, e)
        raise HTTPException(502, f"拉取失败: {e}")
    scan = security_scan(content)
    if not scan["safe"] and not force:
        raise HTTPException(422, detail={
            "message": "安全审核未通过，检测到危险内容（可用 force=true 强制导入）",
            "security_scan": scan})
    await assert_company_capability(db, user.id, company_id,
                                    Capability.OPERATE)
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


@router.patch("/{skill_id}/scope", response_model=SkillScopeOut)
async def set_scope(skill_id: str, scope: str = Query(...),
                    company_id: str = Query(...),
                    db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """设置技能分类：public 公共 / exclusive 专属。

    必须带 company_id 并校验归属：此前只要登录就能改任意技能（含别家公司的），
    而内置技能的 scope 是全局字段——改一次影响所有公司。
    """
    from app.models.skill import Skill as SkillRow
    if scope not in ("public", "exclusive"):
        raise HTTPException(400, "scope must be public or exclusive")
    # 公共/私有属性影响全公司可见范围，按配置变更对待（MANAGE）
    await assert_company_capability(db, user.id, company_id, Capability.MANAGE)
    row = await db.get(SkillRow, skill_id)
    if not row:
        raise HTTPException(404)
    if row.company_id is None:
        # 内置技能（company_id 为空）是全局共享的，不能由某一家公司改公共/私有
        raise HTTPException(
            409, "系统内置技能不支持修改公共/私有属性")
    if row.company_id != company_id:
        # 别家公司的技能按「不存在」处理：409 会告诉调用方这个 id 真实存在，
        # 可被用来枚举他人的技能 id（与全系统 404 约定不一致）。
        raise HTTPException(404)
    row.scope = scope
    await db.commit()
    return {"ok": True, "slug": row.slug, "scope": row.scope}


class InstallIn(BaseModel):
    agent_id: str


def _verify_agent_owner(agent_row: Agent, company_map: dict):
    pass


async def _agent_owned(db: AsyncSession, user_id: str, agent_id: str) -> Agent:
    """给 Agent 装/卸技能属于写操作：走 Agent 写范围（不是"公司所有者"）。"""
    a = await db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Agent not found")
    await ensure_agent_access(db, user, a, Capability.OPERATE)
    return a


@router.post("/{skill_slug}/install", status_code=201,
             response_model=SkillInstallOut)
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


@router.post("/{skill_slug}/uninstall", status_code=200,
             response_model=SkillInstallOut)
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
