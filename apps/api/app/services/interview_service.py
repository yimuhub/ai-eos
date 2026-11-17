"""访谈自动完善服务（V6.2）：部门访谈资料 → 总控提炼 → 专家专属化。

流程（用户口径：「资料直接给到总控 Agent，它自动去完善相应专家的技能」）：
1. 部门访谈 → 文字/语音转写 → POST /interviews 登记（status=uploaded）；
2. POST /interviews/{id}/digest → 组装「当前专家档案 + 访谈全文」喂给
   模型网关（公司已配置的模型），要求输出结构化 JSON；
3. 应用：技能合并（按名去重）+ 人设增强段 + interview_history 版本留痕；
   指定了 agent_id 时同步已上岗 Agent 的 system_prompt。
失败 → status=failed + error 落账，绝不半途改写专家。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.logging_config import get_logger

logger = get_logger(__name__)

_MAX_TRANSCRIPT = 20000
_DIGEST_TIMEOUT_S = 300


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_digest_messages(expert_row, transcript: str) -> list[dict]:
    """总控提炼的两段 prompt：档案上下文 + 访谈原文，强约束输出 JSON。"""
    skills_text = "\n".join(
        f"- {s.get('name')}（{s.get('category') or '通用'}，Lv{s.get('level') or 3}）："
        f"{s.get('description') or ''}"
        for s in (expert_row.skills or []))
    messages = [
        {"role": "system", "content": (
            "你是 AI-EOS 的总控教官，负责把一线访谈转化为专家的技能升级。"
            "只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。"
            "JSON 结构："
            '{"summary": "<一段话总结访谈给专家带来了什么>", '
            '"new_skills": [{"name": "...", "description": "<具体做法要点 40-80 字>", '
            '"category": "<技能分组>", "level": 1-5}], '
            '"prompt_additions": ["<并入专家人设的行为规则/经验要点，每条一句话>"], '
            '"notes": "<其他值得记录的信息>"}'
            "\n要求：new_skills 只收访谈里真实出现的做法/经验（去水、去寒暄），"
            "与现有技能重名的合并进 description 而不是新建；level 按熟练度 1-5。")},
        {"role": "user", "content": (
            f"## 当前专家档案\n姓名：{expert_row.name}\n"
            f"细分领域：{expert_row.category or '（未标注）'}\n"
            f"职责：{expert_row.description or ''}\n"
            f"现有技能：\n{skills_text or '（暂无）'}\n\n"
            f"## 部门访谈记录（原文）\n{transcript[:_MAX_TRANSCRIPT]}")}
    ]
    return messages


def parse_digest(text: str) -> dict:
    """从模型输出里抠 JSON（容忍 ```json 包裹与前后杂文）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    raw = text[start:end + 1]
    data = json.loads(raw)
    for key in ("summary", "new_skills", "prompt_additions"):
        if key not in data:
            raise ValueError(f"digest 缺少 {key}")
    if not isinstance(data["new_skills"], list) \
            or not isinstance(data["prompt_additions"], list):
        raise ValueError("new_skills/prompt_additions 必须是数组")
    return data


def apply_digest(expert_row, digest: dict) -> dict:
    """把提炼结果应用到专家模板：技能合并 + 人设增强 + 留痕。返回增量统计。"""
    existing = {s.get("name"): s for s in (expert_row.skills or [])
                if s.get("name")}
    added = 0
    for sk in digest.get("new_skills") or []:
        name = str(sk.get("name") or "").strip()
        if not name or name in existing:
            continue
        entry = {"name": name,
                 "description": str(sk.get("description") or "")[:300],
                 "category": str(sk.get("category") or "访谈新增"),
                 "level": max(1, min(5, int(sk.get("level") or 3))),
                 "origin": "interview"}
        existing[name] = entry
        added += 1
    expert_row.skills = list(existing.values())

    additions = [str(a) for a in (digest.get("prompt_additions") or []) if a]
    if additions:
        stamp = _now().strftime("%Y-%m-%d")
        block = "\n\n## 来自一线访谈（%s）\n" % stamp + "\n".join(
            f"- {a}" for a in additions)
        expert_row.system_prompt = ((expert_row.system_prompt or "") + block)[:20000]

    history = list(expert_row.interview_history or [])
    history.append({
        "at": _now().isoformat(),
        "summary": digest.get("summary"),
        "skills_added": added,
        "prompt_rules": len(additions),
        "notes": digest.get("notes"),
    })
    expert_row.interview_history = history[-50:]
    return {"skills_added": added, "prompt_rules": len(additions)}


async def sync_to_agent(agent, expert_row, db) -> int:
    """把完善后的专家人设同步到已上岗 Agent（指定 agent_id 时）。"""
    if agent is None:
        return 0
    agent.system_prompt = expert_row.system_prompt
    await db.commit()
    return 1


async def run_digest(db, interview, expert_row, model_ids, gateway_fn) -> dict:
    """执行一次总控提炼并应用；失败落账不抛出。返回最终状态 dict。"""
    import traceback

    interview.status = "digested"
    await db.commit()
    try:
        messages = build_digest_messages(expert_row, interview.transcript)
        result = await gateway_fn(model_ids, messages)
        digest = parse_digest(str(result.get("content") or ""))
        stats = apply_digest(expert_row, digest)
        interview.digest = {**digest, "applied": stats}
        interview.status = "applied"
        await db.commit()
        logger.info("访谈提炼完成 company=%s expert=%s +%d 技能",
                    interview.company_id, expert_row.name, stats["skills_added"])
        return {"status": "applied", "digest": interview.digest, **stats}
    except Exception as exc:  # noqa: BLE001 — 失败如实落账
        logger.warning("访谈提炼失败 interview=%s: %s", interview.id, exc,
                       exc_info=True)
        interview.status = "failed"
        interview.error = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        return {"status": "failed", "error": interview.error}
