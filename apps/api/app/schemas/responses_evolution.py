"""CEO 自我进化域的响应模型。

字段与 `app/self_evolution.py` 的返回逐条对应；全部 `extra="allow"`，
判断错时多余键原样透出，不在序列化层静默裁掉。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


class PlaybookOut(BaseModel):
    model_config = _free()

    id: str
    company_id: str
    agent_id: str | None = None
    version: int
    active: bool
    source: str
    parent_version: int | None = None
    persona: dict[str, Any] = {}
    principles: list[Any] = []
    adjustments: dict[str, Any] = {}
    summary: str | None = None
    lessons: list[Any] = []
    evolution_id: str | None = None
    created_at: str | None = None


class EvolutionActionOut(BaseModel):
    model_config = _free()

    id: str
    type: str
    title: str
    detail: str = ""
    risk: str = "low"
    applied: bool = False
    approved: bool = False
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None


class EvolutionRecordOut(BaseModel):
    model_config = _free()

    id: str
    company_id: str
    level: str
    period_key: str
    period_start: str | None = None
    period_end: str | None = None
    status: str
    assessment: dict[str, Any] = {}
    diagnosis: str | None = None
    summary: str | None = None
    actions: list[EvolutionActionOut] = []
    metrics: dict[str, Any] = {}
    playbook_id: str | None = None
    playbook_version: int | None = None
    exam_id: str | None = None
    error: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    tokens_used: int = 0
    cost: float = 0.0
    source: str = "schedule"
    created_at: str | None = None


class EvolutionScheduleOut(BaseModel):
    model_config = _free()

    id: str
    name: str
    action_type: str
    trigger_type: str
    trigger_config: dict[str, Any] = {}
    next_run_at: str | None = None
    run_count: int = 0


class EvolutionOverviewOut(BaseModel):
    model_config = _free()

    company_id: str
    playbook: PlaybookOut | None = None
    records: list[EvolutionRecordOut] = []
    schedules: list[EvolutionScheduleOut] = []
    levels: list[dict[str, Any]] = []


class EvolutionRunIn(BaseModel):
    company_id: str
    level: str = "daily"


class EvolutionDecideIn(BaseModel):
    approve: bool = True
    note: str | None = None


class EvolutionSchedulesOut(BaseModel):
    model_config = _free()

    company_id: str
    schedules: list[EvolutionScheduleOut] = []
    created: dict[str, Any] = {}
