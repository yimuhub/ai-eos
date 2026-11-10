"""Artifact Collaboration API (P2-09): Draft -> Review -> Approve."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (assert_company_capability, assert_company_owned,
                          get_current_user, get_db)
from app.core.logging_config import get_logger
from app.core.permissions import Capability
from app.models.artifact import (Artifact, ArtifactStatus, ArtifactVersion,
                                 can_transition)
from app.models.deliverable import DeliverableComment
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.responses import (
    ArtifactAcceptOut,
    ArtifactCommentDeletedOut,
    ArtifactCommentOut,
    ArtifactDeclineOut,
)
from app.schemas.responses_ops import (
    ArtifactCreatedOut,
    ArtifactDeletedOut,
    ArtifactDetailOut,
    ArtifactListItemOut,
    ArtifactNewVersionOut,
    ArtifactPublishDecideOut,
    ArtifactPublishOut,
    ArtifactReviewDecisionOut,
    ArtifactSopOut,
    ArtifactSubmitOut,
    ArtifactVersionContentOut,
)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])
logger = get_logger(__name__)


class ArtifactCreate(BaseModel):
    name: str
    artifact_type: str = "document"
    content: str = ""
    project_id: str | None = None
    # B2：交付物可以挂到某个任务下，收件箱/交付单由此把"任务完成"与
    # "成果被验收"串成同一条链路。
    task_id: str | None = None


class DeclineIn(BaseModel):
    comment: str | None = None


class CommentIn(BaseModel):
    body: str


class VersionSubmit(BaseModel):
    content: str
    change_note: str | None = None
    submitted_by_agent_id: str | None = None


class ReviewIn(BaseModel):
    decision: str          # approve / reject / withdraw
    comment: str | None = None
    reviewer_agent_id: str | None = None


@router.post("", response_model=ArtifactCreatedOut)
async def create_artifact(
    data: ArtifactCreate,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建成果并写入 v1 草稿。"""
    await assert_company_capability(db, user.id, company_id, Capability.OPERATE)
    if data.task_id:
        # 只能挂本公司的任务；跨公司按"不存在"处理，避免用 404/403 的差异
        # 探测别人公司有哪些任务 id。
        task = await db.get(Task, data.task_id)
        if not task or task.company_id != company_id:
            raise HTTPException(404, "Task not found")
    if data.project_id:
        # 修复：project_id 此前不校验——可挂别公司项目 id"借壳"，按项目
        # 过滤时成果会出现在别人的项目下。
        proj = await db.get(Project, data.project_id)
        if not proj or proj.company_id != company_id:
            raise HTTPException(404, "Project not found")
    art = Artifact(id=str(uuid.uuid4()), company_id=company_id,
                   project_id=data.project_id, name=data.name,
                   artifact_type=data.artifact_type, current_version=1,
                   created_by=user.id, task_id=data.task_id)
    db.add(art)
    v1 = ArtifactVersion(id=str(uuid.uuid4()), artifact_id=art.id, version=1,
                         content=data.content, change_note="初始版本",
                         submitted_by=user.id)
    db.add(v1)
    await db.commit()
    return {"id": art.id, "name": art.name, "status": art.status.value,
            "current_version": 1, "task_id": art.task_id}


