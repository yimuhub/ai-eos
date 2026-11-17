"""行业专家包服务（V6.2）：内置目录 + 解锁到公司 + 访谈自动完善。

三层结构（用户口径：「行业专家包 + 行业内细分岗位专家 + 访谈专属化」）：
1. **内置目录**（磁盘 `app/expert_packs/<pack>/`）：母本，按行业分组、
   行业内细分岗位（农业包 = 小麦/玉米/土豆/大豆/植保/农资），每专家带
   标准化技能清单（市面上能查到的行业技能写成标准条目）；
2. **解锁到公司** = 把母本写入该公司的 `expert_templates`（公司隔离，
   用户随便编辑专属化，不影响母本；母本升级只影响后续解锁）；
3. **访谈自动完善**：部门访谈资料（文字/语音转写）→ 总控提炼（模型网关）
   → 结构化技能合并 + 人设增强 → 版本留痕（interview_history）。
   「使用中自我升级」另有两条既有通路：上岗 Agent 带 self_learning
   （反思引擎自动沉淀）与 skill_growth 任务成长。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.logging_config import get_logger

logger = get_logger(__name__)

PACKS_DIR = Path(__file__).resolve().parent.parent / "expert_packs"
_CAP_ENUM = ("persistent_memory", "web_browsing", "self_learning",
             "scheduled_work", "computer_use")


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


def list_expert_packs() -> list[dict]:
    """扫描内置专家包目录（含每个专家的完整内容）。"""
    packs = []
    if not PACKS_DIR.is_dir():
        return packs
    for path in sorted(PACKS_DIR.glob("*/pack.json")):
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
            experts = []
            for epath in sorted(path.parent.glob("experts/*.json")):
                experts.append(json.loads(epath.read_text(encoding="utf-8")))
            pack["experts"] = experts
            validate_expert_pack(pack)
            packs.append(pack)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("专家包 %s 内容非法，已跳过：%s", path, exc)
    return packs


def get_expert_pack(pack_id: str) -> dict | None:
    for pack in list_expert_packs():
        if pack["id"] == pack_id:
            return pack
    return None


def validate_expert_pack(pack: dict) -> dict:
    if not isinstance(pack, dict):
        raise ValueError("专家包内容必须是对象")
    for field in ("id", "industry", "name", "version", "description"):
        if not str(pack.get(field) or "").strip():
            raise ValueError(f"专家包缺少必填字段 {field}")
    experts = pack.get("experts") or []
    if not experts:
        raise ValueError("专家包至少要有 1 个专家")
    for e in experts:
        for field in ("id", "name", "category", "mission", "system_prompt"):
            if not str(e.get(field) or "").strip():
                raise ValueError(f"专家 {e.get('id')} 缺少必填字段 {field}")
        skills = e.get("skills") or []
        if len(skills) < 3:
            raise ValueError(f"专家 {e.get('id')} 的标准化技能少于 3 条")
        for sk in skills:
            if not str(sk.get("name") or "").strip():
                raise ValueError(f"专家 {e.get('id')} 有无名技能条目")
        caps = e.get("capability_packs") or []
        for cap in caps:
            if cap not in _CAP_ENUM:
                raise ValueError(f"capability_packs 含未知能力包：{cap}")
    return pack


async def unlock_expert_pack(db, pack_id: str, company_id: str,
                             user_id: str | None) -> int:
    """把内置专家包写入公司的 expert_templates（公司隔离、幂等）。

    幂等键：source_slug = f"{pack_id}:{expert_id}"。解锁后公司即可在
    专家库编辑/雇佣这些专家（编辑即专属化，不影响母本）。
    """
    from sqlalchemy import select

    from app.models.expert_template import ExpertTemplate

    pack = get_expert_pack(pack_id)
    if pack is None:
        raise ValueError(f"内置专家包 {pack_id!r} 不存在")
    validate_expert_pack(pack)

    created = 0
    for e in pack["experts"]:
        slug = f"{pack_id}:{e['id']}"
        exists = (await db.execute(
            select(ExpertTemplate.id).where(
                ExpertTemplate.company_id == company_id,
                ExpertTemplate.source_slug == slug))).first()
        if exists:
            continue
        db.add(ExpertTemplate(
            id=_uuid(), company_id=company_id,
            source=f"expert-pack:{pack_id}", source_slug=slug,
            version=str(pack.get("version")),
            name=str(e["name"])[:200], department=str(e.get("department") or
                                                    pack["industry"])[:100],
            tags=[e.get("category"), pack["industry"]],
            description=e.get("mission"),
            system_prompt=str(e.get("system_prompt") or "")[:6000],
            capability_packs=e.get("capability_packs") or [],
            skills=e.get("skills") or [],
            category=str(e.get("category"))[:60],
            expert_pack_id=pack_id,
            enabled=True))
        created += 1
    await db.commit()
    logger.info("专家包已解锁 company=%s pack=%s 新增专家=%d",
                company_id, pack_id, created)
    return created


async def seeded_expert_count(db, company_id: str, pack_id: str) -> int:
    from sqlalchemy import func, select as _select

    from app.models.expert_template import ExpertTemplate
    n = (await db.execute(
        _select(func.count()).select_from(ExpertTemplate).where(
            ExpertTemplate.company_id == company_id,
            ExpertTemplate.expert_pack_id == pack_id))).scalar()
    return int(n or 0)


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())
