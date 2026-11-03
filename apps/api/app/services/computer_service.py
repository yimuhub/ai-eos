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

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.computer_agent import ComputerActionLog, ComputerPermission
from app.computer_files import PREVIEW_CAPABILITIES, preview_file_op
from app.computer_real import EXTENDED_CAPABILITIES, REAL_EXECUTORS
from app.core.logging_config import get_logger

logger = get_logger(__name__)


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


# 需要「先问老板」的能力。判据不是"改不改文件"，而是能否等价于主机命令执行：
# `open -a Terminal` 之后再 type_text 就是提交命令，所以键鼠注入与 shell 同级；
# 无障碍树的 click_element / set_value 同理——按下的是「删除」还是「发送」，
# 差别只在看不看得见鼠标。文件写与补丁同样是"改主机上的字节"，同级处理；
# read_file 不在这里（只读，策略里归 observe）。
HIGH_RISK_CAPABILITIES = frozenset({
    "run_shell", "run_write", "run_app",
    "type_text", "click", "key",
    "click_element", "set_value",
    "write_file", "apply_patch",
})


# 「始终允许」勾选项按什么界定"同一类操作"。文件写入只能按路径：正文每次都不
# 一样，取错字段的后果是 pattern 落成空串，而空串在白名单里的意思是
# 「该能力全部放行」——批一次就能往任意路径覆盖任意文件，审批门当场失效。
PATH_SCOPED_CAPABILITIES = frozenset({"read_file", "write_file", "apply_patch"})


def whitelist_pattern_for(capability: str, params: dict) -> str:
    p = params or {}
    if capability in PATH_SCOPED_CAPABILITIES:
        path = str(p.get("path") or "").strip()
        # 带引号：匹配的是 params 序列化后的 "path" 字段值本身，
        # 别的参数（正文）里恰好提到同一段路径不会误命中
        return f'"{path}"' if path else ""
    return str(p.get("cmd") or p.get("command") or p.get("text")
               or p.get("task_input") or "")[:300]


async def _legacy_needs_approval(db: AsyncSession, company_id: str) -> bool:
    """公司还没配过三档策略时的旧门：干预策略勾了 computer_command 才拦。"""
    from app.models.intervention import InterventionPolicy

    policy = (await db.execute(
        select(InterventionPolicy).where(
            InterventionPolicy.company_id == company_id,
            InterventionPolicy.project_id.is_(None)))).scalars().first()
    return bool(policy and "computer_command" in (policy.approval_actions or []))


def _preview_summary(preview: dict) -> str:
    """改动预览压成一行——推送和收件箱只显示 reason，这里不说清就等于没说。"""
    path = str(preview.get("path") or "")
    if not preview.get("available"):
        return f"{path or '（路径未知）'} 预演失败：{preview.get('reason')}"
    if not preview.get("changed"):
        return f"{path} 内容与现状一致，批准也不会改盘"
    if preview.get("create"):
        return f"新建 {path}（+{preview.get('added_lines')} 行）"
    line = (f"{path}：+{preview.get('added_lines')} 行 / "
            f"-{preview.get('removed_lines')} 行，{preview.get('hunks')} 处改动")
    if preview.get("edits"):
        line += f"（{preview.get('edits')} 处替换）"
    if preview.get("diff_truncated"):
        line += "（diff 过长已截断）"
    return line


