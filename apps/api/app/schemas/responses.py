"""API 响应模型（Pydantic v2）。

集中定义「核心新端点」的真实响应结构，供路由通过 ``response_model`` 声明，
让 OpenAPI 里的 200 响应不再是空 schema。字段全部来自对应路由的**实际
返回**（逐个核对过），不追求覆盖全部端点，但求声明即真实。

约定：
* 可空字段写成 ``X | None``（无默认值 = 必须出现，可为 null），
  这样既保持「字段始终存在」的旧契约，又能在缺字段时暴露回归；
* 日期字段用 ``datetime``（FastAPI 统一序列化为 ISO 字符串），
  路由已提前 ``isoformat()`` 的则用 ``str``；
* 枚举在路由里已经转成 ``.value`` 字符串，这里用 ``str`` 声明实际类型。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# 订阅 / 试用（app/api/routes/subscription.py）
# ---------------------------------------------------------------------------
class SubscriptionPeriodOut(BaseModel):
    start: str | None
    end: str | None


class SubscriptionLimitsOut(BaseModel):
    seats: int | None
    monthly_tokens: int | None


class SubscriptionOut(BaseModel):
    company_id: str
    plan_code: str
    status: str
    effective_status: str
    trial_ends_at: str | None
    period: SubscriptionPeriodOut
    limits: SubscriptionLimitsOut
    period_start: str | None
    period_end: str | None
    seats_limit: int | None
    monthly_token_quota: int | None


# ---------------------------------------------------------------------------
# BYOK 密钥库（app/api/routes/byok.py）
# ---------------------------------------------------------------------------
class ByokSecretOut(BaseModel):
    name: str
    created_at: str | None
    updated_at: str | None


class ByokUpsertOut(BaseModel):
    name: str
    updated_at: str | None
    rotated: bool


class ByokDeletedOut(BaseModel):
    deleted: str


class ByokVerifyOut(BaseModel):
    name: str
    configured: bool


# ---------------------------------------------------------------------------
# 公司大脑检索（app/api/routes/company_search.py）
# ---------------------------------------------------------------------------
class SearchItemOut(BaseModel):
    id: str
    title: str
    snippet: str
    href: str
    # 仅 hybrid 模式带分数；keyword 模式用 exclude_none 保持"没有该字段"的旧契约
    score: float | None = None


class SearchGroupOut(BaseModel):
    type: str
    label: str
    items: list[SearchItemOut]


class SearchOut(BaseModel):
    query: str
    groups: list[SearchGroupOut]


# ---------------------------------------------------------------------------
# 任务收件箱（app/api/routes/inbox.py）
# ---------------------------------------------------------------------------
class InboxItemOut(BaseModel):
    id: str
    task_id: str
    title: str
    status: str
    agent_id: str | None
    agent_name: str | None
    updated_at: datetime
    artifact_id: str | None
    artifact_status: str | None


class InboxCountsOut(BaseModel):
    in_progress: int
    awaiting_acceptance: int
    completed: int
    failed: int


class InboxOut(BaseModel):
    company_id: str
    counts: InboxCountsOut
    in_progress: list[InboxItemOut]
    awaiting_acceptance: list[InboxItemOut]
    completed: list[InboxItemOut]
    failed: list[InboxItemOut]


# ---------------------------------------------------------------------------
# 持久队列（app/api/routes/queue.py）
# ---------------------------------------------------------------------------
class QueueItemOut(BaseModel):
    id: str
    kind: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error: str | None
    created_at: datetime


class QueueStatsOut(BaseModel):
    company_id: str
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int
    dead: int
    total: int
    oldest_queued_seconds: float


class QueueCancelOut(BaseModel):
    detail: str
    id: str
    status: str


# ---------------------------------------------------------------------------
# 交付验收 / 退回 / 评论（app/api/routes/artifacts.py）
# ---------------------------------------------------------------------------
class ArtifactAcceptOut(BaseModel):
    id: str
    already: bool
    status: str
    accepted_by: str | None
    accepted_at: datetime | None


class ArtifactDeclineOut(BaseModel):
    id: str
    status: str
    comment: str | None


class ArtifactCommentOut(BaseModel):
    id: str
    artifact_id: str
    author_type: str
    author_id: str | None
    body: str
    created_at: datetime


class ArtifactCommentDeletedOut(BaseModel):
    id: str
    deleted: bool


# ---------------------------------------------------------------------------
# 会话 / 项目归属（app/api/routes/messages.py）
# ---------------------------------------------------------------------------
class ConversationOut(BaseModel):
    id: str
    company_id: str
    task_id: str | None
    project_id: str | None
    title: str | None
    participants: list[str] | None
    created_at: datetime
    updated_at: datetime


class ConversationMessageOut(BaseModel):
    id: str
    sender_type: str
    sender_id: str | None
    receiver_id: str | None
    message_type: str
    content: str
    created_at: str


class ConversationDetailOut(ConversationOut):
    messages: list[ConversationMessageOut]


# ---------------------------------------------------------------------------
# Agent 档案（app/api/routes/agents.py）
# ---------------------------------------------------------------------------
class AgentModelBindingOut(BaseModel):
    """Agent 的模型绑定（列表内联的 {role, model_id}）。"""

    role: str
    model_id: str


class AgentOut(BaseModel):
    """Agent 全列（create/get/update 直接返回 ORM 行）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    department_id: str | None
    name: str
    avatar: str | None
    role: str | None
    description: str | None
    system_prompt: str | None
    status: str
    manager_agent_id: str | None
    memory_config: dict | None
    doc_style_id: str | None
    expert_template_id: str | None
    visibility: str | None
    created_by_user_id: str | None
    data_scope: dict | None
    created_at: datetime
    updated_at: datetime


