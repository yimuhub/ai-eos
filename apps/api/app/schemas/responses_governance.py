"""治理（governance）与用量计费（tokens）两个路由的响应模型。

字段全部来自临时 SQLite + 进程内 ASGI 客户端的**实测返回**（脚本见
/tmp/probe_governance.py、/tmp/probe_tokens.py），不是照函数签名猜的。

约定（与 app/schemas/responses.py 一致，另加一条）：
* 每个模型都带 ``extra="allow"``：字段判断若有遗漏，实测多出来的键仍会
  原样序列化出去，绝不因补模型而裁掉前端已能拿到的数据；
* 可空字段写成 ``X | None`` 且**不给默认值** = 该键必须出现、值可为 null，
  与端点当前的 ``{"k": None}`` 行为一致；
* 只有"同一端点按分支返回不同键集合"时才给字段加默认值（可缺键的分支）。
  这里**不用** ``response_model_exclude_none``：它会连嵌套里的 null 键一起丢，
  属于"裁字段"方向；宁可让缺值的键多出现一次 null。
* 端点已提前 ``isoformat()`` 的时间字段用 ``str``（返回的是字符串不是对象）。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# 自我改进治理（app/api/routes/governance.py · P3-13）
# ---------------------------------------------------------------------------


class ImprovementProposedOut(BaseModel):
    """POST /governance/improvements"""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str


class SandboxComparisonOut(BaseModel):
    """沙盒对比：baseline/candidate 由 Query 传入，delta 为二者之差。"""

    model_config = ConfigDict(extra="allow")

    baseline: float
    candidate: float
    delta: float


class SandboxTestOut(BaseModel):
    """POST /governance/improvements/{iid}/sandbox-test"""

    model_config = ConfigDict(extra="allow")

    case_id: str
    status: str
    comparison: SandboxComparisonOut


class ImprovementDeployOut(BaseModel):
    """POST /governance/improvements/{iid}/deploy。

    拒绝分支只返回 {id, status}，批准分支才带两个版本号；这里把后两个字段声明为
    可空且给默认值 —— 不裁字段（批准分支照常带值），最多是让拒绝分支多出两个
    null 键（前端两处都用真值判断，见 apps/web 的 governance 页）。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    deployed_version: int | None = None
    rollback_version: int | None = None


class AgentRollbackOut(BaseModel):
    """POST /governance/agents/{agent_id}/rollback/{version}"""

    model_config = ConfigDict(extra="allow")

    rolled_back_to: int


class ImprovementCaseOut(BaseModel):
    """GET /governance/self-improvement-cases 的单条提案。

    注意 ``expect_effect`` 是响应里的键名，ORM 字段叫 ``expected_impact``。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    agent_id: str
    what_improved: str | None
    why: str | None
    risk: str | None
    expect_effect: str | None
    status: str


class CaseDecisionOut(BaseModel):
    """PUT /governance/self-improvement-cases/{iid}/{decision}"""

    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str


# ---------------------------------------------------------------------------
# 内部市场（app/api/routes/governance.py · P3-14）
# ---------------------------------------------------------------------------


class PackagePublishedOut(BaseModel):
    """POST /governance/marketplace/packages"""

    model_config = ConfigDict(extra="allow")

    id: str
    key: str
    version: str


class MarketplacePackageOut(BaseModel):
    """GET /governance/marketplace/packages 的单条（键名 ``type`` 非 ``package_type``）。"""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    key: str
    name: str
    version: str
    permissions_required: list | None
    dependencies: list | None


class InstallConfirmationOut(BaseModel):
    """未确认权限时的确认清单（potential_risk 是后端固定文案）。"""

    model_config = ConfigDict(extra="allow")

    permissions_required: list | None
    dependencies: list | None
    potential_risk: str


class PackageInstallOut(BaseModel):
    """POST /governance/marketplace/packages/{pid}/install。

    同一状态码 201 下有两种形状：
    未确认 → {installed:false, requires_confirmation:{...}}
    已确认 → {installed:true, installation_id, package, version}
    """

    model_config = ConfigDict(extra="allow")

    installed: bool
    installation_id: str | None = None
    package: str | None = None
    version: str | None = None
    requires_confirmation: InstallConfirmationOut | None = None


class InstallationRollbackOut(BaseModel):
    """POST /governance/installations/{inst_id}/rollback"""

    model_config = ConfigDict(extra="allow")

    id: str
    rolled_back: bool


class InstallationOut(BaseModel):
    """GET /governance/installations 的单条安装记录。

    `package_key` 来自 MarketplacePackage.key（安装记录本身只有 package_id），
    治理中心「已安装」面板显示的就是它。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    package_id: str
    package_key: str
    package_name: str
    package_type: str
    version: str
    installed_by: str | None
    installed_at: str | None


