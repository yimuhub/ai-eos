"""Asset Center: upload / list / download; runtime artifacts auto-saved."""
import asyncio
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.config import settings
from app.core.permissions import Capability
from app.core.scope import agent_read_scope, build_ctx
from app.core.uploads import (ASSET_ALLOWED_EXTENSIONS, ensure_within,
                              generate_stored_name, safe_original_name,
                              stream_upload, validate_extension)
from app.db.session import get_db
from app.models.agent import Agent
from app.models.asset import Asset, AssetType
from app.services.event_system import log_event

router = APIRouter(prefix="/assets", tags=["assets"])

EXT_TYPE = {".md": AssetType.DOCUMENT, ".txt": AssetType.DOCUMENT,
            ".pdf": AssetType.DOCUMENT, ".docx": AssetType.DOCUMENT,
            ".png": AssetType.IMAGE, ".jpg": AssetType.IMAGE, ".jpeg": AssetType.IMAGE,
            ".gif": AssetType.IMAGE, ".webp": AssetType.IMAGE, ".bmp": AssetType.IMAGE,
            ".mp3": AssetType.OTHER, ".wav": AssetType.OTHER, ".m4a": AssetType.OTHER,
            ".ogg": AssetType.OTHER, ".mp4": AssetType.OTHER, ".mov": AssetType.OTHER,
            ".webm": AssetType.OTHER,
            ".py": AssetType.CODE, ".js": AssetType.CODE, ".ts": AssetType.CODE,
            ".json": AssetType.DATA, ".csv": AssetType.DATA}
REPORT_HINTS = ("report", "报告")