class AgentListItemOut(AgentOut):
    """列表额外带一次查出的模型绑定（避免前端 N+1）。"""

    model_configs: list[AgentModelBindingOut]


class AgentPendingAuthOut(BaseModel):
    id: str
    action: str
    since: str | None


class AgentNeedsAuthOut(BaseModel):
    high_risk_actions: list[str]
    policy: str
    pending: list[AgentPendingAuthOut]


class AgentCardOut(BaseModel):
    agent_id: str
    name: str
    role: str | None
    description: str | None
    status: str
    department_id: str | None
    company_id: str
    capabilities: list[str] | None
    computer_capabilities: list[str]
    needs_auth: AgentNeedsAuthOut
    card_version: str


class AgentSkillBoundOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    slug: str
    name: str
    source: str
    reason: str


class AgentSkillProvisionOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    bound: list[AgentSkillBoundOut]
    library: int
    market: int
    generated: int
    fallback: int
    errors: list[str]


class AgentBatchDeleteFailureOut(BaseModel):
    id: str
    reason: str


class AgentBatchDeleteOut(BaseModel):
    deleted: list[str]
    deleted_count: int
    failed: list[AgentBatchDeleteFailureOut]


class AgentCreatedBriefOut(BaseModel):
    id: str
    name: str
    role: str | None


class AgentBatchCreateOut(BaseModel):
    created: list[AgentCreatedBriefOut]
    created_count: int


class AgentManagerOut(BaseModel):
    id: str
    manager_agent_id: str | None


class AgentSubordinateOut(BaseModel):
    id: str
    name: str
    role: str | None
    status: str


class AgentAssignFailureOut(BaseModel):
    agent_id: str
    reason: str


class AgentBatchAssignManagerOut(BaseModel):
    updated: int
    failed: list[AgentAssignFailureOut]


class AgentModelConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    role: str
    model_id: str
    created_at: datetime
    updated_at: datetime


class AgentSwitchModelOut(BaseModel):
    role: str
    model_id: str


class CapabilityPackOut(BaseModel):
    key: str
    label: str
    description: str
    risk: str | None = None
    tools: list[str] | None = None
    system_prompt: str | None = None
    engines: list[str] | None = None
    defaults: dict | None = None


class AgentBackendOut(BaseModel):
    key: str
    label: str
    description: str
    adapter: str | None = None


class CapabilityPacksOut(BaseModel):
    packs: list[CapabilityPackOut]
    backends: list[AgentBackendOut]


# ---------------------------------------------------------------------------
# 任务中心（app/api/routes/tasks.py）
# ---------------------------------------------------------------------------
class TaskBaseOut(BaseModel):
    """Task 全列（summary 只少一个 result）。"""

    id: str
    company_id: str
    project_id: str | None
    parent_task_id: str | None
    title: str
    description: str | None
    status: str
    priority: str
    assigned_agent_id: str | None
    created_by: str
    started_at: str | None
    completed_at: str | None
    progress: float | None
    current_stage: str | None
    auto_start: bool | None
    created_at: datetime
    updated_at: datetime


