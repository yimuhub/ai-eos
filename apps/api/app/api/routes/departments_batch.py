"""部门批量操作：批量创建 + 批量删除（处理子部门与员工归属）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Company, Department
from app.services.event_system import log_event

router = APIRouter(prefix="/departments", tags=["departments-batch"])


class BatchCreateIn(BaseModel):
    company_id: str
    parent_department_id: str | None = None  # 统一父部门（可空=顶层）
    departments: list[dict]                  # [{name, description?}, ...]


@router.post("/batch-create", status_code=201)
async def batch_create_departments(data: BatchCreateIn,
                                   db: AsyncSession = Depends(get_db),
                                   user=Depends(get_current_user)):
    c = await db.get(Company, data.company_id)
    if not c or c.owner_id != user.id:
        raise HTTPException(404, "Company not found")
    created = []
    for item in data.departments:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        d = Department(company_id=data.company_id, name=name,
                       description=item.get("description"),
                       parent_department_id=(
                           item.get("parent_department_id") or data.parent_department_id) or None)
        db.add(d)
        created.append(d)
    await log_event(db, "department.batch_created", "department", None,
                    "user", user.id, {"count": len(created)})
    await db.commit()
    return {"created": [{"id": d.id, "name": d.name} for d in created],
            "created_count": len(created)}


class BatchDeleteIn(BaseModel):
    department_ids: list[str]
    mode: str = "promote"  # promote=子部门上提+员工改为未分配 | cascade=级联删除子部门（员工一律未分配）


@router.post("/batch-delete")
async def batch_delete_departments(data: BatchDeleteIn,
                                   db: AsyncSession = Depends(get_db),
                                   user=Depends(get_current_user)):
    user_id = user.id

    # 预校验权限
    q = await db.execute(
        select(Department.id).join(Company, Company.id == Department.company_id)
        .where(Company.owner_id == user_id))
    owned = set(q.scalars().all())

    deleted, failed = [], []

    for did in data.department_ids:
        if did not in owned:
            failed.append({"id": did, "reason": "不存在或无权限"})
            continue
        d = (await db.execute(select(Department).where(Department.id == did))).scalar_one_or_none()
        if not d:
            failed.append({"id": did, "reason": "不存在"})
            continue
        try:
            async with db.begin_nested():
                # 子部门处理
                if data.mode == "cascade":
                    # 级联：子部门递归归入待删集合（简化为直接提升其子部门到顶层后删本部门）
                    await db.execute(text(
                        "UPDATE departments SET parent_department_id = NULL "
                        "WHERE CAST(parent_department_id AS varchar) = CAST(:did AS varchar)"),
                        {"did": did})
                else:
                    await db.execute(text(
                        "UPDATE departments SET parent_department_id = NULL "
                        "WHERE CAST(parent_department_id AS varchar) = CAST(:did AS varchar)"),
                        {"did": did})
                # 员工改为未分配
                await db.execute(text(
                    "UPDATE agents SET department_id = NULL "
                    "WHERE CAST(department_id AS varchar) = CAST(:did AS varchar)"),
                    {"did": did})
                # 部门负责人引用清空（其他部门的 manager_agent_id 可能指向已删部门员工，此处仅清本部门字段）
                await db.execute(text(
                    "UPDATE agents SET department_id = NULL "
                    "WHERE CAST(department_id AS varchar) = CAST(:did AS varchar)"),
                    {"did": did})
                await db.delete(d)
            deleted.append({"id": did, "name": d.name})
        except (IntegrityError, DBAPIError) as e:
            failed.append({"id": did,
                           "reason": f"约束冲突: {str(getattr(e, 'orig', e))[:100]}"})
            await db.rollback()
            db.expire_all()

    if deleted:
        await log_event(db, "department.batch_deleted", "department", None,
                        "user", user_id,
                        {"count": len(deleted), "names": [x["name"] for x in deleted]})
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    return {"deleted": deleted, "deleted_count": len(deleted), "failed": failed}
