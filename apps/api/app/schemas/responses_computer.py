"""Computer / 沙箱域的 API 响应模型（A3 长尾：``/sandbox`` + ``/computer-agent``）。

分工同 `app/schemas/responses_ops.py`：按模块分文件，避免并行改动撞车，
所以路由请 `from app.schemas.responses_computer import ...`，不要往 `__init__` 里加。

## 字段来自哪里

两个路由的返回都是函数体里现拼的 dict，逐字段对着源码写，并用
`tests/test_computer_sandbox_contract.py` 在临时 SQLite 库上把端点真跑一遍对账
（断言的是"真实返回的键集合 == 模型声明的键"，方向锁死，改漏立刻红）。
可空字段一律对着**数据库列的 nullable** 而不是当前代码路径：
`ComputerActionLog.result` 在 `nl_computer.py` 的拒绝审计里就是 NULL，
只按 `execute_computer_action` 的写法声明成必填，列表接口遇到那条记录就会 500。

## 三处刻意保留的宽松

* `ComputerExecuteOut` 合并了 `/computer-agent/execute` 的四条分支
  （命中白名单、落单待批、dry_run 预检、真实执行），所以**全部字段可选** +
  路由带 `response_model_exclude_unset=True`：没走到的分支不会凭空多出 `null` 键，
  前端 `res.status === "awaiting_approval"` 的判断和收口前逐字节一致。
* `/sandbox/sessions/{sid}/act` 只有两条分支且必填字段互斥（一条要 `seq`、
  一条要 `requires_approval`），所以用 `SandboxActRunOut | SandboxActApprovalOut`
  联合而不是合并模型：合并模型要让「待审批」分支不多出 `seq: null`，
  就得把七个字段全变可选，前端拿到的类型反而更弱。
* `result` / `observation` / `screenshots` 保持 `dict`：里面装的是执行器自由返回的
  结构（screenshot 给 `image_path/size`、run_shell 给 `stdout/stderr/exit_code`、
  预检给 `ok/precheck/error`），钉成具体类型会让任何一个新执行器把 200 变成 500。
* `/computer-agent/permissions` 直接声明 `dict[str, bool]` 而不是模型：它就是
  `capability_status()` 的「能力名 → 是否授权」映射，键集合由
  `EXTENDED_CAPABILITIES` 决定、会随执行器增减，写成固定字段等于每次加能力都要改这里。

全部模型 `extra="allow"`：字段判断错时多余键原样透出，不会被序列化静默裁掉。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


# ---------------------------------------------------------------- /sandbox


class SandboxWorkspaceOut(BaseModel):
    """工作区：`provider` 是 `.value` 字符串，不是枚举对象。"""

    model_config = _free()

    id: str
    name: str
    provider: str


class SandboxSessionOut(BaseModel):
    """建会话 / pause / cancel 共用：只回 id 与状态。"""

    model_config = _free()

    id: str
    status: str


class SandboxSessionResumeOut(SandboxSessionOut):
    """resume 比另外三个多一个标记，告诉前端"从断点继续"。"""

    resumed_from_checkpoint: bool


class SandboxActApprovalOut(BaseModel):
    """高风险动作未批准：不执行，只落一条待审批记录。"""

    model_config = _free()

    requires_approval: bool
    action_id: str
    reason: str


class SandboxActRunOut(BaseModel):
    """真实（Mock）执行一步：Observe → Act → Verify + before/after 截图。"""

    model_config = _free()

    seq: int
    action: str
    result: dict[str, Any]
    observation: dict[str, Any]
    screenshots: dict[str, str]


class SandboxActionOut(BaseModel):
    """历史步骤列表的一项。

    `verified` / `before` / `after` 可空：待审批记录（`SandboxActApprovalOut`
    那条分支落库的 SessionAction）没有截图、也没走过 verify。
    """

    model_config = _free()

    seq: int
    action: str
    high_risk: bool
    result: dict[str, Any] | None = None
    verified: bool | None = None
    before: str | None = None
    after: str | None = None


class SandboxActionApproveOut(BaseModel):
    model_config = _free()

    id: str
    approved: bool


class SandboxWorkspaceFileOut(BaseModel):
    """`content` 在端点里已截到 200 字，全文另有别的入口。"""

    model_config = _free()

    path: str
    size: int
    content: str


# ---------------------------------------------------------- /computer-agent


class CapabilityGrantOut(BaseModel):
    """grant / revoke 同一个形状，只差 `granted` 的布尔值。"""

    model_config = _free()

    capability: str
    granted: bool


class CapabilitiesListOut(BaseModel):
    """前端权限面板：可授权能力清单 + 当前公司逐项授权状态。"""

    model_config = _free()

    capabilities: list[str]
    granted: dict[str, bool]


class ComputerExecuteOut(BaseModel):
    """/computer-agent/execute 的四条分支合并模型（全可选 + exclude_unset）。

    * 命中免批白名单：`log_id/action/result/mode/auto_approved/message`
    * 高危待审批：`status/approval_request_id/message/mode`
    * dry_run 预检：`dry_run/result/precheck_blocked`
    * 真实执行：`log_id/action/result/mode`
    """

    model_config = _free()

    log_id: str | None = None
    action: str | None = None
    result: dict[str, Any] | None = None
    mode: str | None = None
    auto_approved: str | None = None
    message: str | None = None
    status: str | None = None
    approval_request_id: str | None = None
    dry_run: bool | None = None
    precheck_blocked: bool | None = None


class ComputerLogOut(BaseModel):
    """审计日志（含被拒绝的）。`result` 在拒绝路径上是 NULL。"""

    model_config = _free()

    id: str
    agent_id: str | None = None
    action: str
    allowed: bool
    deny_reason: str | None = None
    mock: bool
    result: dict[str, Any] | None = None
    created_at: str


class LatestScreenshotOut(BaseModel):
    """`image_base64` 是完整 data URL，前端直接塞进 <img src>。"""

    model_config = _free()

    image_base64: str
    taken_at: str
    path: str