# ---------------------------------------------------------------------------
# 企业模板（app/api/routes/governance.py · P3-15）
# ---------------------------------------------------------------------------


class TemplateOut(BaseModel):
    """GET /governance/templates 的单条。

    内置模板带 departments/agents 两个计数；用户保存的 EnterpriseTemplate
    只有 key/name/builtin（结构见 save_as_template），故两计数声明为可空默认值。
    """

    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    builtin: bool
    departments: int | None = None
    agents: int | None = None


class TemplateAgentOut(BaseModel):
    """create-company-from-template 里逐个回填的 {name, role}。"""

    model_config = ConfigDict(extra="allow")

    name: str
    role: str | None


class CompanyFromTemplateOut(BaseModel):
    """POST /governance/create-company-from-template。

    ``auto_bound_model`` 在没有可用模型时为 null，但键必须存在（不带默认值）。
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    created_agents: list[TemplateAgentOut]
    auto_bound_model: str | None
    auto_bound_count: int


class TemplateSavedOut(BaseModel):
    """POST /governance/save-company-as-template"""

    model_config = ConfigDict(extra="allow")

    id: str
    key: str
    departments_captured: int


# ---------------------------------------------------------------------------
# 自治模式（app/api/routes/governance.py · P3-16）
# ---------------------------------------------------------------------------


class AutonomousLimitsOut(BaseModel):
    """自治限制三元组：cost 取 AutonomousSettings.cost_limit（Float），
    time_min / actions 为 Integer，均可为 null。"""

    model_config = ConfigDict(extra="allow")

    cost: float | None
    time_min: int | None
    actions: int | None


class AutonomousSettingsSavedOut(BaseModel):
    """PUT /governance/autonomous-settings（``scope`` 回显请求的 scope_type）。"""

    model_config = ConfigDict(extra="allow")

    scope: str
    level: int
    limits: AutonomousLimitsOut


class AutonomousLevelOut(BaseModel):
    """GET /governance/autonomous-settings：无记录时回落 level=2、limits=null。"""

    model_config = ConfigDict(extra="allow")

    level: int
    emergency_stopped: bool
    limits: AutonomousLimitsOut | None


class AutonomousOverviewItemOut(BaseModel):
    """GET /governance/autonomous-overview 的单条（name 由 scope 反查，查不到为 "-"）。"""

    model_config = ConfigDict(extra="allow")

    id: str
    scope_type: str
    scope_id: str | None
    name: str
    level: int
    limits: AutonomousLimitsOut
    emergency_stopped: bool


class AutonomousSettingsDeletedOut(BaseModel):
    """DELETE /governance/autonomous-overview/{settings_id}"""

    model_config = ConfigDict(extra="allow")

    ok: bool
    deleted_id: str
    scope: str
    message: str


class AutonomousLogOut(BaseModel):
    """GET /governance/autonomous-logs 的单条。

    ``summary`` 是从 result JSON 里取出的 ``.summary``（缺字段则 null）；
    ``actions`` 是 actions_taken 的长度，不是数组本身。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    scope_type: str
    scope_id: str | None
    level: int
    why: str | None
    summary: str | None
    actions: int
    cost_estimate: float | None
    human_override: bool
    created_at: str | None


