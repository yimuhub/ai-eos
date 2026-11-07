"""Expert Library API (V3.0 专家库融合).

/experts:
  POST   /experts/import            全量导入 vendor 专家库（幂等）
  GET    /experts                   列表（department / q 筛选）
  GET    /experts/{id}              详情（含完整 raw_md）
  POST   /experts/{id}/to-blueprint 专家 → 工厂蓝图（走既有确认链）
  POST   /experts/search            P1 自然语言找专家（LLM 检索 Top3）
  POST   /experts/team              P2 一句话组建专家团队（批量蓝图）
"""
import asyncio
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.permissions import Capability, require_company_scope
from app.core.logging_config import get_logger
from app.expert_library import SOURCE_URL, DEPT_NAMES, parse_expert_md
from app.models.expert_template import ExpertTemplate
from app.models.user import User
from app.schemas.responses_gate import (ExpertBlueprintOut, ExpertDetailOut,
                                       ExpertDepartmentOut, ExpertImportOut,
                                       ExpertOriginalImportOut, ExpertPickOut,
                                       ExpertPublicOut, ExpertSearchMissOut,
                                       ExpertTeamOut)

logger = get_logger(__name__)

router = APIRouter(prefix="/experts", tags=["experts"],
                   dependencies=[Depends(require_company_scope())])
VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "..", "..", "..", "..", "vendor", "agency-agents-zh")
SOURCE_DIR_PREFIXES = list(DEPT_NAMES.keys()) + ["examples"]


def _vendor_path() -> str:
    """从本文件向上逐级找 vendor/agency-agents-zh（项目根目录）。
    便携包/开发环境布局不同，逐级探测比写死层级稳。"""
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        cand = os.path.join(cur, "vendor", "agency-agents-zh")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 兜底：项目根目录
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "..", "..", "vendor", "agency-agents-zh")


def _detect_dept(fname: str) -> str:
    for p in sorted(SOURCE_DIR_PREFIXES, key=len, reverse=True):
        if fname.startswith(p + "-"):
            return p
    return "specialized"


