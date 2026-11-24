"""C1 任务收件箱：把"公司里现在有什么活"按处理阶段分桶。

为什么不在前端拼：分桶依据（任务状态 + 最新成果的验收状态）跨三张表，
放在前端就得把全量任务、全量成果都拉到浏览器再过滤，既慢又容易
在"看到别的公司的数据"这类边界上出错。

分桶口径（对齐老板的工作视角）：
* in_progress        正在推进：PLANNING / RUNNING / WAITING / PAUSED / REVIEW
* awaiting_acceptance 已完成，但最新成果还在等验收（IN_REVIEW/APPROVED 且未 accepted）
* needs_rework       最新成果被驳回，等待重新提交（任务执行状态仍是 COMPLETED）
* completed          已完成且不需要验收或返工
* failed             FAILED（CANCELLED 是用户主动放弃，不进失败桶）
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.scope import CompanyCtx, build_ctx
from app.db.session import get_db
from app.models.agent import Agent
from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.task import Task, TaskStatus
from app.schemas.responses import InboxOut

router = APIRouter(prefix="/tasks", tags=["inbox"])

BUCKET_LIMIT = 20

IN_PROGRESS_STATUSES = (TaskStatus.PLANNING, TaskStatus.RUNNING,
                        TaskStatus.WAITING, TaskStatus.PAUSED, TaskStatus.REVIEW)
# 需要老板/验收人拍板的成果状态
ACCEPTANCE_STATUSES = (ArtifactStatus.IN_REVIEW, ArtifactStatus.APPROVED)


def _scope(ctx: CompanyCtx):
    """与 GET /tasks 同口径：owner/admin 看全公司，其余只看自己创建的。"""
    base = Task.company_id == ctx.company_id
    if ctx.sees_all_agents:
        return base
    return and_(base, Task.created_by == ctx.user_id)


def _item(row, artifacts: dict, names: dict, reviews: dict) -> dict:
    art = artifacts.get(row.id)
    review = reviews.get(art.id, {}) if art else {}
    return {
        "id": row.id,
        "task_id": row.id,
        "title": row.title,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "agent_id": row.assigned_agent_id,
        "agent_name": names.get(row.assigned_agent_id),
        "updated_at": row.updated_at,
        "artifact_id": art.id if art else None,
        "artifact_status": (art.status.value
                            if art and hasattr(art.status, "value") else None),
        "current_version": art.current_version if art else None,
        "review_comment": review.get("review_comment"),
    }


@router.get("/inbox", response_model=InboxOut)
async def task_inbox(company_id: str = Query(...),
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    # build_ctx 对"与自己无关的公司"直接 404（不泄露该公司是否存在）
    ctx = await build_ctx(db, user, company_id)

    # 只取收件箱需要的列：Task.result 可能有上万字，全量拉回来纯属浪费
    rows = (await db.execute(
        select(Task.id, Task.title, Task.status, Task.assigned_agent_id,
               Task.updated_at, Task.created_at)
        .where(_scope(ctx))
        .order_by(Task.updated_at.desc(), Task.created_at.desc()))).all()

    in_progress, failed = [], []
    completed = []
    for row in rows:
        status = row.status
        if status in IN_PROGRESS_STATUSES:
            in_progress.append(row)
        elif status == TaskStatus.FAILED:
            failed.append(row)
        elif status == TaskStatus.COMPLETED:
            completed.append(row)

    # 最新成果：按 created_at 倒序取第一条即"该任务的最新成果"
    artifacts: dict = {}
    if completed:
        task_ids = [r.id for r in completed]
        arts = (await db.execute(
            select(Artifact.id, Artifact.task_id, Artifact.status,
                   Artifact.accepted_at, Artifact.current_version, Artifact.created_at)
            .where(Artifact.task_id.in_(task_ids))
            .order_by(Artifact.created_at.desc(), Artifact.id.desc()))).all()
        for art in arts:
            artifacts.setdefault(art.task_id, art)

    # 当前版本的审核意见随最新成果一起返回，避免返工入口再发一次请求。
    reviews: dict = {}
    if artifacts:
        artifact_versions = [
            (art.id, art.current_version) for art in artifacts.values()
            if art.current_version is not None
        ]
        if artifact_versions:
            version_rows = (await db.execute(select(
                ArtifactVersion.artifact_id, ArtifactVersion.version,
                ArtifactVersion.review_comment).where(
                tuple_(ArtifactVersion.artifact_id, ArtifactVersion.version).in_(
                    artifact_versions)))).all()
            reviews = {r.artifact_id: {"review_comment": r.review_comment}
                       for r in version_rows}

    awaiting, needs_rework, done = [], [], []
    for row in completed:
        art = artifacts.get(row.id)
        if art is not None and art.status == ArtifactStatus.REJECTED:
            # 执行完成与成果返工是两个维度：任务保持 COMPLETED，仅派生此桶。
            needs_rework.append(row)
        elif (art is not None and art.status in ACCEPTANCE_STATUSES
              and art.accepted_at is None):
            awaiting.append(row)
        else:
            done.append(row)

    # 待返工是必须逐项处理的队列，不能像普通展示桶一样截断；其它桶保留
    # BUCKET_LIMIT 控制首屏体积。
    selected = (in_progress[:BUCKET_LIMIT] + awaiting[:BUCKET_LIMIT]
                + needs_rework + done[:BUCKET_LIMIT] + failed[:BUCKET_LIMIT])
    agent_ids = {r.assigned_agent_id for r in selected if r.assigned_agent_id}
    names: dict = {}
    if agent_ids:
        names = {aid: name for aid, name in (await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()}

    return {
        "company_id": company_id,
        "counts": {
            "in_progress": len(in_progress),
            "awaiting_acceptance": len(awaiting),
            "needs_rework": len(needs_rework),
            "completed": len(done),
            "failed": len(failed),
        },
        "in_progress": [_item(r, artifacts, names, reviews)
                        for r in in_progress[:BUCKET_LIMIT]],
        "awaiting_acceptance": [_item(r, artifacts, names, reviews)
                               for r in awaiting[:BUCKET_LIMIT]],
        "needs_rework": [_item(r, artifacts, names, reviews)
                         for r in needs_rework],
        "completed": [_item(r, artifacts, names, reviews)
                      for r in done[:BUCKET_LIMIT]],
        "failed": [_item(r, artifacts, names, reviews)
                   for r in failed[:BUCKET_LIMIT]],
    }
