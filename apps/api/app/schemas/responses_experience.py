"""Agent 经验池域的响应模型。全部 `extra="allow"`。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


class ExperienceOut(BaseModel):
    model_config = _free()

    id: str
    company_id: str
    agent_id: str
    author_agent_name: str | None = None
    task_id: str
    task_title: str
    reuse_key: str
    kind: str
    scope: str
    status: str
    summary: str | None = None
    steps: list[Any] = []
    pitfalls: list[Any] = []
    lessons: list[Any] = []
    token_tips: list[Any] = []
    quality_tips: list[Any] = []
    tags: list[Any] = []
    attempts: int = 1
    success_count: int = 0
    failure_count: int = 0
    tokens_used: int = 0
    tokens_saved_est: int = 0
    confidence: float = 0.0
    workflow_id: str | None = None
    workflow_version: int | None = None
    adopted_by: list[Any] = []
    source: str = "runtime"
    meta: dict[str, Any] = {}
    created_at: str | None = None


class ExperienceStatsOut(BaseModel):
    model_config = _free()

    total: int = 0
    success: int = 0
    failure: int = 0
    adopted: int = 0
    tokens_saved_est: int = 0


class ExperienceOverviewOut(BaseModel):
    model_config = _free()

    company_id: str
    items: list[ExperienceOut] = []
    stats: ExperienceStatsOut


class ExperienceHarvestIn(BaseModel):
    company_id: str
    task_id: str


class ExperienceAdoptIn(BaseModel):
    agent_id: str
