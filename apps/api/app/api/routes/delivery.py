"""交付中心 API (P4)。

以 Agent 专属交付文件夹为核心：
- /by-agent: 按 Agent 分组的文件视图
- /by-project: 按项目分组的文件视图
- /download/{asset_id}: 文件下载
"""
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent
from app.models.asset import Asset
from app.models.company import Company
from app.models.project import Project
from app.models.user import User

router = APIRouter(prefix="/delivery", tags=["delivery"])


async def _company_of(db: AsyncSession, user_id: str, company_id: str) -> Company:
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user_id:
        raise HTTPException(404, "Company not found")
    return c


def _file_info(a: Asset) -> dict:
    p = Path(a.path) if a.path else None
    return {
        "id": a.id,
        "name": a.name,
        "task_id": a.task_id,
        "project_id": a.project_id,
        "agent_id": a.agent_id,
        "size": a.size,
        "mime_type": a.mime_type or mimetypes.guess_type(a.name)[0],
        "exists": bool(p and p.exists()),
        "created_at": str(a.created_at),
    }


@router.get("/by-agent")
async def by_agent(company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """按 Agent 文件夹分组：每个 Agent 一个"文件夹"，含其全部产出。"""
    await _company_of(db, user.id, company_id)
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        Asset.company_id == company_id,
        Asset.agent_id.is_not(None)).order_by(Asset.created_at.desc())
    )).scalars().all()

    by_agent: dict[str, list] = {}
    for a in assets:
        by_agent.setdefault(a.agent_id, []).append(_file_info(a))

    folders = []
    for ag in agents:
        files = by_agent.get(str(ag.id), [])
        if not files:
            continue
        folders.append({
            "agent_id": str(ag.id), "name": ag.name, "role": ag.role,
            "file_count": len(files),
            "total_size": sum(f["size"] for f in files),
            "latest": files[0]["created_at"] if files else None,
            "files": files,
        })
    # 有产出但 Agent 已删除的孤儿文件夹
    known = {str(ag.id) for ag in agents}
    for aid, files in by_agent.items():
        if aid not in known:
            folders.append({"agent_id": aid, "name": f"(已离职员工 {aid[:8]})",
                            "role": None, "file_count": len(files),
                            "total_size": sum(f["size"] for f in files),
                            "latest": files[0]["created_at"], "files": files})
    folders.sort(key=lambda x: -x["file_count"])
    return {"folders": folders}


@router.get("/by-project")
async def by_project(company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """按项目分组：项目 → 任务 → 产出文件。含未关联项目的散件。"""
    await _company_of(db, user.id, company_id)
    projects = (await db.execute(select(Project).where(
        Project.company_id == company_id))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        Asset.company_id == company_id).order_by(Asset.created_at.desc())
    )).scalars().all()

    groups: list[dict] = []
    for pj in projects:
        files = [_file_info(a) for a in assets if a.project_id == pj.id]
        if files:
            groups.append({"key": str(pj.id), "name": pj.name,
                           "is_project": True, "file_count": len(files),
                           "total_size": sum(f["size"] for f in files),
                           "files": files})
    loose = [_file_info(a) for a in assets if not a.project_id]
    if loose:
        groups.append({"key": "__loose__", "name": "未关联项目",
                       "is_project": False, "file_count": len(loose),
                       "total_size": sum(f["size"] for f in loose),
                       "files": loose})
    return {"groups": groups}


@router.get("/download/{asset_id}")
async def download(asset_id: str, db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    a = await db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404)
    c = await db.get(Company, a.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404)
    p = Path(a.path or "")
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(p, filename=a.name)


# ---------------- 聊天页用：单个 Agent 的交付文件夹 ----------------

@router.get("/agents/{aid}/folder")
async def agent_folder(aid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    from app.api.routes.agent_chat import _owned

    a = await _owned(db, user.id, aid)
    assets = (await db.execute(select(Asset).where(
        Asset.agent_id == str(a.id)).order_by(Asset.created_at.desc())
    )).scalars().all()
    return {"agent": {"id": str(a.id), "name": a.name},
            "files": [_file_info(x) for x in assets]}


@router.get("/by-task")
async def by_task(company_id: str = Query(...),
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """按任务分组：任务 → 产出文件。"""
    from app.models.task import Task
    await _company_of(db, user.id, company_id)
    tasks = (await db.execute(select(Task).where(
        Task.company_id == company_id))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        Asset.company_id == company_id,
        Asset.task_id.is_not(None)).order_by(Asset.created_at.desc())
    )).scalars().all()

    groups = []
    for t in tasks:
        files = [_file_info(a) for a in assets if a.task_id == t.id]
        if not files:
            continue
        groups.append({"key": str(t.id), "name": t.title or "(未命名任务)",
                       "project_id": str(t.project_id) if t.project_id else None,
                       "file_count": len(files),
                       "total_size": sum(f["size"] for f in files),
                       "files": files})
    return {"groups": groups}


@router.get("/by-scope")
async def by_scope(company_id: str = Query(...),
                   scope: str = Query(...), scope_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """精确查某项目或某任务的交付文件（不含其他）。"""
    await _company_of(db, user.id, company_id)
    if scope not in ("project", "task"):
        raise HTTPException(400, "scope must be project or task")
    col = Asset.project_id if scope == "project" else Asset.task_id
    assets = (await db.execute(select(Asset).where(
        Asset.company_id == company_id,
        col == scope_id).order_by(Asset.created_at.desc()))).scalars().all()
    return {"files": [_file_info(a) for a in assets]}
