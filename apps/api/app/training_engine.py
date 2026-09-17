"""Training engine (P3-02): LLM extraction + governed apply/rollback."""
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning_engine import add_growth_event, ensure_profile, update_profile
from app.models.learning import GrowthEventType
from app.models.training import (TrainingApplication, TrainingLearningPoint,
                                 TrainingProposal, TrainingSession,
                                 TrainingStatus, TrainingType)

# 禁止训练触碰的档案字段（Identity 与核心规则）
PROTECTED_FIELDS = {"identity"}


async def extract_learning(db: AsyncSession, session: TrainingSession) -> dict:
    """用 LLM 从原始输入提取 Learning Points + Proposed Changes。"""
    from app.model_gateway import ModelGateway

    sys_prompt = (
        "你是 Agent 训练师。分析以下训练材料，输出JSON：\n"
        '{"summary":"一句话总结",'
        '"learning_points":[{"content":"...","category":"knowledge|skill|preference|capability"}],'
        '"proposals":[{"change_type":"knowledge_update|skill_improvement|preference_update|capability_update",'
        '"payload":{}}]}\n'
        "只输出JSON。")
    user_prompt = f"训练类型：{session.training_type.value}\n标题：{session.title}\n内容：{(session.raw_input or '')[:4000]}"

    gw = ModelGateway(db)
    # 用公司第一个可用模型（注意：chat_with_agent_models 需要 Model.id，
    # 即 AgentModelConfig.model_id，而非 AgentModelConfig.id）
    from sqlalchemy import select as _sel
    from app.models.agent import AgentModelConfig
    mq = await db.execute(_sel(AgentModelConfig).limit(1))
    m = mq.scalars().first()
    if not m:
        raise RuntimeError("没有可用模型")
    from app.model_gateway import set_usage_context
    set_usage_context(scenario="TRAINING", detail={"training": True})
    result = await gw.chat_with_agent_models(
        [m.model_id],
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_prompt}],
        config={"max_tokens": 1200})
    raw = result.get("content") or "{}"
    start, end = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[start:end + 1]) if start >= 0 else {}

    for lp in data.get("learning_points", []):
        db.add(TrainingLearningPoint(
            id=str(uuid.uuid4()), session_id=session.id,
            content=lp.get("content", ""), category=lp.get("category")))
    proposals = []
    for p in data.get("proposals", []):
        pr = TrainingProposal(id=str(uuid.uuid4()), session_id=session.id,
                              change_type=p.get("change_type", "knowledge_update"),
                              payload=p.get("payload", {}))
        db.add(pr)
        proposals.append(pr)
    session.summary = data.get("summary")
    session.status = TrainingStatus.EXTRACTED
    await db.commit()
    return {"summary": session.summary, "points": len(data.get("learning_points", [])),
            "proposal_ids": [p.id for p in proposals]}


