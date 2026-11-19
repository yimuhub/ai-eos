"""技能库路由（V6.2 专家技能引入方案）。

企业端（/skill-library）：
* GET  列表——只回 published 的平台条目 + 本公司自建（按 kind/industry/q 过滤）；
* POST /{id}/install——skill：落成 Skill(source=imported)+agent_skills
  （agent_runtime 会自动把方法论注入系统提示词）；expert：落 expert_templates；
  connector：记录安装企业并提示按 mcp.json 配置。

平台端（/skill-library/admin/*，超管三因子门禁）：
* POST /import——三通道批量导入（纯函数在 app/skill_library/）；
* GET  /queue——审核队列；POST /{id}/review——放行/拒绝。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, get_current_user,
                          get_db)
from app.api.routes.ops import _admin_guard
from app.core.permissions import Capability
from app.models.agent import Agent
from app.models.skill import AgentSkill, Skill
from app.models.skill_library import SkillLibraryItem
from app.models.user import User
from app.services.event_system import log_event
from app.skill_library import (bulk_import, strip_injection)

router = APIRouter(prefix="/skill-library", tags=["skill-library"])

_KINDS = ("skill", "connector", "expert")
_STATUSES = ("auto_pass", "review", "published", "rejected")
_MAX_INSTRUCTION = 4000


async def _owned_guard(db: AsyncSession, user: User, company_id: str) -> None:
    await assert_company_capability(db, user.id, company_id, Capability.VIEW)


def _item_out(row: SkillLibraryItem) -> dict:
    return {
        "id": row.id, "source": row.source, "source_id": row.source_id,
        "kind": row.kind, "name": row.name, "version": row.version,
        "description": (row.description or "")[:300],
        "trigger_keywords": row.trigger_keywords or [],
        "industry_tags": row.industry_tags or [],
        "license": row.license, "brand_refs": row.brand_refs or [],
        "review_status": row.review_status,
        "install_count": row.install_count,
        "installed": (row.installed_companies or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class ImportIn(BaseModel):
    items: list[dict]
    source: str = "workbuddy"


class ReviewIn(BaseModel):
    status: str
    note: str = ""


class InstallIn(BaseModel):
    company_id: str
    agent_ids: list[str] | None = None


# ─────────────────────────── 企业端 ───────────────────────────
@router.get("", response_model=list[dict])
async def list_library(kind: str | None = None, industry: str | None = None,
                       q: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """企业技能库：published 平台条目 + 本公司条目。"""
    stmt = select(SkillLibraryItem).where(
        or_(SkillLibraryItem.review_status == "published",
            SkillLibraryItem.company_id.isnot(None)))
    if kind:
        if kind not in _KINDS:
            raise HTTPException(400, f"kind 取值 {_KINDS}")
        stmt = stmt.where(SkillLibraryItem.kind == kind)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(SkillLibraryItem.name.ilike(like),
                              SkillLibraryItem.description.ilike(like)))
    rows = (await db.execute(stmt.order_by(SkillLibraryItem.install_count.desc())
                             .limit(200))).scalars().all()
    out = []
    for r in rows:
        d = _item_out(r)
        if industry and industry not in (r.industry_tags or []):
            continue
        out.append(d)
    return out


@router.post("/{item_id}/install", response_model=dict)
async def install_item(item_id: str, body: InstallIn,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """安装：skill→Skill 行 + agent_skills（全员或指定）；
    expert→expert_templates；connector→记录安装企业。"""
    await _owned_guard(db, user, body.company_id)
    row = await db.get(SkillLibraryItem, item_id)
    if row is None or row.review_status != "published":
        raise HTTPException(404, "技能不存在或未发布")
    # 已装过的公司跳过重复计数
    installed = row.installed_companies or []
    first_install = body.company_id not in installed

    if row.kind == "skill":
        payload = row.payload or {}
        body_text = str(payload.get("body") or "")
        instruction = strip_injection(body_text)[:_MAX_INSTRUCTION]
        # skills.slug 是全表唯一：必须带公司前缀，否则第二个公司装同一条
        # 平台技能就会撞 UNIQUE（冒烟实测抓出）。
        slug = f"lib-{body.company_id[:8]}-{row.source_id[:60]}"
        existing = (await db.execute(select(Skill).where(
            Skill.slug == slug, Skill.company_id == body.company_id))).scalar_one_or_none()
        if existing is None:
            existing = Skill(slug=slug, name=row.name[:200],
                             description=(row.description or "")[:500],
                             version=row.version or "1.0.0",
                             input_schema={"instruction": instruction,
                                           "trigger_keywords": row.trigger_keywords or []},
                             permissions=[], source="imported",
                             scope="exclusive", company_id=body.company_id)
            db.add(existing)
            await db.flush()
        skill_id = existing.id
        # 授权对象：指定员工或全员
        if body.agent_ids:
            agent_rows = (await db.execute(select(Agent).where(
                Agent.id.in_(body.agent_ids),
                Agent.company_id == body.company_id))).scalars().all()
        else:
            agent_rows = (await db.execute(select(Agent).where(
                Agent.company_id == body.company_id))).scalars().all()
        granted = 0
        for a in agent_rows:
            dup = (await db.execute(select(AgentSkill).where(
                AgentSkill.agent_id == a.id,
                AgentSkill.skill_id == skill_id))).scalar_one_or_none()
            if dup is None:
                db.add(AgentSkill(agent_id=a.id, skill_id=skill_id,
                                  source="manual"))
                granted += 1
        detail = {"skill_id": skill_id, "granted_agents": granted}
    elif row.kind == "expert":
        from app.models.expert_template import ExpertTemplate
        source_slug = f"workbuddy:{row.source_id}"
        existing = (await db.execute(select(ExpertTemplate).where(
            ExpertTemplate.source_slug == source_slug))).scalar_one_or_none()
        if existing is not None:
            detail = {"expert_template_id": existing.id, "created": False}
        else:
            agents = (row.payload or {}).get("agents") or []
            persona = ""
            if agents:
                persona = str(agents[0].get("md") or "")[:6000]
            tpl = ExpertTemplate(
                company_id=body.company_id, source="workbuddy",
                source_slug=source_slug,
                name=row.name[:200], description=(row.description or "")[:2000],
                system_prompt=persona,
                tags=row.industry_tags or [],
                skills=(row.payload or {}).get("skills") or [],
                category="workbuddy")
            db.add(tpl)
            await db.flush()
            detail = {"expert_template_id": tpl.id, "created": True}
    elif row.kind == "connector":
        if first_install:
            installed.append(body.company_id)
            row.installed_companies = installed
        detail = {"hint": "连接器蓝图已记录：请到「连接器」页按 mcp.json 完成配置",
                  "mcp_servers": list((row.payload or {}).get("mcp", {})
                                      .get("mcpServers", {}))[:10]
                  if isinstance((row.payload or {}).get("mcp"), dict) else []}
    else:
        raise HTTPException(400, "未知条目类型")

    if first_install:
        row.install_count = (row.install_count or 0) + 1
    await log_event(db, "skill_library.installed", "agent", row.id,
                    "company", body.company_id,
                    {"kind": row.kind, "name": row.name}, company_id=body.company_id)
    await db.commit()
    return {"ok": True, "kind": row.kind, **detail}


@router.get("/{item_id}/stats", response_model=dict)
async def item_stats(item_id: str, company_id: str,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """技能使用统计（V6.2.2 度量闭环）：
    安装企业数 / 本公司授权员工数 / 这些员工近 7 天任务量与完成量。"""
    await _owned_guard(db, user, company_id)
    row = await db.get(SkillLibraryItem, item_id)
    if row is None:
        raise HTTPException(404, "条目不存在")
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    agent_ids: list[str] = []
    if row.kind == "skill":
        skill = (await db.execute(select(Skill).where(
            Skill.slug == f"lib-{row.source_id[:80]}",
            Skill.company_id == company_id))).scalar_one_or_none()
        if skill:
            grants = (await db.execute(select(AgentSkill.agent_id).where(
                AgentSkill.skill_id == skill.id))).scalars().all()
            agent_ids = list(grants)
    stats = {"install_count": row.install_count or 0,
             "granted_agents": len(agent_ids)}
    if agent_ids:
        from app.models.task import Task
        rows = (await db.execute(select(Task.status, Task.id).where(
            Task.agent_id.in_(agent_ids),
            Task.created_at >= since))).all()
        stats["tasks_7d"] = len(rows)
        stats["tasks_done_7d"] = sum(1 for st, _ in rows
                                     if str(st).upper() in ("DONE", "COMPLETED", "DELIVERED"))
    else:
        stats["tasks_7d"] = 0
        stats["tasks_done_7d"] = 0
    return {"id": row.id, "name": row.name, "kind": row.kind, **stats}


# ─────────────────────────── 开放投递（V6.2.3） ───────────────────────────
class SubmitIn(BaseModel):
    kind: str
    source_id: str
    name: str = ""
    content: str = ""
    files: dict[str, str] | None = None
    license: str | None = None
    contact: str = ""


@router.post("/submit", response_model=dict)
async def submit_item(body: SubmitIn,
                      request: Request,
                      db: AsyncSession = Depends(get_db)):
    """开放投递（V6.2.3）：第三方按 SKILL.md 标准投递技能/连接器/专家包。
    鉴权 = X-Submit-Token（env AIEOS_SKILL_SUBMIT_TOKEN，未配置则 503 关闭通道）。
    一律进 review 队列，不做自动放行。"""
    import hmac as _hmac
    import os as _os
    expected = (_os.environ.get("AIEOS_SKILL_SUBMIT_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(503, "投递通道未开启（未配置 AIEOS_SKILL_SUBMIT_TOKEN）")
    provided = (request.headers.get("X-Submit-Token") or "").strip()
    if not provided or not _hmac.compare_digest(provided, expected):
        raise HTTPException(403, "投递令牌无效")
    if body.kind not in _KINDS:
        raise HTTPException(400, f"kind 取值 {_KINDS}")
    recs = bulk_import([{"kind": body.kind,
                          "source_id": body.source_id,
                          "content": body.content,
                          "files": body.files or {}}],
                       source="community")
    if not recs:
        raise HTTPException(400, "解析失败：内容为空")
    rec = recs[0]
    dup = (await db.execute(select(SkillLibraryItem).where(
        SkillLibraryItem.source == rec["source"],
        SkillLibraryItem.source_id == rec["source_id"]))).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"source_id 已存在（{dup.review_status}）")
    flags = rec.pop("flags", {})
    # 投递通道一律人工审：即便无品牌词也不 auto_pass
    row = SkillLibraryItem(
        id=rec["id"], source=rec["source"], source_id=rec["source_id"],
        kind=rec["kind"], name=(body.name or rec["name"])[:200],
        version=rec.get("version"), description=rec.get("description"),
        payload=rec.get("payload"),
        trigger_keywords=rec.get("trigger_keywords") or [],
        industry_tags=rec.get("industry_tags") or [],
        license=(body.license or rec.get("license")),
        brand_refs=rec.get("brand_refs") or [],
        review_status="review",
        reviewer=None, company_id=None)
    db.add(row)
    await log_event(db, "skill_library.submitted", "external",
                    body.contact[:80] or "anonymous", "skill_library", row.id,
                    {"kind": row.kind, "flags": flags}, company_id=None)
    await db.commit()
    return {"ok": True, "id": row.id, "review_status": row.review_status,
            "message": "已进入审核队列"}


# ─────────────────────────── 平台端（超管） ───────────────────────────
@router.post("/admin/import", response_model=dict)
async def admin_import(body: ImportIn, request: Request,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """三通道批量导入。超管门禁；纯函数解析在 app/skill_library/。"""
    await _admin_guard(db, user, request)
    if not user.is_superadmin:
        raise HTTPException(403, "需要平台超管身份")
    results = bulk_import(body.items[:500], body.source[:30])
    saved = 0
    for rec in results:
        dup = (await db.execute(select(SkillLibraryItem).where(
            SkillLibraryItem.source == rec["source"],
            SkillLibraryItem.source_id == rec["source_id"]))).scalar_one_or_none()
        if dup is not None:
            continue
        flags = rec.pop("flags", {})
        row = SkillLibraryItem(
            id=rec["id"], source=rec["source"], source_id=rec["source_id"],
            kind=rec["kind"], name=rec["name"][:200],
            version=rec.get("version"),
            description=rec.get("description"), payload=rec.get("payload"),
            trigger_keywords=rec.get("trigger_keywords") or [],
            industry_tags=rec.get("industry_tags") or [],
            license=rec.get("license"), brand_refs=rec.get("brand_refs") or [],
            review_status=rec.get("review_status", "review"),
            company_id=None)
        db.add(row)
        saved += 1
        _ = flags
    await db.commit()
    return {"ok": True, "imported": saved, "parsed": len(results),
            "skipped_dup": len(results) - saved}


@router.get("/admin/queue", response_model=list[dict])
async def admin_queue(request: Request, status: str = "review",
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _admin_guard(db, user, request)
    if status not in _STATUSES:
        raise HTTPException(400, f"status 取值 {_STATUSES}")
    rows = (await db.execute(select(SkillLibraryItem).where(
        SkillLibraryItem.review_status == status)
        .order_by(SkillLibraryItem.created_at.desc()).limit(200))).scalars().all()
    return [_item_out(r) for r in rows]


@router.post("/admin/{item_id}/review", response_model=dict)
async def admin_review(item_id: str, body: ReviewIn, request: Request,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _admin_guard(db, user, request)
    if body.status not in ("published", "rejected", "review"):
        raise HTTPException(400, "status 只能是 published/rejected/review")
    row = await db.get(SkillLibraryItem, item_id)
    if row is None:
        raise HTTPException(404, "条目不存在")
    row.review_status = body.status
    row.reviewer = user.username or user.email or "admin"
    await log_event(db, "skill_library.review", "user", str(user.id),
                    "skill_library", row.id,
                    {"status": body.status, "note": body.note[:200]},
                    company_id=None)
    await db.commit()
    return {"ok": True, "id": row.id, "status": row.review_status}
