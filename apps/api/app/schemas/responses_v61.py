"""商业交付引擎响应模型（V6.1）：ops 四引擎 / OAuth / 连接器 / 行业包 / 功能开关。

与 responses_ops.py 的分工：那边收口 war_room/artifacts/models_api 长尾，
这边放 V6.1 新增端点。extra=allow 容纳 payload 里的动态内容
（artifacts/diffs/details 等），必填键显式声明。
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Free(BaseModel):
    model_config = ConfigDict(extra="allow")


class OkOut(_Free):
    ok: bool
    message: str | None = None


class ComplianceItemOut(_Free):
    id: str
    key: str
    name: str
    status: str


class ComplianceListOut(_Free):
    items: list[dict]
    counts: dict


class ComplianceCreateOut(_Free):
    ok: bool
    item: ComplianceItemOut


class CompliancePatchOut(_Free):
    ok: bool
    item: ComplianceItemOut


class ComplianceRefreshOut(_Free):
    ok: bool
    counts: dict


class PayrollRuleOut(_Free):
    ok: bool
    rule: dict


class PayrollListOut(_Free):
    rules: list[dict]


class PayrollEvaluateOut(_Free):
    key: str
    name: str
    mode: str
    details: dict
    total: float


class ReconciliationOut(_Free):
    ok: bool
    run: dict


class ReconciliationListOut(_Free):
    runs: list[dict]


class TraceQueryOut(_Free):
    trace_id: str
    events: list[dict]


class UsageReportOut(_Free):
    month: str | None
    total: dict
    by_agent: dict
    by_scenario: dict


class ConnectorListOut(_Free):
    connectors: list[dict]


class ConnectorCreateOut(_Free):
    ok: bool
    connector: dict


class SyncResultOut(_Free):
    ok: bool
    run: dict


class RunsOut(_Free):
    runs: list[dict]


class IngestOut(_Free):
    ok: bool
    upserted: int


class RecordsOut(_Free):
    records: list[dict]


class OAuthStartOut(_Free):
    ok: bool
    authorize_url: str
    state: str
    hint: str | None = None


class OAuthCallbackOut(_Free):
    ok: bool
    tool_id: str
    tool_name: str
    connected: bool


class MetricCreateOut(_Free):
    ok: bool
    metric: dict


class MetricListOut(_Free):
    metrics: list[dict]


class IndustryPacksOut(_Free):
    packs: list[dict]


class IndustryPublishOut(_Free):
    ok: bool
    package_id: str
    updated: bool
    key: str
    version: str


class FeatureListOut(_Free):
    features: list[dict]


class FeatureSetOut(_Free):
    ok: bool
    key: str
    enabled: bool


class ToolInventoryOut(_Free):
    builtin: list[dict]
    computer: list[dict]
    server: list[dict]
    registered: list[dict]


class ToolActionOut(_Free):
    ok: bool
    tool: dict | None = None
    message: str | None = None


class ToolTestOut(_Free):
    ok: bool
    result: dict


class GrantsOut(_Free):
    granted: list[str]
    resolved: list[dict]


class BindingsListOut(_Free):
    bindings: list[dict]


class ToolRegisterIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    display_name: str | None = None
    description: str | None = None
    source: str = "mcp_http"
    source_ref: str | None = None
    package_key: str | None = None
    category: str | None = None
    parameters: dict[str, Any] | None = None
    risk: str = "low"
    required_capability: str = "operate"
    timeout_seconds: int = 60
    endpoint: str | None = None
    auth_secret_ref: str | None = None


class GrantIn(BaseModel):
    tool_names: list[str] = Field(default_factory=list, max_length=200)


class BindingSetIn(BaseModel):
    user_id: str
    bindings: list[dict] = Field(default_factory=list, max_length=100)
