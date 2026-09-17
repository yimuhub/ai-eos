"""Asset Center: upload / list / download; runtime artifacts auto-saved."""
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.asset import Asset, AssetType
from app.models.company import Company
from app.services.event_system import log_event

router = APIRouter(prefix="/assets", tags=["assets"])

EXT_TYPE = {".md": AssetType.DOCUMENT, ".txt": AssetType.DOCUMENT,
            ".pdf": AssetType.DOCUMENT, ".docx": AssetType.DOCUMENT,
            ".png": AssetType.IMAGE, ".jpg": AssetType.IMAGE, ".jpeg": AssetType.IMAGE,
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


async def save_artifact(db: AsyncSession, company_id: str, name: str, content: str,
                        task_id: str | None = None, agent_id: str | None = None) -> Asset:
    """Called by Agent Runtime to auto-save text results into the Asset Center."""
    root = storage_root()
    fname = f"{uuid.uuid4().hex[:8]}_{name}"
    fpath = root / "artifacts" / fname
    fpath.write_text(content, encoding="utf-8")
    ext = fpath.suffix.lower()
    atype = AssetType.REPORT if any(h in name.lower() for h in REPORT_HINTS) \
        else EXT_TYPE.get(ext, AssetType.OTHER)
    asset = Asset(company_id=company_id, name=name, type=atype, path=str(fpath),
                  mime_type=mimetypes.guess_type(name)[0], size=len(content.encode()),
                  created_by=agent_id or "", creator_type="agent" if agent_id else "user",
                  task_id=task_id, agent_id=agent_id)
    db.add(asset)
    await log_event(db, "asset.created", "asset", None, "agent", agent_id, {"name": name})
    return asset


@router.get("")
async def list_assets(company_id: str, type: str | None = None,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    q = select(Asset).where(Asset.company_id == company_id)
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
    c = await db.get(Company, company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    root = storage_root()
    fname = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    fpath = root / "uploads" / fname
    data = await file.read()
    fpath.write_bytes(data)
    ext = fpath.suffix.lower()
    asset = Asset(company_id=company_id, name=file.filename,
                  type=EXT_TYPE.get(ext, AssetType.OTHER), path=str(fpath),
                  mime_type=file.content_type, size=len(data), created_by=user.id,
                  creator_type="user")
    db.add(asset)
    await log_event(db, "asset.uploaded", "asset", None, "user", user.id,
                    {"name": file.filename})
    await db.commit()
    await db.refresh(asset)
    return {"id": asset.id, "name": asset.name, "type": asset.type.value,
            "size": asset.size}


@router.get("/{aid}/download")
async def download(aid: str, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    a = await db.get(Asset, aid)
    if not a:
        raise HTTPException(404, "Asset not found")
    c = await db.get(Company, a.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Asset not found")
    return FileResponse(a.path, filename=a.name)


@router.get("/{aid}")
async def read_asset(aid: str, db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    a = await db.get(Asset, aid)
    if not a:
        raise HTTPException(404, "Asset not found")
    c = await db.get(Company, a.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Asset not found")
    p = Path(a.path)
    if p.suffix.lower() in (".md", ".txt", ".json", ".csv", ".py"):
        return {"id": a.id, "name": a.name, "content": p.read_text(encoding="utf-8",
                                                                 errors="replace")[:100000]}
    return {"id": a.id, "name": a.name, "download": f"/assets/{a.id}/download"}
