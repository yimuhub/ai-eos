"""Artifact Collaboration API (P2-09): Draft -> Review -> Approve."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.artifact import (ALLOWED_TRANSITIONS, Artifact, ArtifactStatus,
                                 ArtifactVersion, can_transition)
from app.models.user import User

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


class ArtifactCreate(BaseModel):
    name: str
    artifact_type: str = "document"
    content: str = ""
    project_id: str | None = None


class VersionSubmit(BaseModel):
    content: str
    change_note: str | None = None
    submitted_by_agent_id: str | None = None


class ReviewIn(BaseModel):
    decision: str          # approve / reject / withdraw
    comment: str | None = None
    reviewer_agent_id: str | None = None


@router.post("")
async def create_artifact(
    data: ArtifactCreate,
    company_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建成果并写入 v1 草稿。"""
    art = Artifact(id=str(uuid.uuid4()), company_id=company_id,
                   project_id=data.project_id, name=data.name,
                   artifact_type=data.artifact_type, current_version=1,
                   created_by=user.id)
    db.add(art)
    v1 = ArtifactVersion(id=str(uuid.uuid4()), artifact_id=art.id, version=1,
                         content=data.content, change_note="初始版本",
                         submitted_by=user.id)
    db.add(v1)
    await db.commit()
    return {"id": art.id, "name": art.name, "status": art.status.value,
            "current_version": 1}


@router.get("")
async def list_artifacts(
    company_id: str = Query(...),
    project_id: str | None = Query(None),
    status: ArtifactStatus | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(Artifact).where(Artifact.company_id == company_id)
    if project_id:
        q = q.where(Artifact.project_id == project_id)
    if status:
        q = q.where(Artifact.status == status)
    rows = (await db.execute(q.order_by(Artifact.created_at.desc()))).scalars().all()
    return [{"id": a.id, "name": a.name, "type": a.artifact_type,
             "status": a.status.value, "version": a.current_version,
             "project_id": a.project_id} for a in rows]


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    art = await db.get(Artifact, artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id).order_by(
        ArtifactVersion.version.desc()))
    versions = vq.scalars().all()
    return {
        "id": art.id, "name": art.name, "type": art.artifact_type,
        "status": art.status.value, "current_version": art.current_version,
        "versions": [{
            "version": v.version, "status": v.status.value,
            "change_note": v.change_note,
            "content_preview": v.content[:200],
            "reviewed_by": v.reviewed_by,
            "review_comment": v.review_comment,
        } for v in versions],
    }


@router.get("/{artifact_id}/versions/{version}")
async def get_version_content(
    artifact_id: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    vq = await db.execute(select(ArtifactVersion).where(
        ArtifactVersion.artifact_id == artifact_id,
        ArtifactVersion.version == version))
    v = vq.scalars().first()
    if not v:
        raise HTTPException(404, "Version not found")
    return {"version": v.version, "content": v.content,
            "status": v.status.value}


@router.post("/{artifact_id}/versions")
async def submit_new_version(
    artifact_id: str,
    data: VersionSubmit,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交新版本（草稿或被驳回后重新提交）。"""
    art = await db.get(Artifact, artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")
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


@router.post("/{artifact_id}/submit")
async def submit_for_review(
    artifact_id: str,
    data: VersionSubmit | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交审核：DRAFT/REJECTED → IN_REVIEW。"""
    art = await db.get(Artifact, artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")
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


@router.post("/{artifact_id}/review")
async def review_artifact(
    artifact_id: str,
    data: ReviewIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审核：approve / reject / withdraw。"""
    art = await db.get(Artifact, artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")

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