async def apply_session(db: AsyncSession, session: TrainingSession,
                        agent, actor: str) -> list[str]:
    """应用所有 PROPOSED 变更到 Agent 档案/知识库，记录可回滚数据。"""
    pq = await db.execute(select(TrainingProposal).where(
        TrainingProposal.session_id == session.id))
    proposals = pq.scalars().all()

    applied_ids = []
    profile = await ensure_profile(db, agent, agent.company_id)
    for p in proposals:
        if p.change_type == "knowledge_update":
            # 知识写入知识库
            content = (p.payload or {}).get("content") or session.raw_input or ""
            doc = None
            if content.strip():
                from app.models.knowledge import KBDocument
                from app.knowledge_engine import ingest_document
                doc = __import__("app.models.knowledge", fromlist=["KBDocument"]).KBDocument(
                    id=str(uuid.uuid4()), company_id=agent.company_id,
                    project_id=None,
                    title=f"[训练]{session.title}",
                    source_type="text", content=content)
                db.add(doc)
                await db.flush()
                await ingest_document(db, doc)
            rollback = {"kb_doc_id": doc.id if doc else None}
        elif p.change_type == "preference_update":
            rollback = {"preferences": dict(profile.preferences or {})}
            merged = dict(profile.preferences or {})
            merged.update(p.payload or {})
            await update_profile(db, profile, {"preferences": merged},
                                 GrowthEventType.TRAINED,
                                 title=f"训练更新偏好：{session.title[:40]}",
                                 source_type="training",
                                 source_id=session.id, actor=actor)
        elif p.change_type == "capability_update":
            rollback = {"capabilities": list(profile.capabilities or [])}
            caps = set(profile.capabilities or [])
            caps.update((p.payload or {}).get("add", []))
            for rm in (p.payload or {}).get("remove", []):
                caps.discard(rm)
            await update_profile(db, profile, {"capabilities": sorted(caps)},
                                 GrowthEventType.CAPABILITY_CHANGED,
                                 title=f"训练更新能力：{session.title[:40]}",
                                 source_type="training",
                                 source_id=session.id, actor=actor)
        else:  # skill_improvement：写入知识库留档 + 记录学习目标（均可回滚）
            content = (p.payload or {}).get("content") or session.raw_input or ""
            doc = None
            if content.strip():
                from app.models.knowledge import KBDocument
                from app.knowledge_engine import ingest_document
                doc = KBDocument(
                    id=str(uuid.uuid4()), company_id=agent.company_id,
                    project_id=None,
                    title=f"[训练]{session.title}",
                    source_type="text", content=content)
                db.add(doc)
                await db.flush()
                await ingest_document(db, doc)
            rollback = {"kb_doc_id": doc.id if doc else None}
            db.add(__import__("app.models.learning", fromlist=["AgentLearningRecord"]).AgentLearningRecord(
                id=str(uuid.uuid4()), agent_id=agent.id,
                goal=(p.payload or {}).get("goal") or session.title))

        p.status = "APPLIED"
        app_rec = TrainingApplication(
            id=str(uuid.uuid4()), session_id=session.id, proposal_id=p.id,
            applied_changes=p.payload, rollback_data=rollback)
        db.add(app_rec)
        applied_ids.append(app_rec.id)

    session.status = TrainingStatus.APPLIED
    session.decided_at = _now_utc()
    await add_growth_event(db, agent.id, agent.company_id,
                           GrowthEventType.TRAINED,
                           f"完成训练：{session.title}",
                           detail={"points_applied": len(applied_ids)},
                           source_type="training", source_id=session.id,
                           actor=actor)
    await db.commit()
    return applied_ids


async def rollback_application(db: AsyncSession, application: TrainingApplication,
                               agent, actor: str) -> bool:
    """撤销一次已应用的训练变更。"""
    rb = application.rollback_data or {}
    profile = await ensure_profile(db, agent, agent.company_id)
    changed = False
    if "preferences" in rb:
        await update_profile(db, profile, {"preferences": rb["preferences"]},
                             GrowthEventType.CAPABILITY_CHANGED,
                             title="撤销训练变更(preferences)",
                             source_type="training-rollback", actor=actor)
        changed = True
    if "capabilities" in rb:
        await update_profile(db, profile, {"capabilities": rb["capabilities"]},
                             GrowthEventType.CAPABILITY_CHANGED,
                             title="撤销训练变更(capabilities)",
                             source_type="training-rollback", actor=actor)
        changed = True
    if rb.get("kb_doc_id"):
        from app.models.knowledge import KBDocument
        doc = await db.get(KBDocument, rb["kb_doc_id"])
        if doc:
            await db.delete(doc)
        changed = True
    if not changed:
        return False
    application.rolled_back_at = _now_utc()
    # 恢复提案状态
    prop = await db.get(TrainingProposal, application.proposal_id)
    if prop:
        prop.status = "REJECTED"
    await db.commit()
    return True


def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())
