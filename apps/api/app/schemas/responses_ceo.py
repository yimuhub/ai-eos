"""CEO（超级 Agent）域的响应模型。

字段对着 `app/ceo_engine.py` 的返回逐条写，并在
`tests/test_ceo_contract.py` 里用临时库真跑端点做键集合相等断言。
全部 `extra="allow"`：判断错时多余键原样透出，不被序列化静默裁掉。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


class CeoAgentOptionOut(BaseModel):
    model_config = _free()

    id: str
    name: str
    role: str | None = None


class CeoConfigOut(BaseModel):
    model_config = _free()

    company_id: str
    has_ceo: bool
    ceo_agent_id: str
    ceo_agent_name: str
    candidates: list[CeoAgentOptionOut]


class CeoAssignOut(BaseModel):
    model_config = _free()

    company_id: str
    ceo_agent_id: str
    ceo_agent_name: str


class CeoReleaseOut(BaseModel):
    model_config = _free()

    company_id: str
    released_agent_id: str
    released_agent_name: str
    restored_name: str


class CeoPlannedSubtaskOut(BaseModel):
    model_config = _free()

    index: int
    title: str
    description: str
    agent_id: str
    agent_name: str
    agent_matched: bool
    # 指向 new_agents 的下标（预览阶段把子任务和「待招的人」对上）
    new_agent_index: int | None = None
    # 完成该子任务必须具备的技能（供自动配备与前端展示）
    required_skills: list[str] = []


class CeoNewAgentOut(BaseModel):
    """CEO 提议/已招的新员工。

    开工前是提议（`agent_id` 为空），开工后回填真实 id；
    两者共用一套字段，前端只按 `agent_id` 是否为空区分阶段。
    """

    model_config = _free()

    name: str
    role: str = ""
    department: str = ""
    group: str = ""
    reason: str = ""
    agent_id: str = ""
    proposed_name: str = ""
    # 完成岗位任务所需的技能名（计划阶段由 CEO 给出）
    required_skills: list[str] = []
    # 已自动配备的技能（入职后回填真实绑定结果）
    skills: list[dict] = []
    skill_errors: list[str] = []


class CeoMissionPreviewOut(BaseModel):
    model_config = _free()

    mission_id: str
    title: str
    status: str
    objective: str
    deliverable: str
    redlines: list[str]
    acceptance: list[str]
    terminology: dict[str, str]
    extra_instructions: list[str]
    anchor_version: int
    subtasks: list[CeoPlannedSubtaskOut]
    subtask_count: int
    new_agents: list[CeoNewAgentOut]
    coordinator_agent_id: str
    coordinator_agent_name: str
    max_parallel: int
    effective_parallel: int
    provider_cap: int
    provider_type: str
    min_parallel: int
    hard_cap: int


class CeoLaunchOut(BaseModel):
    model_config = _free()

    mission_id: str
    status: str
    subtask_count: int
    max_parallel: int
    effective_parallel: int
    provider_cap: int
    provider_type: str
    new_agents: list[CeoNewAgentOut]
    workflow: dict[str, Any] | None = None


class CeoSlotOut(BaseModel):
    model_config = _free()

    task_id: str
    title: str
    status: str
    agent_id: str
    agent_name: str
    progress: float
    stage: str
    off_track: bool
    result_excerpt: str
    started_at: str | None = None
    completed_at: str | None = None


class CeoMissionDetailOut(BaseModel):
    model_config = _free()

    mission_id: str
    title: str
    instruction: str
    status: str
    objective: str
    deliverable: str
    redlines: list[str]
    acceptance: list[str]
    terminology: dict[str, str]
    extra_instructions: list[str]
    anchor_version: int
    coordinator_agent_id: str
    coordinator_agent_name: str
    subtask_count: int
    new_agents: list[CeoNewAgentOut]
    completed_count: int
    failed_count: int
    off_track_count: int
    retry_count: int
    max_parallel: int
    effective_parallel: int
    provider_cap: int
    degraded: bool
    degrade_reason: str
    degraded_until: str
    workflow_id: str
    stats: dict[str, Any]
    report_excerpt: str
    started_at: str
    finished_at: str
    slots: list[CeoSlotOut]


class CeoMissionListItemOut(BaseModel):
    model_config = _free()

    mission_id: str
    title: str
    status: str
    subtask_count: int
    completed_count: int
    failed_count: int
    off_track_count: int
    coordinator_agent_id: str
    coordinator_agent_name: str
    effective_parallel: int
    degraded: bool
    created_at: str
    finished_at: str


class CeoReportOut(BaseModel):
    model_config = _free()

    mission_id: str
    title: str
    status: str
    report: str
    artifact_id: str | None = None
    finished_at: str
    stats: dict[str, Any]


class CeoStatusOut(BaseModel):
    model_config = _free()

    mission_id: str
    status: str


class CeoCancelOut(BaseModel):
    model_config = _free()

    mission_id: str
    status: str
    cancelled_tasks: int


class CeoRetryOut(BaseModel):
    model_config = _free()

    mission_id: str
    status: str
    retry_tasks: int


class CeoInstructionOut(BaseModel):
    model_config = _free()

    mission_id: str
    anchor_version: int
    extra_instructions: list[str]


class CeoCapacityOut(BaseModel):
    model_config = _free()

    requested: int
    provider_cap: int
    provider_type: str
    worker_free: int
    effective: int
    min_parallel: int
    hard_cap: int
    default_parallel: int


class CeoDigestOut(BaseModel):
    model_config = _free()

    company_id: str
    generated_at: str
    window_hours: int
    markdown: str
    stats: dict[str, Any]


class CeoDigestScheduleOut(BaseModel):
    model_config = _free()

    schedule_id: str
    trigger_config: dict[str, Any]
    created: bool