class AutonomousConfigOut(BaseModel):
    """POST /governance/autonomous-configs。

    max_parallel / rate_limit_per_min 实际落在 action_limit / cost_limit 列上。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    agent_id: str
    external_agent_id: str | None
    max_parallel: int | None
    rate_limit_per_min: float | None


class ExecutionLoggedOut(BaseModel):
    """POST /governance/autonomous-executions（只回新日志 id）"""

    model_config = ConfigDict(extra="allow")

    id: str


class EmergencyStopOut(BaseModel):
    """POST /governance/emergency-stop"""

    model_config = ConfigDict(extra="allow")

    stopped_scopes: int
    checkpoint_saved: bool


class ResumeCheckpointOut(BaseModel):
    """POST /governance/resume-from-checkpoint 与 /emergency-resume（后者是别名）。

    ``restored_checkpoint`` 是急停时用户自己提交的 checkpoint，形状不受后端
    约束，故保持任意 dict；无检查点时为 null。
    """

    model_config = ConfigDict(extra="allow")

    resumed: int
    restored_checkpoint: dict | None


# ---------------------------------------------------------------------------
# 计费标准（app/api/routes/tokens.py）
# ---------------------------------------------------------------------------


class PricingConfigOut(BaseModel):
    """GET /tokens/pricing 的单条。``model_name`` 缺失时端点回填 "-"。"""

    model_config = ConfigDict(extra="allow")

    id: str
    model_id: str
    model_name: str
    billing_mode: str
    input_price_per_m: float
    output_price_per_m: float
    monthly_fee: float
    overage_input_per_m: float
    overage_output_per_m: float
    included_tokens_per_month: int
    currency: str
    enabled: bool
    note: str | None


class PricingModelOptionOut(BaseModel):
    """GET /tokens/pricing/models 的单条。"""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    model_id: str
    provider_type: str


class PricingUpsertOut(BaseModel):
    """POST /tokens/pricing（按 model_id 幂等 upsert）"""

    model_config = ConfigDict(extra="allow")

    id: str
    model_id: str
    enabled: bool


class PricingDeletedOut(BaseModel):
    """DELETE /tokens/pricing/{pid}"""

    model_config = ConfigDict(extra="allow")

    deleted: bool


# ---------------------------------------------------------------------------
# 六维用量统计（app/api/routes/tokens.py）
# ---------------------------------------------------------------------------


class TokenTrendPointOut(BaseModel):
    """GET /tokens/stats/summary 的按天趋势点（date 是 ``str(func.date(...))``）。"""

    model_config = ConfigDict(extra="allow")

    date: str
    total_tokens: int
    total_cost: float
    calls: int


class TokenSummaryOut(BaseModel):
    """GET /tokens/stats/summary"""

    model_config = ConfigDict(extra="allow")

    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float
    calls: int
    avg_latency_ms: int
    trend: list[TokenTrendPointOut]


class UsageGroupStatsOut(BaseModel):
    """_group_stats 的公共形状（六维统计里 5 个维度会再补 name）。"""

    model_config = ConfigDict(extra="allow")

    key: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float
    calls: int


class UsageGroupOut(UsageGroupStatsOut):
    """_group_stats + _attach_names 的形状：key 是维度 id（或 "(未归属)"），
    name 是反查到的对象名，反查不到时端点把 key 原样回填。"""

    model_config = ConfigDict(extra="allow")

    name: str


class ScenarioUsageOut(UsageGroupStatsOut):
    """GET /tokens/stats/by-scenario：占比 + 降本建议。

    `name` 是 SCENARIO_LABELS 里的中文标签（未登记的场景码原样回填 key），
    与其余 5 个维度一样都带可读名称。
    """

    model_config = ConfigDict(extra="allow")

    name: str
    cost_pct: float
    rule_candidate: bool
    suggestion: str | None


class UsageLogOut(BaseModel):
    """GET /tokens/usage-logs 的单条流水（时间已在端点内 isoformat 成字符串）。"""

    model_config = ConfigDict(extra="allow")

    id: str
    agent_name: str
    scenario: str
    scenario_label: str
    model_name: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float
    latency_ms: int
    success: bool
    created_at: str | None
