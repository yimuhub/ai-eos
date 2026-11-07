"""Project Context API (P2-08)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.context_builder import (build_project_context, render_context_prompt,
                                 save_snapshot)
from app.models.project import Project, ProjectContextSnapshot
from app.models.user import User

router = APIRouter(prefix="/projects/{project_id}/context", tags=["context"])


async def _project_owned(db: AsyncSession, user_id: str, project_id: str) -> Project:
    """修复：此前本模块 5 个端点零归属校验——任何登录用户知道 project_id
    即可读他人公司项目上下文/写入快照。统一走 projects 的归属断言。"""
    from app.api.routes.projects import _project_owned as _assert_owned
    return await _assert_owned(db, user_id, project_id)


@router.get("")
async def get_context(
    project_id: str,
    kb_query: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取项目实时上下文（结构化）。"""
    await _project_owned(db, user.id, project_id)
    ctx = await build_project_context(db, project_id, kb_query=kb_query)
    if not ctx:
        raise HTTPException(404, "Project not found")
    return ctx


@router.get("/prompt")
async def get_context_prompt(
    project_id: str,
    kb_query: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取渲染后的上下文文本（可直接注入 Agent prompt）。"""
    await _project_owned(db, user.id, project_id)
    ctx = await build_project_context(db, project_id, kb_query=kb_query)
    if not ctx:
        raise HTTPException(404, "Project not found")
    return {"prompt": render_context_prompt(ctx)}


class SnapshotIn(BaseModel):
    summary: str | None = None


@router.post("/snapshots")
async def create_snapshot(
    project_id: str,
    data: SnapshotIn | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存当前项目上下文快照（版本自增）。"""
    await _project_owned(db, user.id, project_id)
    snap = await save_snapshot(db, project_id)
    if data and data.summary:
        snap.summary = data.summary
        await db.commit()
    return {"id": snap.id, "version": snap.version, "summary": snap.summary}


@router.get("/snapshots")
async def list_snapshots(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _project_owned(db, user.id, project_id)
    q = await db.execute(select(ProjectContextSnapshot).where(
        ProjectContextSnapshot.project_id == project_id).order_by(
        ProjectContextSnapshot.version.desc()))
    snaps = q.scalars().all()
    return [{"id": s.id, "version": s.version, "summary": s.summary,
             "created_at": s.created_at.isoformat()} for s in snaps]


@router.get("/snapshots/{snapshot_id}")
async def get_snapshot(
    project_id: str,
    snapshot_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _project_owned(db, user.id, project_id)
    snap = await db.get(ProjectContextSnapshot, snapshot_id)
    if not snap or snap.project_id != project_id:
        raise HTTPException(404, "Snapshot not found")
    return {"id": snap.id, "version": snap.version, "summary": snap.summary,
            "context_data": snap.context_data}