class TaskOut(TaskBaseOut):
    result: str | None


class TaskExecutionBriefOut(BaseModel):
    id: str
    agent_id: str
    status: str
    output: str | None
    error: str | None
    # agent_runtime 写入的是 [{name, ok, arg}]（不是工具名字符串列表）
    tools_used: list[dict]


class TaskExecutionSummaryOut(TaskExecutionBriefOut):
    output_truncated: bool


class TaskEventBriefOut(BaseModel):
    type: str
    actor_type: str
    payload: dict | None
    at: str


class TaskInterventionBriefOut(BaseModel):
    id: str
    type: str
    content: str | None
    status: str


class TaskDetailOut(TaskOut):
    subtasks: list[str]
    executions: list[TaskExecutionBriefOut]
    events: list[TaskEventBriefOut]
    interventions: list[TaskInterventionBriefOut]


class TaskSummaryOut(TaskBaseOut):
    subtasks: list[str]
    executions: list[TaskExecutionSummaryOut]
    executions_total: int
    events: list[TaskEventBriefOut]
    interventions: list[TaskInterventionBriefOut]


class TaskExecutionOut(BaseModel):
    """agent_executions 全列（GET /tasks/{tid}/executions）。"""

    id: str
    agent_id: str
    task_id: str
    project_id: str | None
    status: str
    input: str | None
    output: str | None
    error: str | None
    metadata_json: dict | None
    completed_at: str | None
    created_at: datetime
    updated_at: datetime


class TaskStartedOut(BaseModel):
    detail: str
    task_id: str


class TaskActionOut(BaseModel):
    detail: str


class TaskTakeoverOut(BaseModel):
    detail: str
    paused_executions: int
    resume_via: str


class TaskInterventionAddedOut(BaseModel):
    id: str
    status: str
    applied: dict


class TaskTemplateOut(BaseModel):
    id: str
    name: str


class LogEntryOut(BaseModel):
    ts: str
    kind: str
    who: str
    type: str
    title: str
    content: str


class LogsOut(BaseModel):
    count: int
    logs: list[LogEntryOut]


# ---------------------------------------------------------------------------
# 项目管理（app/api/routes/projects.py）
# ---------------------------------------------------------------------------
class ProjectOut(BaseModel):
    id: str
    company_id: str
    name: str
    description: str | None
    status: str
    priority: str
    owner_id: str
    manager_agent_id: str | None
    start_date: str | None
    target_date: str | None
    # ProjectIn 允许 attachments=None，路由会原样落库（老行为：新建时可为 null）
    attachments: list[dict] | None
    created_at: datetime
    updated_at: datetime


class ProjectListItemOut(ProjectOut):
    task_total: int
    task_completed: int
    progress: int


class MilestoneOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str | None
    status: str
    target_date: str | None
    completed_at: str | None
    created_at: datetime
    updated_at: datetime


class ProjectMemberOut(BaseModel):
    member_type: str
    member_id: str
    role_in_project: str | None


class ProjectTaskBriefOut(BaseModel):
    id: str
    title: str
    status: str
    priority: str
    assigned_agent_id: str | None


class ProjectAssetBriefOut(BaseModel):
    id: str
    name: str
    type: str


class ProjectDetailOut(ProjectOut):
    milestones: list[MilestoneOut]
    members: list[ProjectMemberOut]
    tasks: list[ProjectTaskBriefOut]
    recent_assets: list[ProjectAssetBriefOut]


class ProjectProgressOut(BaseModel):
    total_tasks: int
    completed: int
    running: int
    percent: int
    milestones_total: int
    milestones_done: int
    manager_agent_id: str | None


class ProjectDeletedOut(BaseModel):
    id: str
    name: str


class ProjectDeleteFailureOut(BaseModel):
    id: str
    reason: str


class ProjectBatchDeleteOut(BaseModel):
    deleted_count: int
    deleted: list[ProjectDeletedOut]
    failed: list[ProjectDeleteFailureOut]


class DetailMessageOut(BaseModel):
    detail: str


class ProjectChatActionOut(BaseModel):
    type: str
    detail: str


class ProjectChatOperationOut(BaseModel):
    reply: str
    actions: list[ProjectChatActionOut]


