"""公司管理：列表统计、单个/批量删除（级联清理全部下属数据）。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Company, Department
from app.models.user import User

router = APIRouter(prefix="/company-manage", tags=["company-manage"])

# 删除公司时的级联表（company_id 外键的表）
# (SQL)——顺序严格：先删最深层子表，最后 agents/departments
CASCADE_SQLS = [
    "DELETE FROM execution_checkpoints WHERE execution_id IN (SELECT id FROM agent_executions WHERE task_id IN (SELECT id FROM tasks WHERE company_id=:cid))",
    "DELETE FROM agent_model_configs WHERE agent_id IN (SELECT id FROM agents WHERE company_id=:cid)",
    "DELETE FROM meeting_messages WHERE meeting_id IN (SELECT id FROM meetings WHERE company_id=:cid)",
    "DELETE FROM meeting_participants WHERE meeting_id IN (SELECT id FROM meetings WHERE company_id=:cid)",
    "DELETE FROM meeting_action_items WHERE meeting_id IN (SELECT id FROM meetings WHERE company_id=:cid)",
    "DELETE FROM meeting_decisions WHERE meeting_id IN (SELECT id FROM meetings WHERE company_id=:cid)",
    "DELETE FROM war_room_tasks WHERE session_id IN (SELECT id FROM war_room_sessions WHERE company_id=:cid)",
    "DELETE FROM group_chat_messages WHERE company_id=:cid",
    "DELETE FROM agent_memories WHERE company_id=:cid",
    "DELETE FROM agent_skills WHERE agent_id IN (SELECT id FROM agents WHERE company_id=:cid)",
    "DELETE FROM kb_chunks WHERE document_id IN (SELECT id FROM kb_documents WHERE company_id=:cid)",
    "DELETE FROM kb_documents WHERE company_id=:cid",
    "DELETE FROM assets WHERE company_id=:cid",
    "DELETE FROM task_events WHERE task_id IN (SELECT id FROM tasks WHERE company_id=:cid)",
    "DELETE FROM agent_executions WHERE task_id IN (SELECT id FROM tasks WHERE company_id=:cid)",
    "DELETE FROM milestones WHERE project_id IN (SELECT id FROM projects WHERE company_id=:cid)",
    "DELETE FROM project_members WHERE project_id IN (SELECT id FROM projects WHERE company_id=:cid)",
    "DELETE FROM project_context_snapshots WHERE project_id IN (SELECT id FROM projects WHERE company_id=:cid)",
    "DELETE FROM interventions WHERE task_id IN (SELECT id FROM tasks WHERE company_id=:cid)",
    "DELETE FROM tasks WHERE company_id=:cid",
    "DELETE FROM projects WHERE company_id=:cid",
    "DELETE FROM meetings WHERE company_id=:cid",
    "DELETE FROM war_room_sessions WHERE company_id=:cid",
    "DELETE FROM agents WHERE company_id=:cid",
    "DELETE FROM departments WHERE company_id=:cid",
]


@router.get("/list")
async def list_companies(db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """公司管理列表：基本信息 + Agent/部门/项目/任务统计 + 负责人。"""
    rows = (await db.execute(select(Company).where(
        Company.owner_id == user.id).order_by(Company.created_at))).scalars().all()
    out = []
    for c in rows:
        cid = str(c.id)

        async def _count(table: str) -> int:
            r = await db.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = :cid"), {"cid": cid})
            return r.scalar() or 0

        agents_n = await _count("agents")
        leader_row = (await db.execute(text(
            "SELECT name FROM agents WHERE company_id=:cid AND manager_agent_id IS NULL "
            "ORDER BY created_at LIMIT 1"), {"cid": cid})).scalar()
        dept_names = (await db.execute(text(
            "SELECT name FROM departments WHERE company_id=:cid ORDER BY sort_order"),
            {"cid": cid})).scalars().all()
        out.append({
            "id": cid, "name": c.name, "description": c.description,
            "created_at": str(c.created_at),
            "stats": {
                "agents": agents_n,
                "departments": await _count("departments"),
                "projects": await _count("projects"),
                "tasks": await _count("tasks"),
            },
            "leader_name": leader_row,
            "department_names": list(dept_names),
        })
    return {"companies": out}


class DeleteIn(BaseModel):
    company_ids: list[str]


@router.post("/batch-delete")
async def batch_delete(data: DeleteIn, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """批量删除公司（级联清除全部下属数据，savepoint 隔离单个失败）。"""
    deleted, failed = 0, []
    # 用独立引擎直连执行级联 DELETE（避开 ORM autoflush/greenlet 问题）
    from app.core.config import settings
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            # 先校验归属
            valid_ids = []
            for cid in data.company_ids:
                c = await db.get(Company, cid)
                if not c or c.owner_id != user.id:
                    failed.append({"id": cid, "reason": "not found"})
                    continue
                # 批量删除公司会级联清除项目/员工/成果/日志，是最重的一档。
                # 走统一权限层而不是就地比较 owner_id：将来引入"管理员"角色时，
                # 这里不需要任何改动。
                from app.core.permissions import Capability, ensure_capability
                await ensure_capability(db, user.id, cid, Capability.DESTRUCTIVE)
                valid_ids.append(cid)
            async with engine.begin() as trans:
                for cid in valid_ids:
                    try:
                        # 先查 id 是否存在防止空 delete
                        r2 = await trans.execute(
                            text("SELECT id FROM companies WHERE id=:cid"), {"cid": cid})
                        if not r2.scalar():
                            continue
                        for sql in CASCADE_SQLS:
                            await trans.execute(text(sql), {"cid": cid})
                        await trans.execute(
                            text("DELETE FROM companies WHERE id=:cid"), {"cid": cid})
                        deleted += 1
                    except Exception as e2:
                        failed.append({"id": cid, "reason": str(e2)[:120]})
    except Exception as e:
        failed.append({"id": "*", "reason": str(e)[:150]})
    finally:
        await engine.dispose()
    return {"deleted_count": deleted, "failed": failed}