def storage_root() -> Path:
    p = Path(settings.storage_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / settings.storage_dir
    (p / "uploads").mkdir(parents=True, exist_ok=True)
    (p / "artifacts").mkdir(parents=True, exist_ok=True)
    return p


def _resolve_asset_path(stored: str) -> Path:
    """把 DB 里存的 path 还原为文件系统路径，兼容新旧两种格式：
    - 新数据：相对路径（如 artifacts/ab12_xxx.md）→ 拼 storage 根；
    - 旧数据：绝对路径且文件还在 → 直接用；
    - 旧数据：绝对路径但文件已不在（换机器）→ 尽力在 storage 下按文件名找回。
    """
    p = Path(stored)
    if p.is_absolute() and p.is_file():
        return p
    if p.is_absolute():
        for sub in ("artifacts", "uploads"):
            cand = storage_root() / sub / p.name
            if cand.is_file():
                return cand
        return p  # 找不到，上层报 404
    return storage_root() / stored


async def save_artifact(db: AsyncSession, company_id: str, name: str, content: str,
                        task_id: str | None = None, agent_id: str | None = None) -> Asset:
    """Called by Agent Runtime to auto-save text results into the Asset Center."""
    root = storage_root().resolve()
    # 名字可能来自 Agent 产出（间接来自 LLM），必须当不可信输入：
    # 落盘只用 basename，保证文件一定在 artifacts/ 内（审查 S9 同一根因）。
    fname = f"{uuid.uuid4().hex[:8]}_{safe_original_name(name)}"
    fpath = ensure_within(root, root / "artifacts" / fname)
    # 写盘是同步阻塞调用：放在事件循环里会卡住同进程的所有请求。
    await asyncio.to_thread(fpath.write_text, content, encoding="utf-8")
    ext = fpath.suffix.lower()
    atype = AssetType.REPORT if any(h in name.lower() for h in REPORT_HINTS) \
        else EXT_TYPE.get(ext, AssetType.OTHER)
    # 存相对路径：换机器/换目录后仍能定位（读取时拼 storage 根）
    rel_path = str(fpath.relative_to(root))
    asset = Asset(company_id=company_id, name=name, type=atype, path=rel_path,
                  mime_type=mimetypes.guess_type(name)[0], size=len(content.encode()),
                  created_by=agent_id or "", creator_type="agent" if agent_id else "user",
                  task_id=task_id, agent_id=agent_id)
    db.add(asset)
    await log_event(db, "asset.created", "asset", None, "agent", agent_id, {"name": name})
    return asset


def _asset_scope(ctx):
    """成果行的可见条件：owner/admin 看全公司；其余看自己上传的
    ∪ 自己可见 Agent 产出的成果。成果里可能有报告全文，不能全员可见。
    """
    base = Asset.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    visible_agents = select(Agent.id).where(agent_read_scope(ctx))
    return and_(base, or_(
        and_(Asset.creator_type == "user", Asset.created_by == ctx.user_id),
        Asset.agent_id.in_(visible_agents)))


async def _visible_asset(db: AsyncSession, user, aid: str) -> Asset:
    """单行校验与列表用同一条件：看不到即 404（不泄露存在性）。"""
    a = await db.get(Asset, aid)
    if not a:
        raise HTTPException(404, "Asset not found")
    ctx = await build_ctx(db, user, a.company_id)
    found = (await db.execute(
        select(Asset.id).where(Asset.id == aid, _asset_scope(ctx)))).first()
    if found is None:
        raise HTTPException(404, "Asset not found")
    return a


@router.get("")
async def list_assets(company_id: str, type: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    ctx = await build_ctx(db, user, company_id)
    q = select(Asset).where(_asset_scope(ctx))
    if type:
        q = q.where(Asset.type == AssetType(type))
    rows = (await db.execute(q.order_by(Asset.created_at.desc()))).scalars().all()
    out = []
    for a in rows:
        d = {c2.key: getattr(a, c2.key) for c2 in a.__table__.columns}
        d["type"] = a.type.value
        out.append(d)
    return out


@router.post("/upload", status_code=201)
async def upload(company_id: str, file: UploadFile,
                 db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    # 上传成果 = 日常写操作：成员（OPERATE）可以，只读不行
    await assert_company_capability(db, user.id, company_id, Capability.OPERATE)
    root = storage_root().resolve()
    # 用户文件名只作元数据：落盘名由服务端生成（uuid + 白名单扩展名），
    # 扩展名不在白名单直接 400，超限由 stream_upload 流式拦截。
    original_name = safe_original_name(file.filename)
    ext = validate_extension(original_name, ASSET_ALLOWED_EXTENSIONS)
    uploads_dir = ensure_within(root, root / "uploads")
    fpath, size = await stream_upload(file, uploads_dir,
                                      generate_stored_name(ext))
    asset = Asset(company_id=company_id, name=original_name,
                  type=EXT_TYPE.get(ext, AssetType.OTHER),
                  path=str(fpath.relative_to(root)),
                  mime_type=file.content_type, size=size, created_by=user.id,
                  creator_type="user")
    db.add(asset)
    await log_event(db, "asset.uploaded", "asset", None, "user", user.id,
                    {"name": original_name})
    await db.commit()
    await db.refresh(asset)
    return {"id": asset.id, "name": asset.name, "type": asset.type.value,
            "size": asset.size}


@router.get("/{aid}/download")
async def download(aid: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    a = await _visible_asset(db, user, aid)
    fpath = _resolve_asset_path(a.path)
    if not fpath.is_file():
        raise HTTPException(404, "Asset file missing")
    return FileResponse(fpath, filename=a.name)


@router.get("/{aid}")
async def read_asset(aid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    a = await _visible_asset(db, user, aid)
    p = _resolve_asset_path(a.path)
    if not p.is_file():
        raise HTTPException(404, "Asset file missing")
    if p.suffix.lower() in (".md", ".txt", ".json", ".csv", ".py"):
        return {"id": a.id, "name": a.name, "content": p.read_text(encoding="utf-8",
                                                                 errors="replace")[:100000]}
    return {"id": a.id, "name": a.name, "download": f"/assets/{a.id}/download"}