@router.get("", response_model=list[ArtifactListItemOut])
async def list_artifacts(
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    task_id: str | None = Query(None),
    status: ArtifactStatus | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 读路径同样要归属校验（此前只有写路径有，带别人的 company_id 即可列其成果）
    await assert_company_owned(db, user.id, company_id)
    q = select(Artifact).where(Artifact.company_id == company_id)
    if project_id:
        q = q.where(Artifact.project_id == project_id)
    if task_id:
        q = q.where(Artifact.task_id == task_id)
    if status:
        q = q.where(Artifact.status == status)
    rows = (await db.execute(q.order_by(Artifact.created_at.desc()))).scalars().all()
    return [{"id": a.id, "name": a.name, "type": a.artifact_type,
             "status": a.status.value, "version": a.current_version,
             "project_id": a.project_id, "task_id": a.task_id,
             "accepted_at": a.accepted_at} for a in rows]


@router.get("/{artifact_id}", response_model=ArtifactDetailOut)
async def get_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    art = await _owned_artifact(artifact_id, db, user)
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id).order_by(
        ArtifactVersion.version.desc()))
    versions = vq.scalars().all()
    comment_count = (await db.execute(
        select(func.count()).select_from(DeliverableComment).where(
            DeliverableComment.artifact_id == artifact_id))).scalar_one()
    return {
        "id": art.id, "name": art.name, "type": art.artifact_type,
        "status": art.status.value, "current_version": art.current_version,
        "task_id": art.task_id, "accepted_by": art.accepted_by,
        "accepted_at": art.accepted_at, "comment_count": comment_count,
        "versions": [{
            "version": v.version, "status": v.status.value,
            "change_note": v.change_note,
            "content_preview": v.content[:200],
            "reviewed_by": v.reviewed_by,
            "review_comment": v.review_comment,
        } for v in versions],
    }


@router.get("/{artifact_id}/versions/{version}",
            response_model=ArtifactVersionContentOut)
async def get_version_content(
    artifact_id: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 版本全文是最敏感的读取面：先做成果归属校验再取版本
    await _owned_artifact(artifact_id, db, user)
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id,
        ArtifactVersion.version == version))
    v = vq.scalars().first()
    if not v:
        raise HTTPException(404, "Version not found")
    return {"version": v.version, "content": v.content,
            "status": v.status.value}


