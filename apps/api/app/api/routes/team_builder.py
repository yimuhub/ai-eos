"""Team Builder API (P2-12)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.team_builder import TEAM_TEMPLATES, build_team_for_project
from app.models.user import User

router = APIRouter(prefix="/team-builder", tags=["team-builder"])


@router.get("/templates")
async def list_templates(user: User = Depends(get_current_user)):
    """列出可用的团队模板。"""
    return [{"key": k, "name": v["name"],
             "roles": [r["role_in_project"] for r in v["roles"]]}
            for k, v in TEAM_TEMPLATES.items()]


class BuildIn(BaseModel):
    template_key: str | None = None


@router.post("/build/{project_id}")
async def build_team(
    project_id: str,
    data: BuildIn | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """为项目自动组建团队。"""
    result = await build_team_for_project(db, project_id,
                                          data.template_key if data else None)
    if not result:
        raise HTTPException(404, "Project not found")
    return result
