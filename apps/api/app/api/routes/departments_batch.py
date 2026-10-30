"""部门批量操作：批量创建 + 批量删除（处理子部门与员工归属）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user
from app.core.permissions import Capability
from app.db.session import get_db
from app.models.agent import Agent
from app.models.company import Department
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
    # 批量建部门 = 改组织架构，属于 MANAGE：普通成员/只读不可为
    await assert_company_capability(db, user.id, data.company_id,
                                    Capability.MANAGE)
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
    # promote=子部门上提到顶层+员工改为未分配 | cascade=递归删除全部子孙部门（员工一律改为未分配）
    mode: str = "promote"


@router.post("/batch-delete")
async def batch_delete_departments(data: BatchDeleteIn,
                                   db: AsyncSession = Depends(get_db),
                                   user=Depends(get_current_user)):
    user_id = user.id

    # 预校验权限：逐个部门校正到其所属公司的 DESTRUCTIVE（批量删除不可逆）。
    # 无权限与不存在都按"不存在或无权限"跳过，不泄露资源是否存在。
    owned: set[str] = set()
    for did in data.department_ids:
        row = await db.get(Department, did)
        if row is None:
            continue
        try:
            await assert_company_capability(db, user_id, row.company_id,
                                            Capability.DESTRUCTIVE)
        except HTTPException:
            continue
        owned.add(did)

    deleted, failed = [], []

    for did in data.department_ids:
        if did not in owned:
            failed.append({"id": did, "reason": "不存在或无权限"})
            continue
        d = (await db.execute(select(Department).where(Department.id == did))).scalar_one_or_none()
        if not d:
            failed.append({"id": did, "reason": "不存在"})
            continue
        dname = d.name  # 提前取值：回滚/删除后 ORM 对象过期
        try:
            async with db.begin_nested():
                # 子部门处理
                if data.mode == "cascade":
                    # 真级联：BFS 递归收集该部门的全部子孙部门，一并删除；
                    # 集合内所有部门的员工一律改为未分配。
                    # 外键风险核查（2026-10-14）：指向 departments 的外键只有
                    # agents.department_id（可空，已先置 NULL）与自引用的
                    # parent_department_id；Department.manager_agent_id 只是
                    # String(36) 非外键。递归删除无约束风险，保留 cascade 语义。
                    doomed: list[str] = [did]
                    _i = 0
                    while _i < len(doomed):
                        _cur = doomed[_i]
                        _i += 1
                        _rows = await db.execute(text(
                            "SELECT id, company_id FROM departments "
                            "WHERE CAST(parent_department_id AS varchar) = CAST(:cur AS varchar)"),
                            {"cur": _cur})
                        for _rid, _rcomp in _rows.all():
                            _cid = str(_rid)
                            # 防御：只收同公司的子孙（防脏数据串公司），已收过的跳过（防环）
                            if _cid not in doomed and str(_rcomp) == str(d.company_id):
                                doomed.append(_cid)
                    for _cid in doomed:
                        await db.execute(text(
                            "UPDATE agents SET department_id = NULL "
                            "WHERE CAST(department_id AS varchar) = CAST(:cid AS varchar)"),
                            {"cid": _cid})
                    # 先删子孙（BFS 顺序反转即由深及浅），本部门由下面的 db.delete(d) 删除
                    for _cid in reversed(doomed):
                        if _cid == did:
                            continue
                        _sub = await db.get(Department, _cid)
                        if _sub is not None:
                            await db.delete(_sub)
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
                # 显式 flush：本条失败在 savepoint 内回滚，不影响同批其他部门
                await db.flush()
            deleted.append({"id": did, "name": dname})
        except (IntegrityError, DBAPIError) as e:
            # savepoint 已由 begin_nested 回滚；不能 rollback 外层事务，
            # 否则此前成功删除的部门会被一并撤销，却仍报告成功。
            failed.append({"id": did,
                           "reason": f"约束冲突: {str(getattr(e, 'orig', e))[:100]}"})
            db.expire_all()

    if deleted:
        await log_event(db, "department.batch_deleted", "department", None,
                        "user", user_id,
                        {"count": len(deleted), "names": [x["name"] for x in deleted]})
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        failed.extend({"id": x["id"], "reason": f"提交失败: {str(e)[:80]}"}
                      for x in deleted)
        deleted = []

    return {"deleted": deleted, "deleted_count": len(deleted), "failed": failed}
