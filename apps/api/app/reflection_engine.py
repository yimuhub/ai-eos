"""Agent 自我反思引擎（借鉴 Hermes 记忆整理机制）。

Hermes 的记忆之道：
1. 常驻注入（AI-EOS 已有：recall → system prompt）
2. 主动存事实（已有：任务完成自动沉淀 EPISODIC）
3. 定期整理合并 ← 本模块补齐这一环

self_reflect(db, agent)：
- 取最近 N 条 EPISODIC 记忆 → LLM 归纳出「经验教训/工作偏好/知识要点」
- 写入 SEMANTIC 类型长期记忆（TTL 更长、重要度更高、去重）
- 反思本身也写一条审计日志

由 scheduler 的 reflect_agent 动作定期触发，或手动 API 触发。
"""
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.model_gateway import ModelGateway


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


REFLECT_SYSTEM_PROMPT = (
    "你是一个 AI Agent 的记忆整理师。下面是这个 Agent 近期完成任务的记录"
    "（EPISODIC 情景记忆）。请从中提炼出对未来工作有帮助的长期经验，"
    "输出一个 JSON 数组，每项包含 type 和 content 字段：\n"
    '- type: "lesson"（经验教训）| "preference"（工作方式偏好）| "knowledge"（领域知识要点）\n'
    "- content: 一句话概括（不超过80字），要具体、可复用\n\n"
    "要求：\n"
    "1. 只提炼真正有价值、可跨任务复用的内容；琐碎的一次性细节不要\n"
    "2. 合并重复主题\n"
    "3. 最多输出5条，没有值得记的就输出 []\n"
    '只输出 JSON 数组，不要其他文字。示例：[{"type":"knowledge","content":"..."}]'
)


async def self_reflect(db: AsyncSession, agent, max_recent: int = 12) -> dict:
    """对单个 Agent 执行一次自我反思：归纳近期经历 → 写入 SEMANTIC 长期记忆。"""
    from app.models.memory import AgentMemory, MemoryType

    # 1. 取最近的情景记忆
    rows = (await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_id == str(agent.id),
            AgentMemory.memory_type == MemoryType.EPISODIC,
        ).order_by(AgentMemory.created_at.desc()).limit(max_recent)
    )).scalars().all()

    if not rows:
        return {"reflected": False, "reason": "no episodic memories",
                "memories_written": 0}

    recent_text = "\n".join(
        f"- {m.created_at:%m-%d %H:%M} {m.content[:200]}"
        for m in reversed(rows))

    # 2. 已有 SEMANTIC 记忆（用于去重判断）
    existing = (await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_id == str(agent.id),
            AgentMemory.memory_type == MemoryType.SEMANTIC,
        ))).scalars().all()
    existing_contents = [m.content for m in existing]

    # 3. LLM 归纳
    gw = ModelGateway(db)
    messages = [
        {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
        {"role": "user", "content":
            f"已有长期记忆（避免重复）：\n"
            f"{chr(10).join('- ' + c[:100] for c in existing_contents[-20:]) or '（无）'}\n\n"
            f"近期任务记录：\n{recent_text}"},
    ]
    # 复用 agent_runtime 的模型优先级查询（primary -> secondary -> fallback）
    from app.models.agent import AgentModelConfig
    cfg_rows = (await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == str(agent.id))
    )).scalars().all()
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    model_ids = [c.model_id for c in sorted(
        cfg_rows, key=lambda c: order.get(c.role, 3))]
    if not model_ids:
        # 修复：员工没绑模型时此前必然 RuntimeError（调度器每轮 FAILED）。
        return {"reflected": False, "reason": "no model", "memories_written": 0}
    llm = await gw.chat_with_agent_models(model_ids, messages)
    raw = (llm.get("content") or "").strip()

    # 解析 JSON 数组（容错：剥掉 markdown 代码块）
    cleaned = raw
    if "```" in cleaned:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, _re.S)
        if m:
            cleaned = m.group(1)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        return {"reflected": True, "memories_written": 0,
                "raw": raw[:300], "note": "LLM 未返回有效 JSON"}

    try:
        items = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {"reflected": True, "memories_written": 0,
                "raw": raw[:300], "note": "JSON 解析失败"}

    if not isinstance(items, list):
        items = []

    # 4. 写入 SEMANTIC 长期记忆（简单去重：子串匹配已有记忆）
    written = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        mtype = str(item.get("type", "knowledge"))
        if not content:
            continue
        dup = any(content[:40] in ec for ec in existing_contents)
        if dup:
            continue
        label = {"lesson": "经验教训", "preference": "工作偏好",
                 "knowledge": "知识要点"}.get(mtype, "知识要点")
        mem = AgentMemory(
            id=str(uuid.uuid4()),
            agent_id=str(agent.id),
            company_id=str(agent.company_id),
            memory_type=MemoryType.SEMANTIC,
            content=f"[{label}] {content}",
            importance=0.8,
            context_ref=None,
            expires_at=None,  # SEMANTIC 长期保留
        )
        db.add(mem)
        written.append(f"{label}: {content[:60]}")

    await db.commit()
    return {"reflected": True, "episodic_reviewed": len(rows),
            "memories_written": len(written), "written": written}


async def record_experience(db: AsyncSession, agent_id: str,
                            company_id: str, title: str, content: str,
                            trigger: str | None = None):
    """兼容旧 reflection 路由：直接写入一条经验记忆。"""
    from app.models.memory import AgentMemory, MemoryType

    mem = AgentMemory(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        company_id=company_id,
        memory_type=MemoryType.EPISODIC,
        content=f"{title}: {content}"[:500],
        importance=0.7,
    )
    db.add(mem)
    await db.commit()
    return mem


async def reflect(db: AsyncSession, memory_row, trigger: str | None = None):
    """兼容旧 reflection 路由：对单条经验做 LLM 反思，返回文本。"""
    from app.models.agent import Agent as AgentModel
    agent = await db.get(AgentModel, str(memory_row.agent_id))
    gw = ModelGateway(db)
    messages = [
        {"role": "system", "content":
            "你是 AI Agent 的反思模块。根据以下经历，总结一条可复用的经验教训（不超过100字）。"},
        {"role": "user", "content": f"标题：{memory_row.content[:200]}"},
    ]
    from app.models.agent import AgentModelConfig
    cfg_rows = (await db.execute(
        select(AgentModelConfig).where(AgentModelConfig.agent_id == str(agent.id))
    )).scalars().all() if agent else []
    order = {"primary": 0, "secondary": 1, "fallback": 2}
    model_ids = [c.model_id for c in sorted(cfg_rows, key=lambda c: order.get(c.role, 3))]
    if not model_ids:
        # 修复：员工没绑模型时此前必然抛 RuntimeError（网关对空列表直接 raise），
        # 调度器的 reflect_agent 动作每轮 FAILED。如实返回"未反思"。
        return {"reflected": False, "reason": "no model"}
    llm = await gw.chat_with_agent_models(model_ids, messages)
    return {"summary": (llm.get("content") or "")[:300]}
