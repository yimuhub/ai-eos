"""员工离职继承：把离职 Agent 的技能、成果、记忆转移给接替者。"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.agents import _verify_agent_access
from app.db.session import get_db
from app.models.agent import Agent, AgentModelConfig
from app.models.memory import AgentMemory
from app.services.event_system import log_event

router = APIRouter(prefix="/agents/offboarding", tags=["offboarding"])


class OffboardIn(BaseModel):
    agent_ids: list[str]              # 离职员工
    successor_id: str                 # 继承人（唯一必填）
    transfer_skills: bool = True      # 技能：继承人不具备的技能补给他
    transfer_assets: bool = True      # 成果/资料：所有权转移
    transfer_memories: bool = True    # 记忆：复制给继承人（并标注来源）
    reassign_open_tasks: bool = True  # 未完成任务改派给继承人


@router.post("/preview")
async def preview_offboarding(data: OffboardIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """离职预览：统计每位离职员工将转移什么，不执行任何写操作。"""
    succ = await db.get(Agent, data.successor_id)
    if not succ:
        raise HTTPException(404, "继承人不存在")
    await _verify_agent_access(db, user.id, succ)

    details = []
    for aid in data.agent_ids:
        a = await db.get(Agent, aid)
        if not a or a.id == data.successor_id:
            continue
        await _verify_agent_access(db, user.id, a)

        skills = (await db.execute(text(
            "SELECT count(*) FROM agent_skills WHERE agent_id = CAST(:aid AS varchar)"),
            {"aid": aid})).scalar() or 0
        assets = (await db.execute(text(
            "SELECT count(*) FROM assets WHERE agent_id = CAST(:aid AS varchar)"),
            {"aid": aid})).scalar() or 0
        memories = (await db.execute(text(
            "SELECT count(*) FROM agent_memories WHERE agent_id = CAST(:aid AS varchar)"),
            {"aid": aid})).scalar() or 0
        open_tasks = (await db.execute(text(
            "SELECT count(*) FROM tasks WHERE assigned_agent_id = CAST(:aid AS varchar) "
            "AND status IN ('TODO','PLANNING','RUNNING','WAITING','PAUSED','REVIEW')"),
            {"aid": aid})).scalar() or 0

        details.append({
            "agent_id": aid, "name": a.name, "role": a.role,
            "skills": skills, "assets": assets,
            "memories": memories, "open_tasks": open_tasks,
        })
    return {"successor": {"id": succ.id, "name": succ.name}, "details": details}


@router.post("/execute")
async def execute_offboarding(data: OffboardIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """执行离职继承 + 删除离职员工。"""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    succ = await db.get(Agent, data.successor_id)
    if not succ:
        raise HTTPException(404, "继承人不存在")
    await _verify_agent_access(db, user.id, succ)
    # 离职交接会真实改写归属（技能/成果/记忆转移到继承人）并删除原员工，
    # 不可撤销：声明为最高危险级别。
    from app.core.permissions import Capability, ensure_capability
    await ensure_capability(db, user.id, succ.company_id, Capability.DESTRUCTIVE)
    sid = str(succ.id)
    succ_name = succ.name  # 提前取值：避免 savepoint 回滚后惰性加载

    user_id = user.id  # 提前缓存，防 rollback 过期后触发同步懒加载

    # 预校验权限：一次查出本用户公司下全部 agent id
    from app.models.company import Company as CompanyM
    q = await db.execute(
        select(Agent.id).join(CompanyM, CompanyM.id == Agent.company_id)
        .where(CompanyM.owner_id == user_id))
    owned_ids = set(q.scalars().all())

    offboarded, failed = [], []

    for aid in data.agent_ids:
        if aid == sid:
            continue
        if aid not in owned_ids:
            failed.append({"id": aid, "reason": "不存在或无权限"})
            continue
        a = (await db.execute(select(Agent).where(Agent.id == aid))).scalar_one_or_none()
        if not a:
            failed.append({"id": aid, "reason": "不存在"})
            continue
        aname = a.name  # 提前取值：回滚/删除后 ORM 对象过期
        try:
            async with db.begin_nested():
                # 1) 技能继承：继承人不具备的技能补给他
                if data.transfer_skills:
                    await db.execute(text(
                        "INSERT INTO agent_skills (agent_id, skill_id) "
                        "SELECT CAST(:sid AS varchar), skill_id FROM agent_skills src "
                        "WHERE src.agent_id = CAST(:aid AS varchar) "
                        "AND NOT EXISTS (SELECT 1 FROM agent_skills mine "
                        "  WHERE mine.agent_id = CAST(:sid AS varchar) AND mine.skill_id = src.skill_id)"),
                        {"aid": aid, "sid": sid})

                # 2) 成果所有权转移
                if data.transfer_assets:
                    await db.execute(text(
                        "UPDATE assets SET agent_id = CAST(:sid AS varchar) WHERE agent_id = CAST(:aid AS varchar)"),
                        {"aid": aid, "sid": sid})

                # 3) 记忆复制（标注传承来源）—— id 在 Python 侧生成，跨方言可用
                if data.transfer_memories:
                    src_memories = (await db.execute(select(AgentMemory).where(
                        AgentMemory.agent_id == aid))).scalars().all()
                    for m in src_memories:
                        db.add(AgentMemory(
                            id=str(uuid.uuid4()),
                            agent_id=sid,
                            company_id=m.company_id,
                            memory_type=m.memory_type,
                            content=f"[来自「{aname}」的传承] {m.content}",
                            importance=m.importance,
                            keywords=m.keywords,
                            context_ref=m.context_ref,
                            expires_at=m.expires_at,
                        ))
                    await db.flush()

                # 4) 任务全部改派给继承人
                if data.reassign_open_tasks:
                    await db.execute(text(
                        "UPDATE tasks SET assigned_agent_id = CAST(:sid AS varchar) WHERE assigned_agent_id = CAST(:aid AS varchar)"),
                        {"aid": aid, "sid": sid})
                else:
                    # 不改派也要清引用，否则删不掉
                    await db.execute(text(
                        "UPDATE tasks SET assigned_agent_id = NULL WHERE assigned_agent_id = CAST(:aid AS varchar)"),
                        {"aid": aid, "sid": sid})

                # 5) 下属改挂到继承人
                await db.execute(text(
                    "UPDATE agents SET manager_agent_id = CAST(:sid AS varchar) WHERE manager_agent_id = CAST(:aid AS varchar)"),
                    {"aid": aid, "sid": sid})

                # 6) 清理剩余引用后删除本体
                await db.execute(text(
                    "DELETE FROM execution_checkpoints WHERE execution_id IN "
                    "(SELECT id FROM agent_executions WHERE agent_id = CAST(:aid AS varchar))"),
                    {"aid": aid})
                for stmt in (
                    "DELETE FROM agent_model_configs WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM meeting_participants WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_skills WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_memories WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_executions WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_capability_history WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_growth_events WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_learning_records WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_profiles WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM career_proposals WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_career_records WHERE agent_id = CAST(:aid AS varchar)",
                    "DELETE FROM agent_reviews WHERE agent_id = CAST(:aid AS varchar)",
                    "UPDATE meetings SET coordinator_agent_id = NULL, current_speaker_id = NULL "
                    "WHERE coordinator_agent_id = CAST(:aid AS varchar) OR current_speaker_id = CAST(:aid AS varchar)",
                    "UPDATE meeting_action_items SET assignee_agent_id = NULL WHERE assignee_agent_id = CAST(:aid AS varchar)",
                    "UPDATE meeting_decisions SET proposed_by_agent_id = NULL WHERE proposed_by_agent_id = CAST(:aid AS varchar)",
                    "UPDATE meeting_messages SET speaker_agent_id = NULL WHERE speaker_agent_id = CAST(:aid AS varchar)",
                    "UPDATE projects SET manager_agent_id = NULL WHERE manager_agent_id = CAST(:aid AS varchar)",
                    "UPDATE agent_factory_jobs SET agent_id = NULL WHERE agent_id = CAST(:aid AS varchar)",
                ):
                    await db.execute(text(stmt), {"aid": aid})
                await db.delete(a)
                # 显式 flush：本条失败在 savepoint 内回滚，不影响其他离职交接
                await db.flush()
            offboarded.append({"id": aid, "name": aname})
        except (IntegrityError, DBAPIError) as e:
            # savepoint 已回滚；不要 rollback 外层事务（会撤销此前成功的交接）
            failed.append({"id": aid,
                           "reason": f"约束冲突: {str(getattr(e, 'orig', e))[:100]}"})
            db.expire_all()
        except HTTPException:
            failed.append({"id": aid, "reason": "无权限"})

    if offboarded:
        await log_event(db, "agent.offboarded", "agent", None, "user", user.id,
                        {"count": len(offboarded),
                         "successor": succ_name,
                         "names": [o["name"] for o in offboarded],
                         "transfer": {"skills": data.transfer_skills,
                                      "assets": data.transfer_assets,
                                      "memories": data.transfer_memories,
                                      "tasks": data.reassign_open_tasks}})
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        failed.extend({"id": x["id"], "reason": f"提交失败: {str(e)[:80]}"}
                      for x in offboarded)
        offboarded = []

    return {"offboarded": offboarded, "offboarded_count": len(offboarded),
            "failed": failed}
