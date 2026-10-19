"""电脑操作执行服务：权限校验 → 执行 → 审计留痕。

## 为什么需要独立一层

改造前有两套入口访问同一个危险能力，且**互不一致**：
1. `POST /computer-agent/execute`（REST）——有权限校验、有审计日志；
2. 聊天里的 `computer_*` 工具调用——调用了 `_computer_action()`，
   而该函数**在整个代码库中根本不存在**，只有调用没有定义。

第 2 条因为 `except Exception` 被吞掉，表面看是"功能没做好"；
但它意味着"权限校验"这条唯一防线在工具调用路径上是缺失的。
一旦有人顺手把 `_computer_action` 补成一个直接调 executor 的实现，
就等于给 LLM 开了一条**绕过权限模型直通主机 shell**的路。

因此本模块把"校验 + 执行 + 审计"收敛为唯一实现，两条入口共用：
任何绕过都会被限制在同一个检查点上。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.computer_agent import ComputerActionLog, ComputerPermission
from app.computer_real import EXTENDED_CAPABILITIES, REAL_EXECUTORS
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 需要用户显式授权的能力（默认拒绝）。列在这里是为了让"哪些能力危险"
# 在代码里一目了然，而不是散落在提示词和文档里。
HIGH_RISK_CAPABILITIES = frozenset({"run_shell", "run_write"})


class ComputerActionDenied(PermissionError):
    """能力未授权。调用方应转成 403 或给 LLM 返回可读的拒绝信息。"""

    def __init__(self, capability: str, reason: str = "") -> None:
        self.capability = capability
        self.reason = reason or f"能力 {capability} 未授权"
        super().__init__(self.reason)


async def is_capability_granted(db: AsyncSession, company_id: str,
                                capability: str) -> bool:
    """查询公司是否已授权某能力。未授权一律视为 False（默认拒绝）。"""
    row = (await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id,
        ComputerPermission.capability == capability))).scalars().first()
    return bool(row and row.granted)


# V5.7 ⑧ 防呆 ACI（学 SWE-agent）：高危动作执行前"先试错"——
# 参数合法性 + 限行窗口预检，不合法直接返回防呆反馈，
# 不烧 LLM 循环、不落脏操作。每步先验证再执行，出错率↓=token↓。
def _precheck_capability(capability: str, params: dict) -> dict | None:
    """返回 None=放行；否则返回 {ok:False, error, precheck:True}。"""
    p = params or {}
    if capability in ("click",):
        x, y = p.get("x"), p.get("y")
        if x is None or y is None:
            return {"ok": False, "precheck": True,
                   "error": f"坐标缺失：x={x!r} y={y!r}，请先截图定位"}
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            return {"ok": False, "precheck": True,
                   "error": f"坐标必须是整数，收到 x={x!r} y={y!r}"}
        if not (0 <= x <= 10000 and 0 <= y <= 10000):
            return {"ok": False, "precheck": True,
                   "error": f"坐标越界（x={x}, y={y}），请先截图确认目标位置"}
        return None
    if capability == "type_text":
        text = p.get("text")
        if not text:
            return {"ok": False, "precheck": True, "error": "text 不能为空"}
        if len(str(text)) > 500:
            return {"ok": False, "precheck": True,
                   "error": f"文本 {len(str(text))} 字超过 500 字上限，请分段输入"}
        return None
    if capability in ("run_shell", "run_write"):
        cmd = str(p.get("command") or p.get("cmd") or "")
        # 限行窗口：破坏性命令拦截（老板可显式 confirm_destructive 绕过）
        if p.get("confirm_destructive") is not True:
            for pat in ("rm -rf /", "mkfs", "dd if=", ":(){", "shutdown",
                        "reboot", "DROP TABLE", "drop database"):
                if pat in cmd.lower():
                    return {"ok": False, "precheck": True,
                           "error": (f"限行窗口：命令含破坏性片段 {pat!r}。"
                                     "如确需执行，请在 params 显式传 "
                                     "confirm_destructive=true（审计留痕）")}
        return None
    if capability == "run_app":
        app = p.get("app")
        if not app:
            return {"ok": False, "precheck": True, "error": "app 不能为空"}
        return None
    return None


async def execute_computer_action(
    db: AsyncSession,
    *,
    company_id: str,
    capability: str,
    params: dict | None = None,
    agent_id: str | None = None,
    auto_approved: str | None = None,
    skip_precheck: bool = False,
) -> dict:
    """执行一次电脑操作。返回统一结构：

    {"ok": bool, "allowed": bool, "capability": str, "result": dict,
     "log_id": str, "deny_reason": str|None}

    - 能力未知 → 明确报错，不猜；
    - 未授权 → 落审计日志（allowed=False）后抛 ComputerActionDenied；
    - 已授权 → 执行、落审计日志，执行异常也如实记录，不伪造成成功。
    - auto_approved：V5.6.1 白名单放行时传入（如 "whitelist"），审计留痕。
    """
    capability = (capability or "").strip()
    if capability not in EXTENDED_CAPABILITIES:
        raise ValueError(
            f"未知能力 {capability!r}，可用：{', '.join(EXTENDED_CAPABILITIES)}")

    granted = await is_capability_granted(db, company_id, capability)

    log = ComputerActionLog(
        id=str(uuid.uuid4()), company_id=company_id, agent_id=agent_id,
        action_type=capability, params=params, allowed=granted, mock=False,
        auto_approved=auto_approved)

    if not granted:
        log.deny_reason = f"能力 '{capability}' 未获授权（默认拒绝）"
        db.add(log)
        await db.commit()
        logger.warning("电脑操作被拒：company=%s capability=%s agent=%s",
                       company_id, capability, agent_id)
        raise ComputerActionDenied(capability, log.deny_reason)

    executor = REAL_EXECUTORS[capability]
    # V5.7 ⑧ 防呆 ACI：执行前先试错——参数/限行预检不通过直接返回
    # 防呆反馈（不进执行器、不烧 token、不落脏操作）
    if not skip_precheck:
        pre = _precheck_capability(capability, params or {})
        if pre:
            log.result = pre
            log.deny_reason = f"precheck: {pre.get('error')}"
            db.add(log)
            await db.commit()
            return {"ok": False, "allowed": True, "capability": capability,
                    "result": pre, "log_id": log.id, "deny_reason": None,
                    "precheck_blocked": True}
    try:
        result = await executor(params or {})
    except Exception as exc:
        # 执行失败必须留痕，且不得包装成成功
        logger.warning("电脑操作执行异常：capability=%s err=%s",
                       capability, exc, exc_info=True)
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    log.result = result
    db.add(log)
    await db.commit()

    return {"ok": bool(result.get("ok")), "allowed": True,
            "capability": capability, "result": result,
            "log_id": log.id, "deny_reason": None}


async def capability_status(db: AsyncSession, company_id: str) -> dict:
    """返回公司全部能力的授权状态，供前端权限面板渲染。"""
    rows = (await db.execute(select(ComputerPermission).where(
        ComputerPermission.company_id == company_id))).scalars().all()
    granted = {r.capability: r.granted for r in rows}
    return {c: bool(granted.get(c, False)) for c in EXTENDED_CAPABILITIES}