@router.post("/{artifact_id}/versions", response_model=ArtifactNewVersionOut)
async def submit_new_version(
    artifact_id: str,
    data: VersionSubmit,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交新版本（草稿或被驳回后重新提交）。"""
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    if art.status not in (ArtifactStatus.DRAFT, ArtifactStatus.REJECTED):
        raise HTTPException(409, f"Cannot submit new version in status {art.status.value}")

    new_ver = art.current_version + 1 if art.status == ArtifactStatus.REJECTED \
        else art.current_version
    v = ArtifactVersion(id=str(uuid.uuid4()), artifact_id=artifact_id,
                        version=new_ver + (1 if art.status == ArtifactStatus.REJECTED else 0),
                        content=data.content, change_note=data.change_note,
                        submitted_by=data.submitted_by_agent_id or user.id)
    # 防重复版本号：取最大版本+1
    max_q = await db.execute(select(ArtifactVersion.version).where(
        ArtifactVersion.artifact_id == artifact_id).order_by(
        ArtifactVersion.version.desc()).limit(1))
    max_ver = max_q.scalar() or 0
    v.version = max_ver + 1

    db.add(v)
    art.current_version = v.version
    await db.commit()
    return {"artifact_id": artifact_id, "new_version": v.version,
            "status": art.status.value}


@router.post("/{artifact_id}/submit", response_model=ArtifactSubmitOut)
async def submit_for_review(
    artifact_id: str,
    data: VersionSubmit | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交审核：DRAFT/REJECTED → IN_REVIEW。"""
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    if not can_transition(art.status, ArtifactStatus.IN_REVIEW):
        raise HTTPException(409, f"Cannot submit from {art.status.value}")

    # 可选同时更新内容为新版本
    if data and data.content:
        max_q = await db.execute(select(ArtifactVersion.version).where(
            ArtifactVersion.artifact_id == artifact_id).order_by(
            ArtifactVersion.version.desc()).limit(1))
        new_v = ArtifactVersion(
            id=str(uuid.uuid4()), artifact_id=artifact_id,
            version=(max_q.scalar() or 0) + 1, content=data.content,
            change_note=data.change_note or "提交审核",
            submitted_by=data.submitted_by_agent_id or user.id)
        db.add(new_v)
        art.current_version = new_v.version

    art.status = ArtifactStatus.IN_REVIEW
    await db.commit()
    return {"id": artifact_id, "status": "IN_REVIEW",
            "current_version": art.current_version}


@router.post("/{artifact_id}/review", response_model=ArtifactReviewDecisionOut)
async def review_artifact(
    artifact_id: str,
    data: ReviewIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审核：approve / reject / withdraw。"""
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)

    decision_map = {
        "approve": ArtifactStatus.APPROVED,
        "reject": ArtifactStatus.REJECTED,
        "withdraw": ArtifactStatus.DRAFT,
    }
    target = decision_map.get(data.decision)
    if not target:
        raise HTTPException(400, "decision must be approve/reject/withdraw")
    if not can_transition(art.status, target):
        raise HTTPException(409,
                            f"Cannot {data.decision} from {art.status.value}")

    # 更新当前版本的审核信息
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id,
        ArtifactVersion.version == art.current_version))
    cur_v = vq.scalars().first()
    if cur_v:
        cur_v.status = target
        cur_v.reviewed_by = data.reviewer_agent_id or user.id
        cur_v.review_comment = data.comment

    art.status = target
    await db.commit()
    return {"id": artifact_id, "status": art.status.value,
            "reviewer": data.reviewer_agent_id or user.id}


@router.post("/{artifact_id}/accept", response_model=ArtifactAcceptOut)
async def accept_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """B2 验收：只有 APPROVED 的交付物能被老板确认接收。

    验收是"我收到了、这事结了"的业务事实，因此幂等：重复点击返回
    already=true，而不是 409 —— 前端超时重试不该被当成错误。
    """
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    if art.accepted_at is not None:
        return {"id": artifact_id, "already": True,
                "status": art.status.value,
                "accepted_by": art.accepted_by,
                "accepted_at": art.accepted_at}
    if art.status != ArtifactStatus.APPROVED:
        raise HTTPException(
            409, f"只有审核通过（APPROVED）的交付物可以验收，当前：{art.status.value}")

    art.accepted_by = user.id
    art.accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if art.created_by and art.created_by != user.id:
        # 与业务同事务（notify 不 commit）：验收事实与通知要么都落库，要么都不落。
        from app.services.notification_service import notify
        await notify(
            db, user_id=art.created_by, company_id=art.company_id,
            type="artifact.accepted",
            title=f"交付物已验收：{art.name}",
            body="老板已确认接收该交付物，无需再跟进。",
            link="/artifacts")
    await db.commit()
    return {"id": artifact_id, "already": False, "status": art.status.value,
            "accepted_by": art.accepted_by, "accepted_at": art.accepted_at}


@router.post("/{artifact_id}/decline", response_model=ArtifactDeclineOut)
async def decline_artifact(
    artifact_id: str,
    data: DeclineIn | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """B2 验收退回：APPROVED 的成果被老板退回返工。

    与审核驳回区分：这里不是"没过审"，而是"验收时发现不对"。因此状态
    同样落到 REJECTED（可重新提交），但必须清空验收痕迹，并把退回意见
    同时写进当前版本与交付评论，返工的人在两处都能看到原因。
    """
    comment = (data.comment or "").strip() if data and data.comment else ""
    if data and data.comment is not None and not (1 <= len(comment) <= 2000):
        raise HTTPException(400, "comment 长度需为 1-2000 字")

    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    if art.status != ArtifactStatus.APPROVED:
        raise HTTPException(
            409, f"只有审核通过（APPROVED）的交付物可以验收退回，当前：{art.status.value}")

    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id,
        ArtifactVersion.version == art.current_version))
    cur_v = vq.scalars().first()
    if cur_v:
        cur_v.status = ArtifactStatus.REJECTED
        cur_v.reviewed_by = user.id
        cur_v.review_comment = comment or None

    art.status = ArtifactStatus.REJECTED
    art.accepted_by = None
    art.accepted_at = None
    db.add(DeliverableComment(
        company_id=art.company_id, artifact_id=artifact_id,
        author_type="user", author_id=user.id,
        body=comment or "验收退回：请返工后重新提交"))
    await db.commit()
    return {"id": artifact_id, "status": art.status.value,
            "comment": comment or None}


@router.get("/{artifact_id}/comments", response_model=list[ArtifactCommentOut])
async def list_comments(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """B2 交付评论：按时间正序，像聊天记录一样从旧到新读。"""
    await _owned_artifact(artifact_id, db, user)
    rows = (await db.execute(
        select(DeliverableComment).where(
            DeliverableComment.artifact_id == artifact_id)
        .order_by(DeliverableComment.created_at.asc(),
                  DeliverableComment.id.asc()))).scalars().all()
    return [{"id": c.id, "artifact_id": c.artifact_id,
             "author_type": c.author_type, "author_id": c.author_id,
             "body": c.body, "created_at": c.created_at} for c in rows]


@router.post("/{artifact_id}/comments", response_model=ArtifactCommentOut)
async def add_comment(
    artifact_id: str,
    data: CommentIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """B2 交付评论：当前用户（user）发言；company_id 从成果反查，不信请求参数。"""
    body = data.body.strip()
    if not (1 <= len(body) <= 4000):
        raise HTTPException(400, "body 长度需为 1-4000 字")
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    row = DeliverableComment(
        company_id=art.company_id, artifact_id=artifact_id,
        author_type="user", author_id=user.id, body=body)
    db.add(row)
    await db.commit()
    return {"id": row.id, "artifact_id": artifact_id,
            "author_type": row.author_type, "author_id": row.author_id,
            "body": row.body, "created_at": row.created_at}


@router.delete("/{artifact_id}/comments/{comment_id}",
               response_model=ArtifactCommentDeletedOut)
async def delete_comment(
    artifact_id: str,
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """B2 删除评论：作者本人可删；其他人需要 MANAGE（老板/管理员）。"""
    art = await _owned_artifact(artifact_id, db, user)
    row = await db.get(DeliverableComment, comment_id)
    if not row or row.artifact_id != artifact_id:
        raise HTTPException(404, "Comment not found")
    if row.author_id != user.id:
        await assert_company_capability(db, user.id, art.company_id,
                                        Capability.MANAGE)
    await db.delete(row)
    await db.commit()
    return {"id": comment_id, "deleted": True}


async def _owned_artifact(artifact_id: str, db: AsyncSession, user: User,
                          capability: Capability = Capability.VIEW) -> Artifact:
    """取成果并做门禁：公司角色需具备 capability，否则 404（不泄露存在性）。"""
    art = await db.get(Artifact, artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")
    await assert_company_capability(db, user.id, art.company_id, capability)
    return art


async def _ensure_published_at_column(db: AsyncSession) -> None:  # pragma: no cover
    """兼容旧库：published_at 已进模型，启动时 ensure_columns/Alembic 会补列。

    仍保留一次幂等探测，防止"发布早于迁移"的老库把 SQL 打死。
    """
    from sqlalchemy import inspect, text

    def _missing(sync_sess) -> bool:
        conn = sync_sess.connection()
        cols = {c["name"] for c in inspect(conn).get_columns("artifacts")}
        return "published_at" not in cols

    if await db.run_sync(_missing):
        dialect = db.get_bind().dialect.name
        col_type = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
        await db.execute(text(
            f"ALTER TABLE artifacts ADD COLUMN published_at {col_type}"))
        await db.commit()
        logger.warning("[artifacts] 已为旧库幂等补列 artifacts.published_at")


# exclude_unset：直接发布 / 已发布幂等 / 需二次确认是三种不同返回，
# 否则没走到的分支会多出 null 键（前端按 key 是否存在分支时会变行为）。
@router.post("/{artifact_id}/publish", response_model=ArtifactPublishOut,
             response_model_exclude_unset=True)
async def publish_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """V5.5 C2 交付流水线三分离：计划/执行 → 审批（review approve）→ 发布。

    APPROVED 的成果点"发布"才离开内网：
    - 公司策略 approval_actions 含 "artifact_publish" → 建发布审批单，
      老板批准后自动标记 published_at（双确认）
    - 否则直接发布（小公司单人模式，不增加流程负担）
    """
    import uuid as _uuid
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    if art.status.value != "APPROVED":
        raise HTTPException(409, f"只有审核通过（APPROVED）的成果可以发布，当前：{art.status.value}")

    from sqlalchemy import text
    await _ensure_published_at_column(db)
    # 已发布 → 幂等返回
    if art.published_at:
        return {"id": artifact_id, "published_at": str(art.published_at),
                "already": True}

    try:
        from app.models.intervention import InterventionPolicy
        pol = (await db.execute(
            select(InterventionPolicy).where(
                InterventionPolicy.company_id == art.company_id,
                InterventionPolicy.level >= 1))).scalars().first()
        needs_approval = bool(pol and "artifact_publish" in (pol.approval_actions or []))
    except Exception as e:  # noqa: BLE001
        # 读不到策略时**默认需要审批**：宁可多一道确认，也不能因为
        # 查询失败就把"成果离开内网"这个动作静默放行（fail-closed）。
        needs_approval = True
        logger.warning("[artifacts] 读取公司审批策略失败，按需要审批处理：%s", e,
                       exc_info=True)

    if needs_approval:
        from app.models.intervention import ApprovalRequest
        req = ApprovalRequest(
            id=str(_uuid.uuid4()), company_id=art.company_id,
            action_type="artifact_publish",
            payload={"artifact_id": artifact_id, "awaiting_execution": True},
            reason=f"交付物「{art.name}」申请正式发布（已审核通过）")
        db.add(req)
        from app.services.notification_service import notify_approval_pending
        await notify_approval_pending(
            db, company_id=art.company_id,
            title=f"待审批：发布交付物「{art.name}」",
            body="交付物申请正式发布，需要二次确认。")
        await db.commit()
        return {"status": "pending_approval", "approval_id": req.id,
                "message": "发布需老板二次确认（交付流水线三分离）"}

    # SQLite 没有 NOW() 函数（实测 OperationalError: no such function: NOW）。
    # 改为绑定参数；写入 naive UTC，与 models/base.py 的 DateTime(timezone=True)
    # 在 SQLite 下的存储格式（以及本仓库 _now()/decided_at 的写法）保持一致，
    # 避免 aware datetime 带来 '+00:00' 后缀导致与 created_at 不可比。
    published_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.execute(
        text("UPDATE artifacts SET published_at = :now WHERE id = :id"),
        {"id": artifact_id, "now": published_at})
    await db.commit()
    return {"id": artifact_id, "published_at": str(published_at),
            "message": "已发布：交付物可对外交付"}


@router.post("/{artifact_id}/publish/decide", response_model=ArtifactPublishDecideOut)
async def decide_publish(
    artifact_id: str,
    data: ReviewIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """C2：发布审批单 approve 后自动标记 published_at。"""
    from sqlalchemy import text
    from app.models.intervention import ApprovalRequest
    art = await _owned_artifact(artifact_id, db, user, Capability.OPERATE)
    req = (await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.company_id == art.company_id,
            ApprovalRequest.action_type == "artifact_publish",
            ApprovalRequest.status == "PENDING",
            ApprovalRequest.payload["artifact_id"].as_string() == artifact_id)
        .order_by(ApprovalRequest.created_at.desc()))).scalars().first()
    if not req:
        raise HTTPException(404, "没有待审批的发布单")
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve/reject")
    req.status = "APPROVED" if data.decision == "approve" else "REJECTED"
    req.decided_by = user.id
    if data.decision == "approve":
        await _ensure_published_at_column(db)
        await db.execute(
            text("UPDATE artifacts SET published_at = :now WHERE id = :id"),
            {"id": artifact_id,
             "now": datetime.now(timezone.utc).replace(tzinfo=None)})
    await db.commit()
    return {"id": artifact_id, "decision": data.decision,
            "published": data.decision == "approve"}


@router.delete("/{artifact_id}", response_model=ArtifactDeletedOut)
async def delete_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除成果（含全部版本）。"""
    # 成果删除只做 MANAGE（与 doc_styles 同口径），不按 DESTRUCTIVE 处理。
    art = await _owned_artifact(artifact_id, db, user, Capability.MANAGE)
    await db.execute(delete(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id))
    # 修复：删除成果连评论一起删——此前孤儿评论常驻，comment_count 对
    # 已删 id 仍能统计出来（deliverable_comments.artifact_id 带
    # ondelete=CASCADE，但 SQLite 主引擎才开 FK；这里显式删干净）。
    await db.execute(delete(DeliverableComment).where(
        DeliverableComment.artifact_id == artifact_id))
    await db.delete(art)
    await db.commit()
    return {"id": artifact_id, "deleted": True}


@router.get("/{artifact_id}/sop", response_model=ArtifactSopOut)
async def artifact_sop(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """V5.6.1 ④ SOP 交接文档（学 OpenHands handoff）：把交付物 + 上下文
    结构化成 Markdown 交接单，下一个 Agent/老板可直接接手而不靠聊天记录。
    包含：成果信息、当前状态、最近 3 版变更、审阅记录、待办建议。
    """
    art = await _owned_artifact(artifact_id, db, user)
    from datetime import datetime
    versions = (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id)
        .order_by(ArtifactVersion.version.desc()).limit(3))).scalars().all()
    v = versions[0] if versions else None
    reviews = [ver for ver in (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id)
        .order_by(ArtifactVersion.version.desc()).limit(10)
    )).scalars().all() if ver.review_comment]

    lines = [
        f"# 交接单：{art.name}",
        "",
        f"- **类型**：{art.artifact_type}",
        f"- **状态**：{art.status.value}（v{art.current_version}）",
        # 修复：此前 datetime.now() 是本地时间，SOP 时间戳随服务器时区漂移
        f"- **生成时间**：{datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')}",
        "",
        "## 交接内容（最新版摘要）",
        (v.content or "")[:800] if v else "",
        "",
        "## 变更历史（最近 3 版）",
    ]
    for ver in versions:
        lines.append(f"- **v{ver.version}**：{ver.change_note or '（无说明）'}"
                     f"（{ver.created_at.isoformat(timespec='seconds') if ver.created_at else '?'}）")
    lines += ["", "## 审阅记录（最近 10 条）"]
    if not reviews:
        lines.append("- 暂无")
    for r in reviews:
        lines.append(f"- v{r.version} {r.status.value if r.status else ''}：{(r.review_comment or '')[:80]}"
                     f"（审阅人 {r.reviewed_by}）")
    lines += [
        "",
        "## 建议下一步",
    ]
    if art.status.value == "DRAFT":
        lines.append("1. 完善内容后提交审阅（POST /artifacts/{id}/submit）")
    elif art.status.value == "IN_REVIEW":
        lines.append("1. 等待/推进审阅决策（POST /artifacts/{id}/review）")
    elif art.status.value == "APPROVED":
        lines.append("1. 可发布（POST /artifacts/{id}/publish）并通知下游")
    else:
        lines.append("1. 按审阅意见修订后重新提交")

    return {"artifact_id": art.id, "title": art.name,
            "markdown": "\n".join(lines), "versions": len(versions)}


# 无 response_model：StreamingResponse 的 zip 二进制（实测 content-type
# application/zip），声明模型只会让 OpenAPI 的 200 响应与真实字节流不一致。
@router.get("/{artifact_id}/download.zip")
async def download_artifact_zip(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """打包下载：全部版本内容（vN.txt）+ meta.json，zip 流式返回。"""
    art = await _owned_artifact(artifact_id, db, user)
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id).order_by(
        ArtifactVersion.version.asc()))
    versions = vq.scalars().all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in versions:
            zf.writestr(f"v{v.version}.txt", v.content or "")
        zf.writestr("meta.json", json.dumps({
            "id": art.id, "name": art.name, "type": art.artifact_type,
            "status": art.status.value, "current_version": art.current_version,
            "versions": len(versions),
        }, ensure_ascii=False, indent=2))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="artifact-{artifact_id}.zip"'})
