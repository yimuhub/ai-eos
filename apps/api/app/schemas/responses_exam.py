"""CEO 周考域的响应模型。全部 `extra="allow"`，多余键原样透出。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


class ExamQuestionOut(BaseModel):
    model_config = _free()

    id: str
    dimension: str
    dimension_label: str | None = None
    question: str
    rubric: str = ""
    max_score: int = 10


class ExamAnswerOut(BaseModel):
    model_config = _free()

    id: str
    answer: str = ""
    answered: bool = False


class ExamGradeOut(BaseModel):
    model_config = _free()

    id: str
    score: float = 0.0
    max_score: int = 10
    comment: str = ""


class ExamOverrideOut(BaseModel):
    model_config = _free()

    by: str | None = None
    at: str | None = None
    note: str | None = None
    previous: dict[str, Any] = {}


class ExamOut(BaseModel):
    model_config = _free()

    id: str
    company_id: str
    ceo_agent_id: str | None = None
    supervisor_agent_id: str | None = None
    period_key: str
    week_start: str | None = None
    week_end: str | None = None
    status: str
    focus: str | None = None
    questions: list[ExamQuestionOut] = []
    answers: list[ExamAnswerOut] = []
    grades: list[ExamGradeOut] = []
    scores: dict[str, Any] = {}
    personality: dict[str, Any] = {}
    overall: float | None = None
    grade: str | None = None
    summary: str | None = None
    recommendations: list[Any] = []
    metrics: dict[str, Any] = {}
    override: ExamOverrideOut | None = None
    tokens_used: int = 0
    cost: float = 0.0
    error: str | None = None
    source: str | None = None
    created_at: str | None = None
    reused: bool | None = None


class ExamScheduleOut(BaseModel):
    model_config = _free()

    id: str
    name: str
    action_type: str
    trigger_type: str
    trigger_config: dict[str, Any] = {}
    next_run_at: str | None = None
    run_count: int = 0


class ExamOverviewOut(BaseModel):
    model_config = _free()

    company_id: str
    latest: ExamOut | None = None
    exams: list[ExamOut] = []
    supervisor: dict[str, Any] | None = None
    schedule: ExamScheduleOut | None = None
    dimensions: list[dict[str, Any]] = []


class ExamRunIn(BaseModel):
    company_id: str


class ExamGradePatchIn(BaseModel):
    id: str
    score: float
    comment: str | None = None


class ExamOverrideIn(BaseModel):
    grades: list[ExamGradePatchIn] | None = None
    personality: dict[str, Any] | None = None
    summary: str | None = None
    overall: float | None = None
    note: str | None = None


class ExamSchedulesOut(BaseModel):
    model_config = _free()

    company_id: str
    schedules: list[ExamScheduleOut] = []
    created: dict[str, Any] = {}
