"""长尾模块的 API 响应模型（A3 长尾：war_room / artifacts / models_api）。

与 `app/schemas/responses.py` 的分工：那边收口核心业务路由，这边只放本轮
补的三个模块。刻意分文件是为了让并行改不同模块的 agent 不撞同一个文件，
因此路由请 `from app.schemas.responses_ops import ...`（不要往 __init__ 里加）。

## 字段来自哪里

每个模型都对照过**真实返回**：用临时 SQLite 库（`/tmp` 下的全新文件）+
进程内 ASGI 客户端把端点逐个跑通，打印返回骨架后逐个落字段，再和端点函数体、
它调用的 service 返回结构交叉验证。没有一处是「照着命名猜」的。

## 统一约定

* 全部 `model_config = ConfigDict(extra="allow")`：即使某个字段判断错了，
  多余字段也不会被 response_model 静默裁掉（这是 A3 最大的线上风险）。
* 分支返回（同一端点不同状态返回不同键，如 lifecycle / publish / test）里
  **只出现在某一支的字段一律给默认值**（`= None`），否则 FastAPI 在另一支
  会抛 ResponseValidationError，把 200 变成 500。这类端点的路由同时带
  `response_model_exclude_unset=True`：没走到的分支不会凭空多出 null 键，
  线上去掉模型后逐字节一致（已用对照组实测）。
* 可空但始终存在的字段写成 `X | None`（不给默认值）。
* datetime 用 `datetime`；端点里已经 `str(...)` / `isoformat()` 过的用 `str`；
  枚举在路由里都转成了 `.value`，所以用 `str`。
* 上游/用户可写入的自由结构（`Model.config`、`WarRoomSession.analysis`）
  保持 `dict`；第三方供应商 `/models` 返回的 id 列表保持裸 `list`，
  因为 `list[str]` 会把「上游返回非字符串 id」从可用变成 500。

有意未建模型的端点（返回的不是 JSON 对象，或结构是任意用户输入）：
`GET /war-room/sessions/{sid}/file`（FileResponse 二进制）、
`GET /artifacts/{artifact_id}/download.zip`（StreamingResponse zip）、
`POST /models/env-key`（响应含密钥掩码片段，见该端点的安全备注）、
`GET|POST /models/convert-config`（把用户 POST 的任意值原样回显）。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# 作战室（app/api/routes/war_room.py）
# ---------------------------------------------------------------------------
class WarRoomTaskOut(BaseModel):
    """`_session_out()` 里的单条分工任务（实测键与顺序一致）。"""

    model_config = ConfigDict(extra="allow")

    id: str
    seq: int
    agent_id: str
    agent_name: str
    title: str
    detail: str | None
    announce_status: str
    announce_text: str | None
    leader_feedback: str | None
    status: str
    deliverable_name: str | None
    deliverable_path: str | None
    deliverable_note: str | None
    reviewed_by: str | None
    review_comment: str | None


class WarRoomSessionOut(BaseModel):
    """看板主体：`GET /war-room/{sid}` 与 `GET /war-room/sessions` 的会话项。"""

    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str | None
    goal: str
    status: str
    leader_agent_id: str
    leader_name: str
    # 负责人 LLM 分析原文（{"analysis": …, "tasks": […]} 或失败时 {"error": …}）
    analysis: dict | None
    deliverable_format: str | None
    deliverable_rule: str | None
    final_output_path: str | None
    tasks: list[WarRoomTaskOut]


class WarRoomSessionsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    sessions: list[WarRoomSessionOut]


class WarRoomKickoffOut(BaseModel):
    """kickoff 只做「建会话 + 投递后台分析」，分析结果靠看板轮询取。"""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    project_id: str | None


class WarRoomLifecycleOut(BaseModel):
    """pause/resume/stop 返回 status；delete 返回 deleted（两条分支各缺一半）。"""

    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str | None = None
    deleted: bool | None = None


class WarRoomBatchDeleteFailureOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    reason: str


class WarRoomBatchDeleteOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    deleted_count: int
    failed: list[WarRoomBatchDeleteFailureOut]


class WarRoomLeaderOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    name: str
    role: str | None


class WarRoomTeamMemberOut(BaseModel):
    """`round(done / n * 100)` 不带 ndigits → 实测是 int。"""

    model_config = ConfigDict(extra="allow")

    agent_id: str
    name: str
    role: str | None
    task_count: int
    approved: int
    progress_pct: int
    statuses: list[str]


class WarRoomOverallProgressOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_tasks: int
    announced: int
    acknowledged: int
    submitted: int
    approved: int
    pct: int


class WarRoomTeamOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    goal: str
    status: str
    deliverable_format: str | None
    deliverable_rule: str | None
    leader: WarRoomLeaderOut
    members: list[WarRoomTeamMemberOut]
    overall_progress: WarRoomOverallProgressOut


class WarRoomChatMessageOut(BaseModel):
    """路由里 `created_at` 已经 `str(...)` 过，所以这里仍是 str。"""

    model_config = ConfigDict(extra="allow")

    id: str
    role: str | None
    agent_name: str | None
    text: str | None
    created_at: str


class WarRoomChatListOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[WarRoomChatMessageOut]


class WarRoomChatBlockOut(BaseModel):
    """快照里的 chat 增量块：无消息时 latest_id 回落到 since_id 或 None。"""

    model_config = ConfigDict(extra="allow")

    messages: list[WarRoomChatMessageOut]
    incremental: bool
    latest_id: str | None


class WarRoomSnapshotOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    session: WarRoomSessionOut
    team: WarRoomTeamOut
    chat: WarRoomChatBlockOut


class WarRoomAnnouncementOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent: str
    text: str


class WarRoomAnnounceOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    announcements: list[WarRoomAnnouncementOut]


class WarRoomAcknowledgeOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    all_acknowledged: bool
    session_status: str


class WarRoomStartOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    dispatched: int


class WarRoomReviewOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str


class WarRoomBossSayOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    agents_responding: int


class WarRoomFinalizeOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    final_output: str
    merged_parts: int
    preview: str


# ---------------------------------------------------------------------------
# 成果协作（app/api/routes/artifacts.py）
# ---------------------------------------------------------------------------
class ArtifactCreatedOut(BaseModel):
    """注意：该端点没有 `status_code=201`，实测返回 200。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    status: str
    current_version: int
    task_id: str | None


