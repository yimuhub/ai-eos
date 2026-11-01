"""「老板批准」这条线上的 API 响应模型（A3 长尾：``/intervention`` + ``/experts``）。

分工同 `app/schemas/responses_ops.py`：按模块分文件，避免并行改动撞车，
所以路由请 `from app.schemas.responses_gate import ...`，不要往 `__init__` 里加。

## 字段来自哪里

* `/intervention` 十个端点的返回全是函数体里现拼的 dict，逐个对着源码写，
  并用 `tests/test_expert_intervention_contract.py` 在临时 SQLite 库上真跑对账。
* `/experts` 的列表/详情/编辑/复制/新建共用 `ExpertTemplate.public_dict()`
  （17 键），所以这里也只有一个 `ExpertPublicOut`，其余模型继承或嵌套它——
  public_dict 加一个键，这些端点会一起红，而不是七处各处漏。

## 三处刻意保留的宽松

* `InterventionPolicyOut` 合并了「库里有策略」和「没策略回默认」两条分支：
  前者带 `id` 不带 `is_default`，后者相反。两个字段都给默认值 + 路由带
  `response_model_exclude_unset=True`，没走到的分支不会凭空多出 `null` 键，
  前端 `policy.is_default` 与 `policy.id` 的语义和收口前逐字节一致。
  同理 `ApprovalGateOut`（自动放行 vs 落单待批）与 `DecideOut`（可选 `execution`）。
* `ApprovalExecutionOut.result` 保持 `Any`：它装的是外部 Agent 的整包返回或
  本机命令的 stdout，结构由第三方决定，写成具体类型会把 200 变成 500。
* `ExpertSearchHitOut` 是 `ExpertPickOut | ExpertSearchMissOut` 的联合：
  LLM 命中时返回完整人设 + `reason`，解析失败时返回
  `{"id": null, "message": "请改关键词重试"}`。两个模型的必填字段互斥
  （一个要 `name`、一个要 `message`），所以联合不会误判。这条不是凑数的：
  写成单一模型就得把 17 个字段全可选，前端拿到 `id: null` 的卡片会渲染成空白。

`ApprovalRequestOut.payload` / `OverrideLogOut.original|override` 保持 `dict | None`：
它们是 Agent 自由写入的 JSON 列，任何具体 schema 都会在脏数据上炸。

全部模型 `extra="allow"`：字段判断错时多余键原样透出，不会被序列化静默裁掉。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


# ---------------------------------------------------------------- intervention


class InterventionPolicyOut(BaseModel):
    """`GET|PUT /intervention/policy`。

    `is_default` 只在"公司没配过策略"那条分支出现；`id` 只在真实策略行出现。
    路由带 `response_model_exclude_unset=True` 让两者互不打扰。
    """

    model_config = _free()

    id: str | None = None
    company_id: str
    project_id: str | None
    level: int
    approval_actions: list[str] | None
    is_default: bool = False


class ActionCheckOut(BaseModel):
    """`POST /intervention/check`：这个动作在该公司的当前档位下要不要审批。"""

    model_config = _free()

    required: bool
    level: int


class ApprovalGateOut(BaseModel):
    """`POST /intervention/requests`：两个分支——自动放行 / 已落单待批。

    自动放行带 `detail`（老板看得懂的为什么），落单带 `request_id` + `status`。
    """

    model_config = _free()

    approved: bool
    auto: bool
    detail: str | None = None
    request_id: str | None = None
    status: str | None = None


class ApprovalRequestOut(BaseModel):
    """`GET /intervention/requests` 的一行（待审批队列）。"""

    model_config = _free()

    id: str
    action_type: str
    status: str
    reason: str | None
    requested_by: str | None
    payload: dict | None


class ApprovalExecutionOut(BaseModel):
    """批准挂起的高危动作后自动补执行的结果。"""

    model_config = _free()

    ok: bool
    result: Any | None = None
    error: str | None = None


class DecideOut(BaseModel):
    """`POST /intervention/requests/{id}/decide`。

    `execution` 只有"批准 + 该单挂了高危副作用"时才有；并发下第二个点击者
    拿到的 409 不走这个模型（那是错误信封）。
    """

    model_config = _free()

    id: str
    status: str
    execution: ApprovalExecutionOut | None = None


class OverrideOut(BaseModel):
    """`POST /intervention/override`：`target` 是 `类型:id 前 8 位` 的短标签。"""

    model_config = _free()

    id: str
    target: str


class OverrideLogOut(BaseModel):
    """`GET /intervention/overrides` 的一行（人工改判审计）。"""

    model_config = _free()

    id: str
    target_type: str
    target_id: str
    original: dict | None
    override: dict | None
    reason: str | None
    created_at: str


class WhitelistOut(BaseModel):
    """`GET /intervention/whitelist` 的一行（V5.6.1 工具级免批）。"""

    model_config = _free()

    id: str
    capability: str
    param_pattern: str
    note: str | None
    created_at: str | None


class WhitelistDeletedOut(BaseModel):
    model_config = _free()

    id: str
    deleted: bool


# --------------------------------------------------------------------- experts


class ExpertPublicOut(BaseModel):
    """`ExpertTemplate.public_dict()` 的镜像——专家库所有列表/卡片的共同形状。

    `description` 在端点里已截到 300 字、`deliverables` 截到 8 条，
    `prompt_chars`/`has_raw_md` 是"正文有多长/有没有原文"的替代指标，
    列表页靠它们决定要不要显示「查看完整档案」，所以三个都是必填。
    """

    model_config = _free()

    id: str
    source: str
    source_url: str | None
    source_slug: str
    name: str
    emoji: str | None
    color: str | None
    license: str | None
    department: str
    tags: list[str]
    description: str
    deliverables: list[str]
    recommended_packs: list[str]
    enabled: bool
    hired_count: int
    prompt_chars: int
    has_raw_md: bool


class ExpertDetailOut(ExpertPublicOut):
    """详情 / 编辑 / 复制 / 手动新建的回执：人设正文另带 `raw_md` + `system_prompt`。"""

    raw_md: str
    system_prompt: str


class ExpertDepartmentOut(BaseModel):
    """`GET /experts/departments`：部门目录计数（下拉筛选用）。"""

    model_config = _free()

    department: str
    count: int


class ExpertPickOut(ExpertPublicOut):
    """`POST /experts/search` 命中的一条：人设 + LLM 给的一句匹配理由。"""

    reason: str


class ExpertSearchMissOut(BaseModel):
    """`POST /experts/search` 没谈成：LLM 返回不可解析时的一条降级提示。"""

    model_config = _free()

    id: str | None = None
    message: str


class ExpertImportOut(BaseModel):
    """`POST /experts/import`：从 vendor 目录全量导入的回执。"""

    model_config = _free()

    imported_or_updated: int
    skipped: int
    files: int


class ExpertOriginalImportOut(BaseModel):
    """`POST /experts/import-original`：原创库替换 + 旧上游下架的计数。

    前端直接把 `created`/`updated`/`disabled_legacy` 拼成一句提示，
    所以这三个键缺一个就是页面上一条 `undefined`。
    """

    model_config = _free()

    imported_or_updated: int
    created: int
    updated: int
    skipped: int
    files: int
    disabled_legacy: int
    note: str


class ExpertTeamJobOut(BaseModel):
    """组队结果的一位成员：人设 + 分工 + 新建的工厂蓝图 Job id。"""

    model_config = _free()

    template: ExpertPublicOut
    task: str
    job_id: str


class ExpertTeamOut(BaseModel):
    """`POST /experts/team`。`team_size` 等于 `jobs` 长度（LLM 选空时为 0）。"""

    model_config = _free()

    goal: str
    team_size: int
    jobs: list[ExpertTeamJobOut]
    note: str


class ExpertBlueprintOut(BaseModel):
    """`POST /experts/{eid}/to-blueprint`：雇佣走的是工厂确认链，所以回 job id。"""

    model_config = _free()

    job_id: str
    template: ExpertPublicOut
    note: str
