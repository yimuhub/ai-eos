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


async def execute_computer_action(
    db: AsyncSession,
    *,
    company_id: str,
    capability: str,
    params: dict | None = None,
    agent_id: str | None = None,
) -> dict:
    """执行一次电脑操作。返回统一结构：

    {"ok": bool, "allowed": bool, "capability": str, "result": dict,
     "log_id": str, "deny_reason": str|None}

    - 能力未知 → 明确报错，不猜；
    - 未授权 → 落审计日志（allowed=False）后抛 ComputerActionDenied；
    - 已授权 → 执行、落审计日志，执行异常也如实记录，不伪造成成功。
    """
    capability = (capability or "").strip()
    if capability not in EXTENDED_CAPABILITIES:
        raise ValueError(
            f"未知能力 {capability!r}，可用：{', '.join(EXTENDED_CAPABILITIES)}")

    granted = await is_capability_granted(db, company_id, capability)

    log = ComputerActionLog(
        id=str(uuid.uuid4()), company_id=company_id, agent_id=agent_id,
        action_type=capability, params=params, allowed=granted, mock=False)

    if not granted:
        log.deny_reason = f"能力 '{capability}' 未获授权（默认拒绝）"
        db.add(log)
        await db.commit()
        logger.warning("电脑操作被拒：company=%s capability=%s agent=%s",
                       company_id, capability, agent_id)
        raise ComputerActionDenied(capability, log.deny_reason)

    executor = REAL_EXECUTORS[capability]
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
