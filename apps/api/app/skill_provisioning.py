"""员工技能自动配备：本地技能库 → 公开技能市场 → CEO 生成专属技能。

## 为什么要「三级兜底」

老板不需要替每名员工手工装技能，但技能也不能凭空长出来：

1. **本地技能库**：公司已有（内置 / 自建 / 导入）技能里按岗位与任务描述打分，
   命中即装——零成本、零外呼。
2. **公开技能市场**：本地没有时，去 GitHub 上的公开技能目录找一份
   ``SKILL.md``。抓取内容必须过安全审核（`security_scan`），命中危险模式
   一律不装；只把文本当方法论注入提示词，**不执行其中任何代码**。
3. **CEO 生成**：市场也没有时，由负责人按岗位与任务上下文生成一张技能卡
   （适用场景/步骤/交付标准/常见错误），落库为该公司专属技能后安装。

任何一级失败都只降级到下一级，不会让「建员工」或「开工」失败；失败原因写
warning 与审计事件，避免静默降级。

## 安全边界

* 市场抓取只允许 ``https://raw.githubusercontent.com``，有超时与大小上限；
* 抓到的文本保存前截断，严禁执行、严禁写文件、严禁当代码用；
* 自动外呼受 ``AIEOS_SKILL_MARKET_AUTO`` 开关控制（默认开），测试环境置 0；
* 每次配备最多绑定 ``MAX_AUTO_SKILLS`` 个技能，防止提示词被塞爆。
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.knowledge_engine import _tokens
from app.models.agent import Agent, AgentModelConfig
from app.models.skill import AgentSkill, Skill as SkillRow, SkillStatus
from app.services.event_system import log_event

logger = get_logger(__name__)

MAX_AUTO_SKILLS = 3
MARKET_TIMEOUT_S = 8
MARKET_MAX_BYTES = 64 * 1024
SKILL_TEXT_LIMIT = 4000

MARKET_REPOS = [
    {"name": "anthropics/skills", "desc": "Anthropic 官方 Agent Skills",
     "api": "https://api.github.com/repos/anthropics/skills/contents/skills",
     "raw": "https://raw.githubusercontent.com/anthropics/skills/main/{path}"},
    {"name": "awesome-claude-code", "desc": "社区精选 Claude 技能合集",
     "api": "https://api.github.com/repos/hesreallyhim/awesome-claude-code/contents/skills",
     "raw": "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/skills/{path}"},
]

DANGEROUS_PATTERNS = [
    "rm -rf", "curl *| bash", "curl *| sh", "wget *| sh", "eval(",
    "exec(", "os.system", "subprocess", "__import__", "base64 -d",
    "chmod 777", "/etc/passwd", ".ssh", "crypto", "bitcoin", "keylogger",
]

# 远程脚本执行：URL 长度不限，只要把下载内容直接管道给 shell 就算危险
_PIPE_TO_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^\n]{0,300}\|\s*(?:ba|z|k)?sh\b", re.I)


def security_scan(text: str) -> dict:
    """技能内容安全审核：命中危险模式则标记。

    除了字面量模式，单独识别 ``curl ... | bash`` / ``wget ... | sh``：
    原来的 `curl *| bash` 是字面量匹配，URL 一长就漏检，等于把最典型的
    远程脚本执行放过去。这里用正则跨任意 URL 匹配。
    """
    low = (text or "").lower()
    hits = [pat.replace("*", "") for pat in DANGEROUS_PATTERNS
            if ("*" in pat and pat.replace(" *", " ") in low)
            or ("*" not in pat and pat in low)]
    if _PIPE_TO_SHELL.search(low):
        hits.append("curl|wget | sh")
    return {"safe": len(hits) == 0, "hits": hits,
            "level": "pass" if not hits else ("high" if len(hits) >= 3 else "warn")}


def _env_on(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _now_query(*parts: str | None) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())[:2000]


def _overlap(query_tokens: set[str], text: str) -> float:
    """查询 token 与技能文本的覆盖率（0~1），与知识库检索同口径。"""
    if not query_tokens:
        return 0.0
    tokens = _tokens(text or "")
    if not tokens:
        return 0.0
    return len(query_tokens & tokens) / (len(query_tokens) ** 0.5
                                         * len(tokens) ** 0.5)


async def ensure_builtin_skills(db: AsyncSession) -> dict[str, SkillRow]:
    """确保内置技能在库里（幂等），返回 slug → 行。"""
    from app.skill_system import BUILTIN_SKILLS

    out: dict[str, SkillRow] = {}
    for cls in BUILTIN_SKILLS:
        inst = cls()
        row = (await db.execute(
            select(SkillRow).where(SkillRow.slug == inst.id))).scalars().first()
        if row is None:
            row = SkillRow(
                slug=inst.id, name=inst.name, description=inst.description,
                version=inst.version, input_schema=inst.input_schema,
                output_schema=inst.output_schema,
                permissions=inst.permissions)
            db.add(row)
            await db.flush()
        out[inst.id] = row
    return out


async def _bound_skill_ids(db: AsyncSession, agent_id: str) -> set[str]:
    return set((await db.execute(
        select(AgentSkill.skill_id).where(AgentSkill.agent_id == agent_id)
    )).scalars().all())


async def bind_skill(db: AsyncSession, agent_id: str,
                     skill: SkillRow) -> bool:
    """幂等安装：已装返回 False，新装返回 True。"""
    exists = (await db.execute(
        select(AgentSkill.id).where(AgentSkill.agent_id == agent_id,
                                    AgentSkill.skill_id == skill.id)
        .limit(1))).first()
    if exists:
        return False
    db.add(AgentSkill(agent_id=agent_id, skill_id=skill.id))
    await db.flush()
    return True


async def local_skill_matches(db: AsyncSession, company_id: str,
                              query: str, limit: int = 3) -> list[SkillRow]:
    """在本地技能库里挑最相关的技能。

    可见范围与 `/skills` 一致：系统内置（company_id 为空）+ 本公司技能。
    """
    q_tokens = _tokens(query)
    rows = (await db.execute(
        select(SkillRow).where(
            SkillRow.status == SkillStatus.ENABLED,
            or_(SkillRow.company_id.is_(None),
                SkillRow.company_id == company_id))
        .limit(200))).scalars().all()
    scored: list[tuple[float, SkillRow]] = []
    for row in rows:
        instruction = ""
        if isinstance(row.input_schema, dict):
            instruction = str(row.input_schema.get("instruction") or "")
        text = " ".join(filter(None, [row.name, row.description, instruction[:800]]))
        score = _overlap(q_tokens, text)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return [row for _score, row in scored[:limit]]


# ---------------------------------------------------------------------------
# 公开技能市场
# ---------------------------------------------------------------------------

async def _fetch_market_repo(client: httpx.AsyncClient, repo: dict,
                             query: str) -> list[dict]:
    try:
        resp = await client.get(
            repo["api"],
            headers={"User-Agent": "ai-eos",
                     "Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:  # noqa: BLE001 — 单个仓库失败不影响其它来源
        logger.warning("技能市场仓库 %s 抓取失败，已跳过：%s: %s",
                       repo["name"], type(e).__name__, e)
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if it.get("type") != "dir":
            continue
        name = it.get("name", "")
        if query and query.lower() not in name.lower():
            continue
        out.append({
            "name": name, "repo": repo["name"], "path": it.get("path"),
            "url": it.get("html_url"), "source_market": repo["desc"],
            "raw_url": repo["raw"].format(path=f"{it.get('path')}/SKILL.md"),
        })
    return out


async def search_market_skills(query: str, limit: int = 5) -> list[dict]:
    """并发搜索公开技能市场；单仓库失败只跳过自己。"""
    async with httpx.AsyncClient(timeout=MARKET_TIMEOUT_S,
                                 follow_redirects=True) as client:
        per_repo = await asyncio.gather(
            *[_fetch_market_repo(client, repo, query) for repo in MARKET_REPOS],
            return_exceptions=True)
    results: list[dict] = []
    for repo, r in zip(MARKET_REPOS, per_repo):
        if isinstance(r, BaseException):
            logger.warning("技能市场仓库 %s 抓取异常，已跳过：%s",
                           repo["name"], r)
            continue
        results.extend(r)
    return results[:limit]


async def fetch_market_skill(raw_url: str) -> str:
    """拉取市场技能正文（只允许 GitHub raw，大小上限内截断）。"""
    if not (raw_url or "").startswith("https://raw.githubusercontent.com"):
        raise ValueError("仅支持 GitHub raw 地址")
    async with httpx.AsyncClient(timeout=MARKET_TIMEOUT_S,
                                 follow_redirects=True) as client:
        resp = await client.get(raw_url, headers={"User-Agent": "ai-eos"})
        resp.raise_for_status()
        if len(resp.content) > MARKET_MAX_BYTES:
            raise ValueError("技能文件过大，已拒绝")
        return resp.content.decode("utf-8", errors="replace")


def _pick_market_candidate(query: str, items: list[dict]) -> dict | None:
    q_tokens = _tokens(query)
    best: tuple[float, dict] | None = None
    for item in items:
        score = _overlap(q_tokens, str(item.get("name") or ""))
        if best is None or score > best[0]:
            best = (score, item)
    if best is None:
        return items[0] if items else None
    # 名称完全没重叠时仍允许试第一个：任务描述往往比目录名更具体
    return best[1]


async def _install_market_skill(db: AsyncSession, company_id: str,
                                query: str) -> tuple[SkillRow | None, dict]:
    items = await search_market_skills(query, limit=5)
    if not items:
        return None, {"ok": False, "reason": "市场无结果"}
    item = _pick_market_candidate(query, items)
    try:
        content = await fetch_market_skill(item["raw_url"])
    except Exception as e:  # noqa: BLE001
        logger.warning("技能市场内容拉取失败 %s：%s: %s", item.get("raw_url"),
                       type(e).__name__, e)
        return None, {"ok": False, "reason": f"拉取失败：{type(e).__name__}"}
    scan = security_scan(content)
    if not scan["safe"]:
        return None, {"ok": False, "reason": "安全审核未通过",
                      "scan": scan, "name": item.get("name")}
    row = SkillRow(
        slug=f"mkt_{uuid.uuid4().hex[:10]}",
        name=str(item.get("name") or "市场技能")[:200],
        description=f"自动从 {item.get('repo')} 引入（{item.get('name')}）"[:500],
        version="1.0.0", status=SkillStatus.ENABLED, scope="exclusive",
        company_id=company_id, source="imported",
        input_schema={"instruction": content[:SKILL_TEXT_LIMIT],
                      "market_url": item.get("raw_url"),
                      "security_scan": scan,
                      "auto_installed": True})
    db.add(row)
    await db.flush()
    return row, {"ok": True, "name": row.name, "repo": item.get("repo"),
                 "security_scan": scan}


# ---------------------------------------------------------------------------
# CEO 生成专属技能
# ---------------------------------------------------------------------------

SKILL_PROMPT = """你是企业技能设计师。请为下面这个岗位/任务设计一张可执行的技能卡。
岗位：{role}
任务背景：{context}