async def approval_gate(db: AsyncSession, *, company_id: str, capability: str,
                        params: dict | None, agent_id: str | None = None) -> dict:
    """三档策略 + 免批白名单 + 审批单——REST 与聊天两条入口共用。

    返回四种结论之一，调用方按结论决定要不要真执行：

    - `{"action": "allow"}`：直接执行（只读动作，或旧规则下没开审批门）；
    - `{"action": "allow_auto", "auto_approved": ...}`：命中白名单，放行但留痕；
    - `{"action": "block", "reason": ...}`：硬拦截，任何模式都不放行；
    - `{"action": "hold", "approval_request_id": ..., "reason": ...}`：
      审批单已建、通知已发，本次不执行。

    为什么必须在 service 层：聊天里的 `computer_*` 工具调用过去只查授权、
    不查策略，等于「未配置审批的老板在设置页开了三档模式，模型照样能直接点」。
    """
    from app.models.whitelist import ApprovalWhitelist

    from app.services import computer_policy as policy_service

    params = params or {}
    row = await policy_service.get_policy_row(db, company_id)
    verdict = policy_service.evaluate_action(row, capability, params)

    if verdict["decision"] == "blocked":
        return {"action": "block", "reason": verdict["reason"],
                "mode": verdict["mode"]}

    needs_approval = verdict["decision"] == "approval" or (
        verdict["decision"] == "legacy"
        and capability in HIGH_RISK_CAPABILITIES
        and await _legacy_needs_approval(db, company_id))
    if not needs_approval:
        auto = (f"policy:{verdict['mode']}"
                if verdict["decision"] == "allow" and row is not None else None)
        return {"action": "allow", "auto_approved": auto,
                "reason": verdict["reason"], "mode": verdict["mode"]}

    wl_rows = (await db.execute(
        select(ApprovalWhitelist).where(
            ApprovalWhitelist.company_id == company_id,
            ApprovalWhitelist.capability == capability))).scalars().all()
    if ApprovalWhitelist.matches(capability, params, list(wl_rows)):
        return {"action": "allow_auto", "auto_approved": "whitelist",
                "reason": "命中免批白名单", "mode": verdict["mode"]}

    import json

    from app.models.intervention import ApprovalRequest, new_id
    from app.services.notification_service import notify_approval_pending

    # 文件写/补丁：建单前先只演不写算一次 diff。老板点「批准」时看到的必须
    # 是「哪个文件、加几行删几行」，而不是被截断成正好 200 字的文件正文。
    preview: dict | None = None
    if capability in PREVIEW_CAPABILITIES:
        preview = await asyncio.to_thread(preview_file_op, capability, params)

    detail = (f"改动预览：{_preview_summary(preview)}" if preview is not None
              else json.dumps(params, ensure_ascii=False)[:200])
    reason = f"高危本机操作 {capability}（{verdict['reason']}）: {detail}"
    req = ApprovalRequest(
        id=new_id(), company_id=company_id, action_type="computer_command",
        payload={"capability": capability, "params": params,
                 "agent_id": agent_id, "awaiting_execution": True,
                 "preview": preview},
        requested_by_agent_id=agent_id, reason=reason)
    db.add(req)
    await notify_approval_pending(
        db, company_id=company_id,
        title=f"待审批：高危本机操作 {capability}", body=reason)
    await db.commit()
    return {"action": "hold", "approval_request_id": req.id,
            "reason": verdict["reason"], "mode": verdict["mode"]}


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
    # 无障碍树一路：参数残缺先给可读反馈，不要白花一次 osascript 冷启动
    if capability == "find_element" and not str(p.get("query") or "").strip():
        return {"ok": False, "precheck": True,
                "error": "query 不能为空（要匹配的按钮/字段文字）"}
    if capability in ("click_element", "set_value") and not str(p.get("ref") or "").strip():
        return {"ok": False, "precheck": True,
                "error": "缺少 ref：先用 accessibility_snapshot 或 find_element 拿到元素引用"}
    if capability == "get_window" and not str(p.get("app") or "").strip():
        return {"ok": False, "precheck": True,
                "error": "get_window 需要 app（先用 list_windows 看进程名）"}
    # 文件一路：路径缺失/内容类型错，先给可读反馈，不要等到执行器抛栈
    if capability in ("read_file", "write_file", "apply_patch"):
        if not str(p.get("path") or "").strip():
            return {"ok": False, "precheck": True,
                    "error": "path 不能为空（绝对路径或相对仓库根的路径）"}
        if capability == "read_file":
            return None
        if capability == "write_file" and not isinstance(p.get("content"), str):
            return {"ok": False, "precheck": True,
                    "error": 'write_file 需要 content（字符串，清空文件传 ""）'}
        if capability == "apply_patch":
            edits = p.get("edits")
            if not isinstance(edits, list) or not edits:
                return {"ok": False, "precheck": True,
                        "error": 'apply_patch 需要 edits：[{"old": "原文", "new": "新文"}]'}
            for i, edit in enumerate(edits):
                if not isinstance(edit, dict) or not isinstance(edit.get("old"), str) \
                        or not edit.get("old") or not isinstance(edit.get("new"), str):
                    return {"ok": False, "precheck": True,
                            "error": f"第 {i + 1} 处 edits 不合法：old 必须是非空原文字符串，"
                                     "new 必须是字符串（删除传空串）"}
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
    - 硬拦截（凭据目录 / 灾难性命令 / 向终端宿主注入）→ 一律不执行，
      这里兜底是因为「审批通过后执行」和「聊天工具调用」也走本函数：
      红线不能只在 REST 入口检查一次；
    - 未授权 → 落审计日志（allowed=False）后抛 ComputerActionDenied；
    - 已授权 → 执行、落审计日志，执行异常也如实记录，不伪造成成功。
    - auto_approved：V5.6.1 白名单放行时传入（如 "whitelist"），审计留痕。
    """
    capability = (capability or "").strip()
    if capability not in EXTENDED_CAPABILITIES:
        raise ValueError(
            f"未知能力 {capability!r}，可用：{', '.join(EXTENDED_CAPABILITIES)}")

    from app.services import computer_policy as policy_service

    block = policy_service.hard_block_reason(capability, params or {})
    if block:
        blocked_log = ComputerActionLog(
            id=str(uuid.uuid4()), company_id=company_id, agent_id=agent_id,
            action_type=capability, params=params, allowed=False,
            deny_reason=block, mock=False, auto_approved=auto_approved,
            result={"ok": False, "decision": "blocked"})
        db.add(blocked_log)
        await db.commit()
        logger.warning("硬拦截：company=%s capability=%s reason=%s",
                       company_id, capability, block)
        return {"ok": False, "allowed": False, "capability": capability,
                "result": {"ok": False, "blocked": True, "error": block},
                "log_id": blocked_log.id, "deny_reason": block,
                "decision": "blocked"}

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