class ProjectDeliverableOut(BaseModel):
    id: str
    name: str
    type: str
    size: int
    version: int
    created_by: str | None
    creator_type: str | None
    created_at: str


# ---------------------------------------------------------------------------
# 交付中心（app/api/routes/delivery.py）
# ---------------------------------------------------------------------------
class DeliveryFileOut(BaseModel):
    id: str
    name: str
    task_id: str | None
    project_id: str | None
    agent_id: str | None
    size: int
    mime_type: str | None
    exists: bool
    created_at: str


class DeliveryAgentFolderOut(BaseModel):
    agent_id: str
    name: str
    role: str | None
    file_count: int
    total_size: int
    latest: str | None
    files: list[DeliveryFileOut]


class DeliveryByAgentOut(BaseModel):
    folders: list[DeliveryAgentFolderOut]


class DeliveryProjectGroupOut(BaseModel):
    key: str
    name: str
    is_project: bool
    file_count: int
    total_size: int
    files: list[DeliveryFileOut]


class DeliveryByProjectOut(BaseModel):
    groups: list[DeliveryProjectGroupOut]


class DeliveryTaskGroupOut(BaseModel):
    key: str
    name: str
    project_id: str | None
    file_count: int
    total_size: int
    files: list[DeliveryFileOut]


class DeliveryByTaskOut(BaseModel):
    groups: list[DeliveryTaskGroupOut]


class DeliveryFilesOut(BaseModel):
    files: list[DeliveryFileOut]


class DeliveryAgentBriefOut(BaseModel):
    id: str
    name: str


class DeliveryAgentFolderDetailOut(BaseModel):
    agent: DeliveryAgentBriefOut
    files: list[DeliveryFileOut]


# ---------------------------------------------------------------------------
# 考核（app/api/routes/review.py）
# ---------------------------------------------------------------------------
class ReviewCycleOut(BaseModel):
    id: str
    name: str


class ReviewSubmittedOut(BaseModel):
    id: str
    score: float


class ReviewOut(BaseModel):
    id: str
    source: str
    score: float
    strengths: list[str] | None
    weaknesses: list[str] | None
    suggestions: list[str] | None
    comment: str | None
    evidence: dict | None
    created_at: str


class AgentMetricsOut(BaseModel):
    """performance_engine.compute_agent_metrics 的完整返回。"""

    agent_id: str
    tasks_total: int
    tasks_completed: int
    tasks_failed: int
    completion_rate: float | None
    avg_duration_s: float | None
    meeting_spoke_count: int
    messages_sent: int
    score: float
    cost_total: float
    cost_per_task: float
    output_value_est: float
    roi: float | None


class AutoReviewOut(BaseModel):
    agent_id: str
    automated_score: float
    metrics: AgentMetricsOut
    strengths: list[str]
    weaknesses: list[str]


# ---------------------------------------------------------------------------
# 调度（app/api/routes/schedules.py）
# ---------------------------------------------------------------------------
class ScheduleOut(BaseModel):
    id: str
    name: str
    trigger_type: str
    trigger_config: dict | None
    action_type: str
    action_params: dict | None
    status: str
    next_run_at: str | None
    last_run_at: str | None
    run_count: int
    attachments: list


class ScheduleStateOut(BaseModel):
    id: str
    status: str


class ScheduleRunOut(BaseModel):
    id: str
    status: str
    result: dict | None
    error: str | None
    started_at: str
    finished_at: str | None


class ScheduleTickOut(BaseModel):
    executed: int


# ---------------------------------------------------------------------------
# 技能中心（app/api/routes/skills.py）
# ---------------------------------------------------------------------------
class SkillOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    version: str
    input_schema: dict | None
    output_schema: dict | None
    permissions: list[str] | None
    status: str
    scope: str
    company_id: str | None
    source: str
    source_label: str
    created_at: datetime
    updated_at: datetime


class CustomSkillOut(BaseModel):
    id: str
    slug: str
    name: str
    scope: str


class SecurityScanOut(BaseModel):
    safe: bool
    hits: list[str]
    level: str


class SkillImportOut(BaseModel):
    id: str
    slug: str
    name: str
    scope: str
    security_scan: SecurityScanOut


class SkillMarketInstallOut(BaseModel):
    id: str
    slug: str
    name: str
    security_scan: SecurityScanOut