要求：
- 技能必须针对性解决这类任务，不要写空话；
- 步骤要能照着做，交付标准要能判断合格；
- 只输出 JSON，不要任何其它文字：
{{"name":"技能名（不超过20字）",
  "description":"一句话说明这个技能做什么",
  "when_to_use":"什么情况下使用",
  "steps":["步骤1","步骤2","步骤3"],
  "quality_bar":"交付合格标准",
  "pitfalls":["常见错误1","常见错误2"]}}"""


async def _llm_chat(db: AsyncSession, agent: Agent,
                    messages: list[dict], max_tokens: int = 900) -> str:
    from app.model_gateway import ModelGateway, usage_context

    rows = (await db.execute(
        select(AgentModelConfig.model_id)
        .where(AgentModelConfig.agent_id == agent.id)
        .order_by(AgentModelConfig.created_at))).scalars().all()
    if not rows:
        rows = (await db.execute(
            select(AgentModelConfig.model_id)
            .join(Agent, Agent.id == AgentModelConfig.agent_id)
            .where(Agent.company_id == agent.company_id)
            .order_by(AgentModelConfig.created_at).limit(6))).scalars().all()
    model_ids = list(dict.fromkeys(rows))
    if not model_ids:
        return ""
    with usage_context(company_id=str(agent.company_id), agent=str(agent.id),
                       scenario="SKILL_GENERATION"):
        result = await ModelGateway(db).chat_with_agent_models(
            model_ids, messages, config={"max_tokens": max_tokens})
    return result.get("content") or ""


def _extract_json(text: str) -> dict:
    import json
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _skill_instruction(data: dict) -> str:
    steps = [str(s).strip() for s in (data.get("steps") or []) if str(s).strip()]
    pitfalls = [str(s).strip() for s in (data.get("pitfalls") or []) if str(s).strip()]
    lines = [f"【适用场景】{str(data.get('when_to_use') or '').strip()[:500]}"]
    if steps:
        lines.append("【执行步骤】\n" + "\n".join(
            f"{i}. {s[:300]}" for i, s in enumerate(steps[:10], 1)))
    if data.get("quality_bar"):
        lines.append(f"【交付标准】{str(data['quality_bar'])[:500]}")
    if pitfalls:
        lines.append("【常见错误】\n" + "\n".join(f"- {s[:300]}" for s in pitfalls[:6]))
    return "\n".join(lines)[:SKILL_TEXT_LIMIT]


async def generate_skill(db: AsyncSession, agent: Agent, role: str,
                         context: str) -> tuple[SkillRow | None, dict]:
    raw = await _llm_chat(db, agent, [
        {"role": "system", "content": "你是严谨的技能设计师，只输出 JSON。"},
        {"role": "user", "content": SKILL_PROMPT.format(
            role=role[:200], context=context[:1200])},
    ])
    data = _extract_json(raw)
    name = str(data.get("name") or "").strip()[:120]
    if not name:
        return None, {"ok": False, "reason": "模型没有返回可用技能卡"}
    instruction = _skill_instruction(data)
    scan = security_scan(instruction + " " + str(data.get("description") or ""))
    if not scan["safe"]:
        return None, {"ok": False, "reason": "生成内容安全审核未通过",
                      "scan": scan}
    row = SkillRow(
        slug=f"auto_{uuid.uuid4().hex[:10]}", name=name,
        description=str(data.get("description") or "")[:500] or None,
        version="1.0.0", status=SkillStatus.ENABLED, scope="exclusive",
        company_id=agent.company_id, source="custom",
        input_schema={"instruction": instruction,
                      "generated": True,
                      "when_to_use": data.get("when_to_use"),
                      "steps": data.get("steps") or [],
                      "quality_bar": data.get("quality_bar"),
                      "pitfalls": data.get("pitfalls") or [],
                      "security_scan": scan})
    db.add(row)
    await db.flush()
    return row, {"ok": True, "name": row.name, "security_scan": scan}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

async def provision_agent_skills(
    db: AsyncSession, agent: Agent, *,
    hints: list[str] | None = None,
    coordinator: Agent | None = None,
    allow_market: bool = False,
    allow_generate: bool = True,
    max_skills: int = MAX_AUTO_SKILLS,
) -> dict:
    """给 Agent 自动配备技能，按 本地库 → 市场 → 生成 逐级兜底。

    返回 ``{"bound": [...], "library": n, "market": n, "generated": n,
    "fallback": n, "errors": [...]}``；任何异常都不向上抛，调用方可以
    在「建员工 / 开工」的同一事务里安全调用。
    """
    hints = [h for h in (hints or []) if h and h.strip()]
    query = _now_query(agent.role, agent.name, agent.description, *hints)
    result: dict = {"bound": [], "library": 0, "market": 0,
                    "generated": 0, "fallback": 0, "errors": []}
    try:
        bound_ids = await _bound_skill_ids(db, str(agent.id))
        if coordinator is not None and str(coordinator.id) == str(agent.id):
            coordinator = None  # 自己不需要给自己当生成器

        # 1) 本地技能库
        for row in await local_skill_matches(db, agent.company_id, query,
                                             limit=max_skills):
            if len(bound_ids) >= max_skills:
                break
            if row.id in bound_ids:
                continue
            if await bind_skill(db, str(agent.id), row):
                bound_ids.add(row.id)
                result["library"] += 1
                result["bound"].append({
                    "id": row.id, "slug": row.slug, "name": row.name,
                    "source": "library", "reason": "本地技能库匹配"})

        # 2) 公开技能市场（仅 CEO/开工场景默认开启，受全局开关约束）
        if (allow_market and len(bound_ids) < max_skills
                and _env_on("AIEOS_SKILL_MARKET_AUTO", True)):
            row, info = await _install_market_skill(db, agent.company_id, query)
            if row is not None:
                if await bind_skill(db, str(agent.id), row):
                    bound_ids.add(row.id)
                    result["market"] += 1
                    result["bound"].append({
                        "id": row.id, "slug": row.slug, "name": row.name,
                        "source": "market",
                        "reason": f"技能市场：{info.get('repo') or '公开仓库'}"})
            elif not info.get("ok"):
                result["errors"].append(f"市场未命中：{info.get('reason')}")

        # 3) CEO 生成专属技能
        if (allow_generate and len(bound_ids) < max_skills
                and _env_on("AIEOS_SKILL_AUTO_GENERATE", True)):
            generator = coordinator or agent
            row, info = await generate_skill(
                db, generator, str(agent.role or agent.name), query)
            if row is not None:
                if await bind_skill(db, str(agent.id), row):
                    bound_ids.add(row.id)
                    result["generated"] += 1
                    result["bound"].append({
                        "id": row.id, "slug": row.slug, "name": row.name,
                        "source": "generated", "reason": "CEO 按岗位生成"})
            elif not info.get("ok"):
                result["errors"].append(f"生成技能失败：{info.get('reason')}")

        # 4) 兜底：至少给一项通用能力，避免「无技能上岗」
        if not bound_ids:
            builtins = await ensure_builtin_skills(db)
            fallback = builtins.get("web_search")
            if fallback is not None and await bind_skill(db, str(agent.id), fallback):
                result["fallback"] = 1
                result["bound"].append({
                    "id": fallback.id, "slug": fallback.slug,
                    "name": fallback.name, "source": "fallback",
                    "reason": "通用兜底能力"})

        if result["bound"]:
            await log_event(
                db, "skill.auto_bound", "agent", str(agent.id), "system",
                str(coordinator.id) if coordinator else None,
                {"skills": [b["name"] for b in result["bound"]],
                 "library": result["library"], "market": result["market"],
                 "generated": result["generated"],
                 "fallback": result["fallback"]},
                company_id=agent.company_id)
    except Exception as exc:  # noqa: BLE001 — 技能配备失败不能拖垮建员工/开工
        logger.warning("技能自动配备失败 agent=%s：%s: %s", agent.id,
                       type(exc).__name__, exc, exc_info=True)
        result["errors"].append(f"技能配备异常：{type(exc).__name__}")
    return result
