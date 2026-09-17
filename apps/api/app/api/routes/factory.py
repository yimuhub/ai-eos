"""Agent Factory API (P3-08)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.logging_config import get_logger
from app.factory_engine import (AgentFactoryJob, FactoryJobStatus,
                                check_quota, generate_blueprint)
from app.models.agent import Agent, AgentModelConfig
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter(prefix="/factory", tags=["factory"])


class BlueprintIn(BaseModel):
    goal: str


@router.post("/blueprint", status_code=201)
async def create_blueprint(
    data: BlueprintIn,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """根据目标生成 Agent 蓝图（Proposal，未创建 Agent）。"""
    if not await check_quota(db, company_id):
        raise HTTPException(429, "今日创建配额已用尽（防无限创建）")
    try:
        bp = await generate_blueprint(db, company_id, data.goal)
    except Exception as e:
        raise HTTPException(502, f"蓝图生成失败: {e}")
    job = AgentFactoryJob(id=str(__import__("uuid").uuid4()),
                          company_id=company_id, goal=data.goal,
                          blueprint=bp, created_by=user.id)
    db.add(job)
    await db.commit()
    return {"job_id": job.id, "status": "BLUEPRINT", "blueprint": bp}


class ApproveIn(BaseModel):
    blueprint: dict | None = None   # 用户可编辑后的蓝图


@router.post("/jobs/{job_id}/approve")
async def approve_job(
    job_id: str,
    data: ApproveIn | None = None,
    bind_model_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户批准蓝图 → 创建 Agent（TESTING 状态）+ 绑定模型。"""
    job = await db.get(AgentFactoryJob, job_id)
    if not job or job.status != FactoryJobStatus.BLUEPRINT:
        raise HTTPException(409, "只有 BLUEPRINT 状态可批准")
    bp = (data.blueprint if data and data.blueprint else job.blueprint) or {}

    # 创建真实 Agent（DISABLED=测试期不可上岗，通过测试后转 IDLE）
    agent = Agent(
        company_id=job.company_id,
        name=bp.get("name") or "FactoryAgent",
        role=bp.get("role"),
        description=f"Factory创建。目标：{job.goal[:200]}",
        system_prompt=bp.get("system_prompt") or f"你是{bp.get('role', '助手')}。",
        status="DISABLED")
    db.add(agent)
    await db.flush()

    # 模型绑定：显式指定或复制公司第一个 Agent 的配置
    model_id = bind_model_id
    if not model_id:
        mq = await db.execute(select(AgentModelConfig).limit(1))
        first = mq.scalars().first()
        model_id = first.model_id if first else None
    if model_id:
        db.add(AgentModelConfig(agent_id=agent.id, role="primary",
                                model_id=model_id))

    await db.flush()  # 先持久化 Agent，避免后续 FK 违规
    # ---- 岗前培训：蓝图技能 → 专属技能入库 + 安装 + 训练会话（区别于普通新建的核心）----
    training_summary = []
    try:
        import uuid as _uuid
        from app.models.skill import AgentSkill, Skill as SkillRow, SkillStatus
        from app.models.training import (TrainingSession, TrainingProposal,
                                         TrainingLearningPoint, TrainingStatus,
                                         TrainingType)
        for sk_name in (bp.get("required_skills") or [])[:6]:
            slug = f"factory_{_uuid.uuid4().hex[:8]}"
            sk = SkillRow(slug=slug, name=sk_name,
                          description=f"Agent 工厂蓝图技能（{job.goal[:60]}）",
                          version="1.0.0", status=SkillStatus.ENABLED,
                          scope="exclusive", company_id=job.company_id,
                          source="custom",
                          input_schema={"instruction":
                                        f"岗位「{bp.get('role')}」的专属技能：{sk_name}。"
                                        f"知识领域：{', '.join(bp.get('knowledge_scope') or [])}"})
            db.add(sk)
            await db.flush()
            db.add(AgentSkill(agent_id=str(agent.id), skill_id=sk.id))
            training_summary.append(f"技能「{sk_name}」已注册并安装")

        # 训练会话：把蓝图的职责/知识域作为训练材料，LLM 提取学习点并应用到档案
        material = (f"岗位职责：{chr(10)}" +
                    chr(10).join(f"- {r}" for r in (bp.get("responsibilities") or [])) +
                    f"{chr(10)}知识领域：{', '.join(bp.get('knowledge_scope') or [])}"
                    + f"{chr(10)}系统提示词：{bp.get('system_prompt') or ''}")
        session = TrainingSession(
            id=str(_uuid.uuid4()), company_id=job.company_id, agent_id=str(agent.id),
            training_type=TrainingType.CONVERSATION,
            title=f"岗前培训：{job.goal[:40]}",
            raw_input=material, status=TrainingStatus.PENDING)
        db.add(session)
        await db.flush()

        from app.training_engine import extract_learning
        learning = await extract_learning(db, session)  # 内部已写 LearningPoint/Proposal
        n_pts = learning.get("points", 0)

        # 直接应用培训成果到 Agent 档案（记忆沉淀）
        from app.memory_engine import MemoryType, remember
        lp_rows = (await db.execute(select(TrainingLearningPoint).where(
            TrainingLearningPoint.session_id == session.id))).scalars().all()
        for lp in lp_rows[:5]:
            await remember(db, str(agent.id), job.company_id,
                           MemoryType.SEMANTIC,
                           str(lp.content)[:280], importance=0.75)
        training_summary.append(f"岗前培训完成：{n_pts} 个学习点提取，"
                                f"{min(len(lp_rows), 5)} 条已写入长期记忆")

        job.status = FactoryJobStatus.TESTING
        job.agent_id = agent.id
        job.blueprint = bp
        await db.commit()
    except Exception as e:
        await db.rollback()  # 回滚会撤销 Agent —— 重新创建最小可用 Agent
        agent2 = Agent(
            company_id=job.company_id,
            name=bp.get("name") or "FactoryAgent",
            role=bp.get("role"),
            description=f"Factory创建(培训降级)。目标：{job.goal[:200]}",
            system_prompt=bp.get("system_prompt") or f"你是{bp.get('role', '助手')}。",
            status="DISABLED")
        db.add(agent2)
        await db.flush()
        job.status = FactoryJobStatus.TESTING
        job.agent_id = agent2.id
        job.blueprint = bp
        training_summary.append(f"⚠️ 岗前培训异常（已降级为未培训员工）：{e}")
        await db.commit()
        return {"job_id": job_id, "agent_id": agent2.id,
                "status": "TESTING", "agent_name": agent2.name,
                "training": training_summary}

    return {"job_id": job_id, "agent_id": agent.id,
            "status": "TESTING", "agent_name": agent.name,
            "training": training_summary}