@router.post("/import", status_code=201, response_model=ExpertImportOut)
async def import_experts(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """从 vendor 目录全量导入专家模板（按 source_slug 幂等更新）。"""
    vdir = _vendor_path()
    if not os.path.isdir(vdir):
        raise HTTPException(404, "vendor/agency-agents-zh 不存在（需先 git clone 或解压）")
    files = [f for f in os.listdir(vdir)
             if f.endswith(".md") and os.path.isfile(os.path.join(vdir, f))]
    ok, skipped = 0, 0
    for fname in files:
        base = fname[:-3]  # 去 .md
        if base.startswith("examples"):
            continue
        try:
            text = await asyncio.to_thread(
                Path(os.path.join(vdir, fname)).read_text, encoding="utf-8")
        except Exception as e:
            logger.warning("vendor 专家文件读取失败，跳过 %s：%s: %s", fname,
                           type(e).__name__, e)
            skipped += 1
            continue
        rec = parse_expert_md(base, text, _detect_dept(base))
        if not rec:
            skipped += 1
            continue
        rec.pop("dept_label", None)  # 展示用，非持久字段
        dept = rec.get("department") or "general"
        rec.setdefault("tags", [DEPT_NAMES.get(dept, dept)])
        rec["source_url"] = SOURCE_URL
        # 幂等：同 slug 已存在则更新人设
        q = await db.execute(select(ExpertTemplate).where(
            ExpertTemplate.company_id == company_id,
            ExpertTemplate.source_slug == rec["source_slug"]))
        row = q.scalars().first()
        if row:
            for k in ("name", "emoji", "color", "department", "description",
                      "system_prompt", "raw_md", "deliverables", "tags"):
                v = rec.get(k)
                if v is not None:
                    setattr(row, k, v)
        else:
            db.add(ExpertTemplate(company_id=company_id, license="MIT",
                                  source="agency-agents-zh", **rec))
        ok += 1
    await db.commit()
    return {"imported_or_updated": ok, "skipped": skipped,
            "files": len(files)}


@router.post("/import-original", status_code=201,
             response_model=ExpertOriginalImportOut)
async def import_original_experts(
    company_id: str = Query(...),
    disable_uncovered: int = Query(1, description="=1 时把没被原创覆盖的旧 agency-agents-zh 记录自动下架"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导入 AI-EOS 原创专家（content/experts/，LLM 独立重写，非上游拷贝）。

    - 按 source_slug 原地替换旧记录：内容换新 + source=ai-eos-original +
      license=AI-EOS 原创 + version=2.0（旧版仍可从 git 回溯）。
    - source_slug 规则与旧 /experts/import 完全一致（{dept}-{filename}），
      故能精准命中库里的旧 MIT 记录。
    - disable_uncovered=1：把"没被本次覆盖"的旧 agency-agents-zh 记录下架
      （enabled=False），确保专家库内剩下的全部为 AI-EOS 原创，商业化合规。
    """
    odir = _original_path()
    if not os.path.isdir(odir):
        raise HTTPException(404, "content/experts 不存在（需先跑 tools/rewrite_experts.py 生成）")
    files = [f for f in os.listdir(odir)
             if f.endswith(".md") and os.path.isfile(os.path.join(odir, f))]
    ok, created, updated, skipped = 0, 0, 0, 0
    covered_slugs = set()
    for fname in files:
        base = fname[:-3]
        try:
            text = await asyncio.to_thread(
                Path(os.path.join(odir, fname)).read_text, encoding="utf-8")
        except Exception as e:
            # 原实现 open().read() 句柄未关闭；改 with 后失败仍跳过该文件，但要留痕
            logger.warning("专家文件读取失败，跳过 %s：%s: %s", fname,
                           type(e).__name__, e)
            skipped += 1
            continue
        rec = parse_expert_md(base, text, _detect_dept(base))
        if not rec or not rec.get("name"):
            skipped += 1
            continue
        slug = rec["source_slug"]
        covered_slugs.add(slug)
        rec.pop("dept_label", None)
        dept = rec.get("department") or "general"
        rec.setdefault("tags", [DEPT_NAMES.get(dept, dept)])
        # 原创标记
        rec["license"] = "AI-EOS 原创"
        rec["source"] = "ai-eos-original"
        rec["version"] = "2.0"
        rec.pop("source_url", None)  # 原创，不再指向上游
        q = await db.execute(select(ExpertTemplate).where(
            ExpertTemplate.company_id == company_id,
            ExpertTemplate.source_slug == slug))
        row = q.scalars().first()
        if row:
            for k in ("name", "emoji", "color", "department", "description",
                     "system_prompt", "raw_md", "deliverables", "tags",
                     "license", "source", "version"):
                v = rec.get(k)
                if v is not None:
                    setattr(row, k, v)
            row.enabled = True  # 曾被下架的也随本次原创更新重新上架
            updated += 1
        else:
            db.add(ExpertTemplate(company_id=company_id, **rec))
            created += 1
        ok += 1

    # 下架没被原创覆盖的旧上游记录（它们 source 仍是 agency-agents-zh）
    disabled = 0
    if disable_uncovered:
        q2 = await db.execute(select(ExpertTemplate).where(
            ExpertTemplate.company_id == company_id,
            ExpertTemplate.source == "agency-agents-zh",
            ExpertTemplate.enabled.is_(True)))
        for old in q2.scalars().all():
            if old.source_slug not in covered_slugs:
                old.enabled = False
                disabled += 1

    await db.commit()
    return {
        "imported_or_updated": ok, "created": created, "updated": updated,
        "skipped": skipped, "files": len(files),
        "disabled_legacy": disabled,
        "note": "专家库已全部替换为 AI-EOS 原创内容（见 docs/EXPERTS_STATEMENT.md）",
    }


def _original_path() -> str:
    """找 content/experts（AI-EOS 原创专家目录），逐级向上探测。"""
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        cand = os.path.join(cur, "content", "experts")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "..", "content", "experts")


@router.get("", response_model=list[ExpertPublicOut])
async def list_experts(
    company_id: str = Query(...),
    department: str = Query(None),
    q: str = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conds = [ExpertTemplate.company_id == company_id,
             ExpertTemplate.enabled.is_(True)]
    if department and department not in ("all", ""):
        conds.append(ExpertTemplate.department == department)
    if q:
        like = f"%{q}%"
        conds.append(or_(ExpertTemplate.name.ilike(like),
                         ExpertTemplate.description.ilike(like),
                         ExpertTemplate.tags.ilike(like)))
    rows = (await db.execute(select(ExpertTemplate).where(*conds)
                             .order_by(ExpertTemplate.department,
                                       ExpertTemplate.name))).scalars().all()
    return [r.public_dict() for r in rows]


@router.get("/departments", response_model=list[ExpertDepartmentOut])
async def expert_departments(company_id: str = Query(...),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    q = await db.execute(
        select(ExpertTemplate.department, func.count())
        .where(ExpertTemplate.company_id == company_id)
        .group_by(ExpertTemplate.department))
    return [{"department": d, "count": n}
            for d, n in q.all() if d and d != "examples"]


class SearchIn(BaseModel):
    query: str
    limit: int = 3


@router.post("/search",
             response_model=list[ExpertPickOut | ExpertSearchMissOut])
async def search_experts(
    data: SearchIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """P1：自然语言找专家 — 列全量人设摘要 → LLM 选最匹配的 Top N。"""
    from app.model_gateway import ModelGateway, set_usage_context
    rows = (await db.execute(select(ExpertTemplate).where(
        ExpertTemplate.company_id == company_id,
        ExpertTemplate.enabled.is_(True)))).scalars().all()
    if not rows:
        raise HTTPException(404, "专家库为空，请先导入")
    catalog = "\n".join(
        f"{i+1}. {r.name}（{DEPT_NAMES.get(r.department, r.department)}）："
        f"{(r.description or '')[:80]}" for i, r in enumerate(rows[:300]))

    from app.models.agent import AgentModelConfig
    mq = await db.execute(select(AgentModelConfig).limit(1))
    m0 = mq.scalars().first()
    if not m0:
        raise HTTPException(503, "没有可用模型，请先在模型配置页接入")
    model_ids = [m0.model_id]
    gw = ModelGateway(db)
    set_usage_context(company_id=company_id, scenario="EXPERT_SEARCH")
    result = await gw.chat_with_agent_models(
        model_ids,
        [{"role": "system", "content":
          "你是专家匹配引擎。根据老板需求从编号列表中挑出最匹配的专家，"
          "只输出 JSON：{\"picks\":[{\"id\":编号,\"reason\":\"一句理由\"}]}"},
         {"role": "user",
          "content": f"老板需求：{data.query}\n\n候选专家目录：\n{catalog}"}],
        config={"max_tokens": 400})
    import json
    raw = (result.get("content") or "{}").strip()
    s, e = raw.find("["), raw.rfind("]")
    picks = []
    if s >= 0:
        try:
            arr = json.loads(raw[s:e + 1])
            for p in arr[:data.limit]:
                idx = int(p.get("id", 0)) - 1
                if 0 <= idx < len(rows):
                    r = rows[idx]
                    picks.append({**r.public_dict(), "reason": p.get("reason", "")})
        except Exception as e:
            # LLM 返回非 JSON/结构异常：降级走"请改关键词重试"提示，但必须留痕
            logger.warning("专家检索 LLM 结果解析失败：%s: %s（原始输出前 200 字：%s）",
                           type(e).__name__, e, raw[:200])
    return picks or [{"id": None, "message": "LLM 未返回有效结果，请改关键词重试"}]


class TeamIn(BaseModel):
    goal: str          # 一句话，如"组建新品上市小组"
    headcount: int = 5


@router.post("/team", response_model=ExpertTeamOut)
async def build_team(
    data: TeamIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """P2：一句话组建专家团队 — LLM 从专家库选 N 个工种 → 批量建工厂蓝图 Job。"""
    from app.model_gateway import ModelGateway, set_usage_context
    rows = (await db.execute(select(ExpertTemplate).where(
        ExpertTemplate.company_id == company_id,
        ExpertTemplate.enabled.is_(True)))).scalars().all()
    if not rows:
        raise HTTPException(404, "专家库为空，请先导入")
    catalog = "\n".join(
        f"{i+1}. {r.name}（{DEPT_NAMES.get(r.department, r.department)}）"
        for i, r in enumerate(rows[:300]))
    from app.models.agent import AgentModelConfig
    mq = await db.execute(select(AgentModelConfig).limit(1))
    m0 = mq.scalars().first()
    if not m0:
        raise HTTPException(503, "没有可用模型，请先在模型配置页接入")
    model_ids = [m0.model_id]
    gw = ModelGateway(db)
    set_usage_context(company_id=company_id, scenario="EXPERT_TEAM")
    result = await gw.chat_with_agent_models(
        model_ids,
        [{"role": "system", "content":
          "你是团队组建引擎。根据老板目标从编号列表选互补的团队（不重复编号），"
          "只输出 JSON：{\"team\":[{\"id\":编号,\"task\":\"该成员负责的事\"}]}"},
         {"role": "user",
          "content": (f"老板目标：{data.goal}\n\n请组建 {data.headcount} 人团队。\n\n"
                      f"候选专家：\n{catalog}")}],
        config={"max_tokens": 500})
    import json
    raw = (result.get("content") or "{}").strip()
    s, e = raw.find("["), raw.rfind("]")
    team = []
    if s >= 0:
        try:
            for p in json.loads(raw[s:e + 1])[:data.headcount]:
                idx = int(p.get("id", 0)) - 1
                if 0 <= idx < len(rows):
                    team.append({"template": rows[idx].public_dict(),
                                "task": p.get("task", "")})
        except Exception as e:
            logger.warning("组队 LLM 结果解析失败：%s: %s（原始输出前 200 字：%s）",
                           type(e).__name__, e, raw[:200])
    # 批量建蓝图 Job（每个模板走既有确认链：审查→验收→上岗）
    from app.factory_engine import AgentFactoryJob
    jobs = []
    for t in team:
        tpl = t["template"]
        bp = {"name": tpl["name"], "role": tpl["name"],
              "source_expert": tpl["id"],
              "responsibilities": [t["task"] or tpl["description"][:100]],
              "required_skills": [], "knowledge_scope": [],
              "system_prompt": next((r.system_prompt for r in rows
                                     if r.id == tpl["id"]), ""),
              "knowledge_base_hint": "expert-template",
              "permission_level": "standard"}
        job = AgentFactoryJob(id=str(__import__("uuid").uuid4()),
                              company_id=company_id,
                              goal=f"专家库雇佣：{tpl['name']}（{data.goal}）",
                              blueprint=bp, created_by=user.id)
        db.add(job)
        jobs.append({"template": tpl, "task": t["task"], "job_id": job.id})
    await db.commit()
    # 雇佣计数
    for t in team:
        row = await db.get(ExpertTemplate, t["template"]["id"])
        if row:
            row.hired_count = (row.hired_count or 0) + 1
    await db.commit()
    return {"goal": data.goal, "team_size": len(jobs),
            "jobs": jobs,
            "note": "团队蓝图已创建，请在工厂页逐个审查批准"}


@router.get("/{eid}", response_model=ExpertDetailOut)
async def expert_detail(eid: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    t = await db.get(ExpertTemplate, eid)
    if not t or t.company_id != company_id:
        raise HTTPException(404, "not found")
    return {**t.public_dict(), "raw_md": t.raw_md or "",
            "system_prompt": t.system_prompt or ""}


# ---------------------------------------------------------------------------
# V5.0 专家管理三件套：编辑 / 复制 / 手动新建
# ---------------------------------------------------------------------------
class ExpertEditIn(BaseModel):
    name: str | None = None
    emoji: str | None = None
    color: str | None = None
    department: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    raw_md: str | None = None
    enabled: bool | None = None


@router.put("/{eid}", response_model=ExpertDetailOut)
async def expert_edit(eid: str, data: ExpertEditIn,
                      company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """编辑专家：已发布专家的修改即时生效（source 仍为 ai-eos-original）。"""
    t = await db.get(ExpertTemplate, eid)
    if not t or t.company_id != company_id:
        raise HTTPException(404, "not found")
    for k in ("name", "emoji", "color", "department", "description",
              "system_prompt", "raw_md", "enabled"):
        v = getattr(data, k)
        if v is not None:
            setattr(t, k, v)
    if data.raw_md is not None:
        # 重提交付物章节（验收出题用）
        rec = parse_expert_md(t.source_slug, data.raw_md, t.department or "general")
        if rec:
            t.deliverables = rec.get("deliverables") or []
            if not (t.description or "") and rec.get("description"):
                t.description = rec["description"]
    await db.commit()
    return {**t.public_dict(), "raw_md": t.raw_md or "",
            "system_prompt": t.system_prompt or ""}


@router.post("/{eid}/copy", status_code=201, response_model=ExpertDetailOut)
async def expert_copy(eid: str, company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """复制专家：生成「名称（副本）」新记录，继承人设/章节，可继续编辑。"""
    t = await db.get(ExpertTemplate, eid)
    if not t or t.company_id != company_id:
        raise HTTPException(404, "not found")
    slug = f"custom-copy-{uuid.uuid4().hex[:8]}"
    clone = ExpertTemplate(
        id=str(uuid.uuid4()), company_id=company_id,
        source="ai-eos-original", source_slug=slug,
        license="AI-EOS 原创", version="1.0",
        name=f"{t.name}（副本）", emoji=t.emoji, color=t.color,
        department=t.department, tags=list(t.tags or []),
        description=t.description, system_prompt=t.system_prompt,
        raw_md=t.raw_md, deliverables=list(t.deliverables or []),
        enabled=True)
    db.add(clone)
    await db.commit()
    return {**clone.public_dict(), "raw_md": clone.raw_md or "",
            "system_prompt": clone.system_prompt or ""}


class ExpertManualIn(BaseModel):
    name: str                                  # 必须
    emoji: str | None = None
    color: str | None = None
    department: str | None = "general"
    description: str | None = None
    system_prompt: str | None = None           # 人设正文（8 章节或自由文本）
    attachments: list[str] | None = None      # 📎 附件引用（上传后的文件 URL/路径）
    tags: list[str] | None = None


@router.post("/manual", status_code=201, response_model=ExpertDetailOut)
async def expert_manual(data: ExpertManualIn,
                       company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """手动新建专家：不依赖 302 预设，老板凭空定义专家人设（带附件）。

    附件在创建前已单独上传（files 端点），这里只登记引用。
    source_slug=custom-<8hex>，与预设专家隔离。"""
    if not data.name.strip():
        raise HTTPException(400, "专家名称不能为空")
    slug = f"custom-{uuid.uuid4().hex[:8]}"
    text = data.system_prompt or data.description or ""
    rec = parse_expert_md(slug, f"# {data.name}\n\n{text}", data.department or "general") \
        if text.strip() else None
    t = ExpertTemplate(
        id=str(uuid.uuid4()), company_id=company_id,
        source="ai-eos-original", source_slug=slug,
        license="AI-EOS 原创", version="1.0",
        name=data.name.strip(),
        emoji=data.emoji or "🧑‍💼", color=data.color or "#6366f1",
        department=data.department or "general",
        tags=data.tags or [data.department or "general"],
        description=data.description,
        system_prompt=data.system_prompt or "",
        raw_md=data.system_prompt or "",
        deliverables=(rec or {}).get("deliverables") or [],
        enabled=True)
    if data.attachments:
        t.tags = list(dict.fromkeys((data.tags or []) + data.attachments))
    db.add(t)
    await db.commit()
    return {**t.public_dict(), "raw_md": t.raw_md or "",
            "system_prompt": t.system_prompt or ""}


@router.post("/{eid}/to-blueprint", response_model=ExpertBlueprintOut)
async def expert_to_blueprint(eid: str, company_id: str = Query(...),
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """专家模板 → 工厂蓝图 Job（走既有审查→验收→上岗确认链）。"""
    import uuid as _uuid
    t = await db.get(ExpertTemplate, eid)
    if not t or t.company_id != company_id:
        raise HTTPException(404, "not found")
    from app.factory_engine import AgentFactoryJob
    bp = {"name": t.name, "role": t.name, "source_expert": t.id,
          "responsibilities": (t.deliverables or [])[:4] or
          [(t.description or "")[:150]],
          "required_skills": [d[:40] for d in (t.deliverables or [])[:3]],
          "knowledge_scope": (t.tags or [])[:4] or [t.department],
          "system_prompt": t.system_prompt or "",
          "knowledge_base_hint": "expert-template",
          "permission_level": "standard"}
    job = AgentFactoryJob(id=str(_uuid.uuid4()), company_id=company_id,
                          goal=f"专家库雇佣：{t.name}",
                          blueprint=bp, created_by=user.id)
    db.add(job)
    t.hired_count = (t.hired_count or 0) + 1
    await db.commit()
    return {"job_id": job.id, "template": t.public_dict(),
            "note": "蓝图已创建，请在工厂页审查批准后上岗"}
