"""Company & Department management."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import (Capability, ensure_capability,
                                 require_capability)
from app.db.session import get_db
from app.models.company import Company, Department
from app.services.event_system import log_event

router = APIRouter(tags=["organization"])


class CompanyIn(BaseModel):
    name: str
    description: str | None = None
    logo: str | None = None
    attachments: list[dict] | None = None  # 📎 附件口


class DepartmentIn(BaseModel):
    name: str
    description: str | None = None
    parent_department_id: str | None = None
    sort_order: int | None = None
    pinned: bool | None = None
    skill_ids: list[str] | None = None       # 部门专属技能（skills.id 列表）
    kb_document_ids: list[str] | None = None  # 部门专属知识库文档
    attachments: list[dict] | None = None      # 📎 附件口


@router.get("/companies")
async def list_companies(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = await db.execute(select(Company).where(Company.owner_id == user.id))
    return q.scalars().all()


@router.post("/companies", status_code=201)
async def create_company(data: CompanyIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    company = Company(owner_id=user.id, **data.model_dump())
    db.add(company)
    await log_event(db, "company.created", "company", None, "user", user.id, {"name": data.name})
    await db.commit()
    await db.refresh(company)
    return company


@router.get("/companies/{cid}")
async def get_company(cid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    c = await _owned_company(db, user.id, cid)
    return c


@router.patch("/companies/{cid}")
async def update_company(cid: str, data: CompanyIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    c = await _owned_company(db, user.id, cid)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    await db.commit()
    await db.refresh(c)
    return c


@router.delete("/companies/{cid}", status_code=204,
               # 删公司会级联清除项目、员工、成果、日志——整个系统里最重的一档。
               # 用依赖声明而不是在函数体里比较 owner_id：危险级别对调用方可见，
               # 将来引入"管理员"角色时这里一行都不用改。
               dependencies=[Depends(require_capability(
                   Capability.DESTRUCTIVE, "cid"))])
async def delete_company(cid: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    c = await _owned_company(db, user.id, cid)
    await db.delete(c)
    await db.commit()


@router.get("/companies/{cid}/departments")
async def list_departments(cid: str, db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    await _owned_company(db, user.id, cid)
    q = await db.execute(select(Department).where(Department.company_id == cid))
    rows = q.scalars().all()
    # 置顶优先 → sort_order 升序 → 创建时间
    rows = sorted(rows, key=lambda d: (not d.pinned, d.sort_order,
                                       d.created_at or __import__("datetime").datetime.max))
    return [{
        "id": d.id, "name": d.name, "description": d.description,
        "parent_department_id": d.parent_department_id,
        "sort_order": d.sort_order, "pinned": d.pinned,
        "skill_ids": d.skill_ids or [], "kb_document_ids": d.kb_document_ids or [],
        "attachments": d.attachments or [],
    } for d in rows]


@router.post("/departments", status_code=201)
async def create_department(cid: str, data: DepartmentIn,
                            db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    await _owned_company(db, user.id, cid)
    d = Department(company_id=cid, **data.model_dump())
    db.add(d)
    await log_event(db, "department.created", "department", None, "user", user.id,
                    {"name": data.name, "company_id": cid})
    await db.commit()
    await db.refresh(d)
    return d


class DepartmentPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_department_id: str | None = None
    sort_order: int | None = None
    pinned: bool | None = None
    skill_ids: list[str] | None = None
    kb_document_ids: list[str] | None = None
    attachments: list[dict] | None = None  # 📎 附件口


@router.patch("/departments/{did}")
async def update_department(did: str, data: DepartmentPatchIn,
                            db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    d = await _owned_department(db, user.id, did)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    await db.commit()
    await db.refresh(d)
    return d


class ReorderIn(BaseModel):
    ordered_ids: list[str]  # 按新顺序排列的部门 id


@router.post("/departments/reorder")
async def reorder_departments(data: ReorderIn,
                              db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """按传入顺序设置 sort_order（用于卡片排序）。"""
    for i, did in enumerate(data.ordered_ids):
        d = await _owned_department(db, user.id, did)
        d.sort_order = i
    await db.commit()
    return {"updated": len(data.ordered_ids)}


class PinIn(BaseModel):
    pinned: bool


@router.put("/departments/{did}/pin")
async def pin_department(did: str, data: PinIn,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """置顶/取消置顶部门。置顶的 sort_order 设为 -1 组。"""
    d = await _owned_department(db, user.id, did)
    d.pinned = data.pinned
    d.sort_order = -1000 if data.pinned else 0
    await db.commit()
    return {"id": did, "pinned": d.pinned}


@router.delete("/departments/{did}", status_code=204)
async def delete_department(did: str, db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    d = await _owned_department(db, user.id, did)
    # 删部门会解除员工与专属技能/知识库的关联（可重建），属于次高危险级别
    await ensure_capability(db, user.id, d.company_id, Capability.MANAGE)
    await db.delete(d)
    await db.commit()


async def _owned_company(db: AsyncSession, owner_id: str, cid: str) -> Company:
    c = await db.get(Company, cid)
    if not c or c.owner_id != owner_id:
        raise HTTPException(404, "Company not found")
    return c


async def _owned_department(db: AsyncSession, owner_id: str, did: str) -> Department:
    d = await db.get(Department, did)
    if not d:
        raise HTTPException(404, "Department not found")
    c = await db.get(Company, d.company_id)
    if not c or c.owner_id != owner_id:
        raise HTTPException(404, "Department not found")
    return d