class ArtifactListItemOut(BaseModel):
    """列表项用 `version`（详情用 `current_version`），命名不一致但这是真实契约。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    type: str
    status: str
    version: int
    project_id: str | None
    task_id: str | None
    accepted_at: datetime | None


class ArtifactVersionBriefOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int
    status: str
    change_note: str | None
    content_preview: str
    reviewed_by: str | None
    review_comment: str | None


class ArtifactDetailOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    type: str
    status: str
    current_version: int
    task_id: str | None
    accepted_by: str | None
    accepted_at: datetime | None
    comment_count: int
    versions: list[ArtifactVersionBriefOut]


class ArtifactVersionContentOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int
    content: str
    status: str


class ArtifactNewVersionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    artifact_id: str
    new_version: int
    status: str


class ArtifactSubmitOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    current_version: int


class ArtifactReviewDecisionOut(BaseModel):
    """决策人：`data.reviewer_agent_id or user.id`，两者都是字符串 id。"""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    reviewer: str


class ArtifactPublishOut(BaseModel):
    """发布有三种分支返回：

    * 直接发布：`{id, published_at, message}`
    * 已发布（幂等）：`{id, published_at, already}`
    * 需要老板二次确认：`{status, approval_id, message}` —— 这一支**没有 id**

    `published_at` 是路由里 `str(datetime)` 出来的（空格分隔，不是 ISO 的 T），
    所以这里保持 str，不敢改成 datetime。
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    published_at: str | None = None
    already: bool | None = None
    status: str | None = None
    approval_id: str | None = None
    message: str | None = None


class ArtifactPublishDecideOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    decision: str
    published: bool


class ArtifactDeletedOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    deleted: bool


class ArtifactSopOut(BaseModel):
    """SOP 交接单：`versions` 是「最近 3 版」的条数，不是版本号。"""

    model_config = ConfigDict(extra="allow")

    artifact_id: str
    title: str
    markdown: str
    versions: int


# ---------------------------------------------------------------------------
# 模型注册表（app/api/routes/models_api.py）
# ---------------------------------------------------------------------------
class ModelsDiagnosticsProviderOut(BaseModel):
    """安全约束：只有 `key_present` 这个布尔，绝不出现密钥本身。"""

    model_config = ConfigDict(extra="allow")

    provider_type: str
    default_env_var: str
    key_present: bool
    models_ready: int
    model_count: int
    last_test_ok: bool | None
    last_test_at: str | None


class ModelsDiagnosticsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_count: int
    usable_model_ids: list[str]
    ready: bool
    providers: list[ModelsDiagnosticsProviderOut]
    hint: str


class ModelConfigItemOut(BaseModel):
    """`config` 已过 `_safe_config()`：实测 `api_key` / `openai_api_key`
    这类密钥字段在返回前就被剔除，只剩 `api_key_env` 这种变量名引用。

    类型写成 `dict | list | None`：PATCH 端点把请求体里的 `config` 原样
    `setattr` 落库（不校验形状），万一老库里存的是 list，只声明 dict 会让
    列表接口在校验阶段 500。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    provider_type: str
    base_url: str | None
    model_id: str
    config: dict | None
    company_id: str | None
    unowned: bool


class ModelCreatedOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    provider_type: str
    company_id: str | None


class ModelUpdatedOut(BaseModel):
    """`claimed` 只在「未归属配置被认领」那一支出现。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    company_id: str | None
    claimed: bool | None = None


class ModelTestOut(BaseModel):
    """成功支给 reply/raw_model，失败支只给 error（上游异常原文，截断 500）。"""

    model_config = ConfigDict(extra="allow")

    status: str
    reply: str | None = None
    raw_model: str | None = None
    error: str | None = None


class ModelBindAgentsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_id: str
    role: str
    agent_count: int
    bound: int
    skipped: int


class ProviderModelsListOut(BaseModel):
    """`models` 保持裸 list：内容是第三方供应商 `/v1/models` 的 id 原文，
    写成 `list[str]` 会让「上游返回非字符串 id」从能用在 response_model
    校验阶段变成 500。
    """

    model_config = ConfigDict(extra="allow")

    status: str
    base_url: str | None = None
    models: list | None = None
    error: str | None = None


class ModelBatchImportOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    imported: list[str]
    imported_count: int
    skipped_existing: list[str]