class SkillMarketItemOut(BaseModel):
    name: str
    repo: str
    path: str | None
    url: str | None
    source_market: str
    raw_url: str


class SkillMarketSearchOut(BaseModel):
    results: list[SkillMarketItemOut]
    total: int


class SkillPreviewOut(BaseModel):
    content: str
    security_scan: SecurityScanOut


class SkillScopeOut(BaseModel):
    ok: bool
    slug: str
    scope: str


class SkillInstallOut(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# 学习中心（app/api/routes/learning.py）
# ---------------------------------------------------------------------------
class LearningProfileCoreOut(BaseModel):
    id: str
    identity: str
    capabilities: list
    preferences: dict
    learning_goals: list
    version: int


class LearningStatsOut(BaseModel):
    tasks_total: int
    tasks_completed: int
    tasks_failed: int
    meeting_spoke_count: int


class CapabilityHistoryOut(BaseModel):
    version: int
    capabilities: list | None
    reason: str | None
    created_at: str


class GrowthEventBriefOut(BaseModel):
    type: str
    title: str
    detail: dict | None
    source_type: str | None
    created_at: str


class LearningRecordBriefOut(BaseModel):
    id: str
    goal: str
    status: str
    progress: int


class LearningProfileOut(BaseModel):
    profile: LearningProfileCoreOut
    stats: LearningStatsOut
    capability_history: list[CapabilityHistoryOut]
    growth_events: list[GrowthEventBriefOut]
    learning_records: list[LearningRecordBriefOut]


class ProfilePatchOut(BaseModel):
    # 无变更时路由只返回 {"changed": false}，配合 exclude_none 保持旧契约
    changed: bool
    version: int | None = None


class LearningRecordCreatedOut(BaseModel):
    id: str
    status: str


class LearningRecordUpdatedOut(BaseModel):
    id: str
    progress: int
    status: str


class GrowthEventOut(GrowthEventBriefOut):
    id: str


# ---------------------------------------------------------------------------
# 绩效（app/api/routes/performance.py）
# ---------------------------------------------------------------------------
class CompanyRefOut(BaseModel):
    id: str


class HeadcountOut(BaseModel):
    total: int
    by_status: dict[str, int]


class AgentCostOut(BaseModel):
    agent: str
    tokens: int
    cost: float


class CostMonthOut(BaseModel):
    tokens: int
    cost: float
    top: list[AgentCostOut]


class AgentOutputOut(BaseModel):
    agent: str
    tasks: int


class Output30dOut(BaseModel):
    total_tasks: int
    top: list[AgentOutputOut]


class AssessmentOut(BaseModel):
    agent: str
    score: float


class HrOverviewOut(BaseModel):
    company: CompanyRefOut
    headcount: HeadcountOut
    cost_month: CostMonthOut
    output_30d: Output30dOut
    assessment_top: list[AssessmentOut]
    health: float


class RankedAgentOut(AgentMetricsOut):
    agent_name: str


class SnapshotOut(BaseModel):
    ranked: list[RankedAgentOut]


class PerformanceHistoryItemOut(BaseModel):
    id: str
    agent_id: str
    score: float
    metrics: dict | None
    period_end: str


# ---------------------------------------------------------------------------
# 复盘（app/api/routes/reflection.py）
# ---------------------------------------------------------------------------
class LessonOut(BaseModel):
    id: str
    lesson: str
    tags: list | None
    created_at: str


class LessonDeletedOut(BaseModel):
    ok: bool
    deleted_id: str


class ReflectionProposalOut(BaseModel):
    id: str
    title: str
    status: str
    type: str | None  # improvement_proposals.proposal_type 可空


class ReflectionOut(BaseModel):
    id: str
    trigger: str
    went_well: list | None
    what_failed: list | None
    root_cause: str | None
    created_at: str
    proposals: list[ReflectionProposalOut]


class ProposalDecisionOut(BaseModel):
    id: str
    status: str
    applied: bool


class AgentOverviewItemOut(BaseModel):
    id: str
    name: str
    role: str | None
    status: str
    lesson_count: int


class DepartmentOverviewOut(BaseModel):
    dept_id: str
    dept_name: str
    agents: list[AgentOverviewItemOut]


class AgentsOverviewOut(BaseModel):
    departments: list[DepartmentOverviewOut]
