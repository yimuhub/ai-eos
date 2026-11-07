"""交付中心 API (P4)。

以 Agent 专属交付文件夹为核心：
- /by-agent: 按 Agent 分组的文件视图
- /by-project: 按项目分组的文件视图
- /download/{asset_id}: 文件下载
"""
import io
import mimetypes
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
# Asset.path 存的是相对 storage 根的路径（如 artifacts/ab12_x.md），
# 直接 Path() 会按进程 CWD 解析，换目录/换机器全部 404。
from app.api.routes.assets import _resolve_asset_path
from app.core.scope import agent_read_scope, build_ctx
from app.core.timeutil import iso_utc
from app.models.agent import Agent
from app.models.asset import Asset
from app.models.company import Company
from app.models.project import Project
from app.models.user import User
from app.schemas.responses import (DeliveryAgentFolderDetailOut,
                                   DeliveryByAgentOut, DeliveryByProjectOut,
                                   DeliveryByTaskOut, DeliveryFilesOut)

router = APIRouter(prefix="/delivery", tags=["delivery"])


async def _company_of(db: AsyncSession, user_id: str, company_id: str) -> Company:
    """交付中心的所有列表都以"能进这家公司"为前置条件（VIEW）。"""
    return await assert_company_owned(db, user_id, company_id)


def _asset_scope(ctx):
    """成果可见条件：owner/admin 全公司；其余只看自己上传的
    ∪ 自己可见 Agent 的产出（与 /assets 列表同口径）。"""
    base = Asset.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    visible_agents = select(Agent.id).where(agent_read_scope(ctx))
    return and_(base, or_(
        and_(Asset.creator_type == "user", Asset.created_by == ctx.user_id),
        Asset.agent_id.in_(visible_agents)))


def _file_info(a: Asset) -> dict:
    p = _resolve_asset_path(a.path) if a.path else None
    return {
        "id": a.id,
        "name": a.name,
        "task_id": a.task_id,
        "project_id": a.project_id,
        "agent_id": a.agent_id,
        "size": a.size,
        "mime_type": a.mime_type or mimetypes.guess_type(a.name)[0],
        "exists": bool(p and p.exists()),
        "created_at": iso_utc(a.created_at),
    }


@router.get("/by-agent", response_model=DeliveryByAgentOut)
async def by_agent(company_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """按 Agent 文件夹分组：每个 Agent 一个"文件夹"，含其全部产出。"""
    ctx = await build_ctx(db, user, company_id)
    agents = (await db.execute(select(Agent).where(
        agent_read_scope(ctx)))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        _asset_scope(ctx),
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


@router.get("/by-project", response_model=DeliveryByProjectOut)
async def by_project(company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """按项目分组：项目 → 任务 → 产出文件。含未关联项目的散件。"""
    ctx = await build_ctx(db, user, company_id)
    projects = (await db.execute(select(Project).where(
        Project.company_id == company_id))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        _asset_scope(ctx)).order_by(Asset.created_at.desc())
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
    ctx = await build_ctx(db, user, a.company_id)
    found = (await db.execute(select(Asset.id).where(
        Asset.id == asset_id, _asset_scope(ctx)))).first()
    if found is None:
        raise HTTPException(404)
    p = _resolve_asset_path(a.path or "")
    if not p.is_file():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(p, filename=a.name)


@router.get("/zip")
async def zip_delivery(company_id: str = Query(...),
                       agent_id: str | None = Query(None),
                       project_id: str | None = Query(None),
                       task_id: str | None = Query(None),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """把一个文件夹下的产出打成 zip：项目 20 个文件不必点 20 次。

    目标三选一；一个都不传直接 400，避免无意间把全公司成果打包拖走。
    """
    if not (agent_id or project_id or task_id):
        raise HTTPException(400, "agent_id / project_id / task_id 至少要给一个")
    ctx = await build_ctx(db, user, company_id)
    conds = [_asset_scope(ctx)]
    if agent_id:
        conds.append(Asset.agent_id == agent_id)
    if project_id:
        conds.append(Asset.project_id == project_id)
    if task_id:
        conds.append(Asset.task_id == task_id)
    assets = (await db.execute(select(Asset).where(*conds))).scalars().all()

    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for a in assets:
            path = _resolve_asset_path(a.path or "")
            if not path.is_file():
                continue
            # 文件名可能来自用户上传：只取 basename，防止 zip 内出现 ../ 路径
            name = Path(a.name or path.name).name or "unnamed"
            stem, suffix = Path(name).stem, Path(name).suffix
            candidate, n = name, 1
            while candidate in used:
                candidate = f"{stem}({n}){suffix}"
                n += 1
            used.add(candidate)
            zf.write(path, arcname=candidate)

    if not used:
        raise HTTPException(404, "这个文件夹里没有可下载的文件")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="aieos-delivery.zip"',
            "Cache-Control": "no-store",
        },
    )


# ---------------- 聊天页用：单个 Agent 的交付文件夹 ----------------

@router.get("/agents/{aid}/folder",
            response_model=DeliveryAgentFolderDetailOut)
async def agent_folder(aid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    from app.api.deps import assert_agent_owned

    a = await assert_agent_owned(db, user.id, aid)
    assets = (await db.execute(select(Asset).where(
        Asset.agent_id == str(a.id)).order_by(Asset.created_at.desc())
    )).scalars().all()
    return {"agent": {"id": str(a.id), "name": a.name},
            "files": [_file_info(x) for x in assets]}


@router.get("/by-task", response_model=DeliveryByTaskOut)
async def by_task(company_id: str = Query(...),
                  db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """按任务分组：任务 → 产出文件。"""
    from app.models.task import Task
    ctx = await build_ctx(db, user, company_id)
    tasks = (await db.execute(select(Task).where(
        Task.company_id == company_id))).scalars().all()
    assets = (await db.execute(select(Asset).where(
        _asset_scope(ctx),
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


@router.get("/by-scope", response_model=DeliveryFilesOut)
async def by_scope(company_id: str = Query(...),
                   scope: str = Query(...), scope_id: str = Query(...),
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """精确查某项目或某任务的交付文件（不含其他）。"""
    ctx = await build_ctx(db, user, company_id)
    if scope not in ("project", "task"):
        raise HTTPException(400, "scope must be project or task")
    col = Asset.project_id if scope == "project" else Asset.task_id
    assets = (await db.execute(select(Asset).where(
        _asset_scope(ctx),
        col == scope_id).order_by(Asset.created_at.desc()))).scalars().all()
    return {"files": [_file_info(a) for a in assets]}
