"""文档风格 DIY (P4)：公司自定义风格库 + Agent 绑定。"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import Column, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.db.base import Base
from app.models.agent import Agent
from app.models.user import User

router = APIRouter(prefix="/doc-styles", tags=["doc-styles"])


class _TS:
    from datetime import datetime
    from sqlalchemy import DateTime, func
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now())
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())


class DocStyle(Base, _TS):
    """公司级自定义文档风格。"""
    __tablename__ = "doc_styles"
    id = Column(String(36), primary_key=True)
    company_id = Column(String(36), index=True)
    name = Column(String(100))                    # 风格名（如"内容创作风"）
    hint = Column(Text)                           # 排版要求提示词
    sample = Column(Text)                         # 示例片段（可选）


class StyleIn(BaseModel):
    name: str
    hint: str
    sample: str | None = None


async def _owned_company(db, user_id, company_id,
                 capability: Capability = Capability.VIEW):
    """公司门禁：走 resolve_role，成员/管理员不再是"陌生人"。越权 404。"""
    return await assert_company_capability(db, user_id, company_id, capability)


@router.get("")
async def list_styles(company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id)
    rows = (await db.execute(select(DocStyle).where(
        DocStyle.company_id == company_id).order_by(DocStyle.created_at))).scalars().all()
    return [{"id": r.id, "name": r.name, "hint": r.hint,
             "sample": r.sample} for r in rows]


@router.post("")
async def create_style(data: StyleIn,
                       company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    import uuid
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    row = DocStyle(id=uuid.uuid4().hex[:32], company_id=company_id,
                   name=data.name, hint=data.hint, sample=data.sample)
    db.add(row)
    await db.commit()
    return {"id": row.id}


@router.patch("/{style_id}")
async def update_style(data: StyleIn, style_id: str,
                       company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    row = await db.get(DocStyle, style_id)
    if not row or row.company_id != company_id:
        raise HTTPException(404)
    row.name, row.hint, row.sample = data.name, data.hint, data.sample
    await db.commit()
    return {"ok": True}


@router.delete("/{style_id}")
async def delete_style(style_id: str, company_id: str = Query(...),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    # 删除风格只做 MANAGE：这是可逆的内容级删除（绑定会被一并解除），
    # 不按 DESTRUCTIVE 处理，避免把公司管理员排除在日常维护之外。
    await _owned_company(db, user.id, company_id, Capability.MANAGE)
    row = await db.get(DocStyle, style_id)
    if not row or row.company_id != company_id:
        raise HTTPException(404)
    # 解除所有绑定
    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id,
        Agent.doc_style_id == style_id))).scalars().all()
    for a in agents:
        a.doc_style_id = None
    await db.delete(row)
    await db.commit()
    return {"ok": True}


class BindIn(BaseModel):
    agent_ids: list[str]
    style_id: str | None = None   # None=解除绑定


@router.post("/assign")
async def assign(data: BindIn, company_id: str = Query(...),
                 db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """把风格批量分配给 Agent；style_id=None 解除绑定。"""
    await _owned_company(db, user.id, company_id, Capability.OPERATE)
    if data.style_id:
        row = await db.get(DocStyle, data.style_id)
        if not row or row.company_id != company_id:
            raise HTTPException(404, "style not found")
    from app.models.agent import Agent
    changed = 0
    for aid in data.agent_ids:
        a = await db.get(Agent, aid)
        if a and a.company_id == company_id:
            a.doc_style_id = data.style_id
            changed += 1
    await db.commit()
    return {"changed": changed}


@router.post("/upload-sample")
async def upload_sample(company_id: str = Query(...),
                        file: UploadFile = File(...),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """上传示例附件 → 自动转 Markdown 作为风格示例。"""
    from app.doc_convert import extract_to_md

    await _owned_company(db, user.id, company_id)
    raw = await file.read()
    md, ok = extract_to_md(file.filename or "sample", raw)
    if not ok:
        raise HTTPException(400, f"无法解析 {file.filename}，请用 txt/md/docx/pdf/xlsx/pptx")
    return {"file_name": file.filename, "markdown": md[:20000]}