@router.post("/jobs/{job_id}/test-result")
async def submit_test_result(
    job_id: str,
    passed: bool = Query(...),
    score: float | None = Query(None),
    notes: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交测试任务结果：通过 → ACTIVE；不通过 → FAILED。"""
    job = await db.get(AgentFactoryJob, job_id)
    if not job or job.status != FactoryJobStatus.TESTING:
        raise HTTPException(409, "只有 TESTING 状态可提交测试结果")
    agent = await db.get(Agent, job.agent_id) if job.agent_id else None

    if passed:
        job.status = FactoryJobStatus.ACTIVE
        job.test_result = {"passed": True, "score": score, "notes": notes}
        if agent:
            agent.status = "IDLE"
    else:
        job.status = FactoryJobStatus.FAILED
        job.error = notes or "测试未通过"
        job.test_result = {"passed": False, "score": score}
        if agent:
            agent.status = "DISABLED"
    await db.commit()
    return {"job_id": job_id, "status": job.status.value}


@router.get("/jobs")
async def list_jobs(
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = await db.execute(select(AgentFactoryJob).where(
        AgentFactoryJob.company_id == company_id).order_by(
        AgentFactoryJob.created_at.desc()))
    rows = q.scalars().all()
    return [{"id": j.id, "goal": j.goal[:80], "status": j.status.value,
             "agent_id": j.agent_id, "blueprint_name":
                 (j.blueprint or {}).get("name"),
             "created_at": j.created_at.isoformat()} for j in rows]


@router.get("/jobs/{job_id}/training-detail")
async def training_detail(job_id: str,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """培训验收数据：训练会话、学习点、提案、建议测试任务。"""
    from app.models.training import (TrainingSession, TrainingLearningPoint,
                                     TrainingProposal)
    job = await db.get(AgentFactoryJob, job_id)
    if not job:
        raise HTTPException(404)

    sessions = (await db.execute(select(TrainingSession).where(
        TrainingSession.agent_id == job.agent_id).order_by(
        TrainingSession.created_at))).scalars().all() if job.agent_id else []

    out_sessions = []
    for sess in sessions:
        lps = (await db.execute(select(TrainingLearningPoint).where(
            TrainingLearningPoint.session_id == sess.id))).scalars().all()
        prs = (await db.execute(select(TrainingProposal).where(
            TrainingProposal.session_id == sess.id))).scalars().all()
        out_sessions.append({
            "id": sess.id, "title": sess.title, "status": str(sess.status),
            "summary": sess.summary,
            "learning_points": [{"content": lp.content,
                                 "category": lp.category} for lp in lps],
            "proposals": [{"change_type": pr.change_type,
                           "payload": pr.payload} for pr in prs],
        })

    # 建议测试任务：从蓝图职责生成 3 条验收问题
    bp = job.blueprint or {}
    test_tasks = []
    for i, r_ in enumerate((bp.get("responsibilities") or [])[:3]):
        test_tasks.append(f"请以「{bp.get('role') or bp.get('name')}」的身份，"
                          f"说明你将如何完成这项职责：{r_}。给出具体步骤。")
    if not test_tasks:
        test_tasks = [f"请介绍你自己的岗位和能力，并演示如何处理「{job.goal[:50]}」相关的一项工作。"]

    return {"job_id": job_id, "goal": job.goal, "status": str(job.status),
            "agent_id": job.agent_id, "blueprint": bp,
            "sessions": out_sessions, "test_tasks": test_tasks}


class ReviewIn(BaseModel):
    approve: bool
    notes: str | None = None


@router.post("/jobs/{job_id}/review-training")
async def review_training(job_id: str, data: ReviewIn,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """老板审查培训结果：批准 → Agent 转正式（ACTIVE/IDLE）；驳回 → FAILED 重新培训。"""
    job = await db.get(AgentFactoryJob, job_id)
    if not job or job.status != FactoryJobStatus.TESTING:
        raise HTTPException(409, "只有 TESTING 状态可提交验收")
    agent = await db.get(Agent, job.agent_id) if job.agent_id else None

    if data.approve:
        job.status = FactoryJobStatus.ACTIVE
        job.test_result = {"approved": True, "notes": data.notes}
        if agent:
            agent.status = "IDLE"
    else:
        job.status = FactoryJobStatus.FAILED
        job.error = data.notes or "老板驳回，需重新培训"
        job.test_result = {"approved": False, "notes": data.notes}
        if agent:
            agent.status = "DISABLED"
        # 清除该 Agent 培训期间写入的记忆，避免错误知识残留
        try:
            await db.execute(text(
                "DELETE FROM agent_memories WHERE agent_id = :aid").bindparams(
                aid=job.agent_id))
        except Exception as mem_err:
            # 清除失败会让"被驳回员工"带着错误知识残留——必须可见
            logger.error("驳回时清除培训记忆失败 job=%s agent=%s: %s",
                         job_id, job.agent_id, mem_err, exc_info=True)
    await db.commit()
    return {"job_id": job_id, "status": job.status.value}


class AskIn(BaseModel):
    question: str


@router.post("/jobs/{job_id}/ask")
async def exam_ask(job_id: str, data: AskIn,
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """验收考试：让被培训的 Agent 真实回答问题（调用其记忆/技能/知识库）。"""
    from app.kb_engine import cost_optimized_rag
    from app.model_gateway import ModelGateway
    job = await db.get(AgentFactoryJob, job_id)
    if not job or not job.agent_id:
        raise HTTPException(404)
    agent = await db.get(Agent, job.agent_id)
    if not agent:
        raise HTTPException(404, "agent missing")

    messages = [{"role": "system", "content":
        f"你是「{agent.name}」，{agent.role or '数字员工'}。这是你的岗前验收考试："
        f"请以专业身份完整回答老板的提问，展示你在岗前培训中掌握的知识和方法论。"
        f"回答要具体、有步骤、可执行（300字以内）。"}]
    # 该 Agent 的长期记忆（培训写入的学习点）
    try:
        from app.memory_engine import recall
        mems = await recall(db, str(agent.id), query=data.question[:200])
        if mems:
            lines = "\n".join(f"- {m.get('content', '')[:150]}" for m in mems[:6])
            messages.append({"role": "system",
                             "content": f"你在岗前培训中学到的要点：\n{lines}"})
    except Exception as mem_err:
        logger.warning("验收考试记忆召回失败 agent=%s: %s", agent.id, mem_err,
                       exc_info=True)
    # 知识库
    try:
        rag = await cost_optimized_rag(db, agent.company_id, str(agent.id),
                                       data.question[:300], agent=agent)
        if rag.get("context_text"):
            messages.append({"role": "system",
                             "content": f"相关知识库：\n{rag['context_text'][:1500]}"})
    except Exception as kb_err:
        logger.warning("验收考试知识库检索失败 agent=%s: %s", agent.id, kb_err,
                       exc_info=True)
    messages.append({"role": "user", "content": data.question})

    gw = ModelGateway(db)
    llm = await gw.chat_with_agent_models_by_agent(agent, messages)
    return {"answer": (llm.get("content") or "(无回答)").strip()}
