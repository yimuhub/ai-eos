"""Computer Agent Runtime + Sandbox (P3-10 & P3-11).

曾用名 app.sandbox，2026-10-14 改名为 app.computer_workspace 以消歧：
app/core/sandbox.py 是"文件访问沙箱"，与本模块（Computer 工作区模型）
同名不同义，import 极易拿错。路由模块 app/api/routes/sandbox.py 保持不变。

Sandbox：隔离工作区（Mock Provider，未来可接 Docker/Cloud VM）。
Computer Session：Observe → Reason → Act → Verify 循环，
每步记录 before/after 截图（Mock），可暂停/取消/恢复。
高风险动作需审批。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WorkspaceProvider(str, enum.Enum):
    MOCK = "MOCK"
    DOCKER = "DOCKER"        # 预留
    CLOUD_VM = "CLOUD_VM"    # 预留
    LOCAL_MAC = "LOCAL_MAC"  # 预留


class ComputerWorkspace(Base):
    __tablename__ = "computer_workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[WorkspaceProvider] = mapped_column(
        Enum(WorkspaceProvider), nullable=False, default=WorkspaceProvider.MOCK)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ComputerSession(Base):
    __tablename__ = "computer_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("computer_workspaces.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), index=True)
    goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="RUNNING", index=True)
    # RUNNING / PAUSED / COMPLETED / CANCELLED / FAILED
    snapshot_data: Mapped[dict | None] = mapped_column(JSON)  # 暂停时保存状态
    started_at: Mapped[datetime] = mapped_column(default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SessionAction(Base):
    """Session 内每步操作（before/after 截图 + 结果）。"""
    __tablename__ = "session_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("computer_sessions.id", ondelete="CASCADE"), nullable=False,
        index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON)
    observation: Mapped[dict | None] = mapped_column(JSON)   # 屏幕观察结果
    result: Mapped[dict | None] = mapped_column(JSON)
    verified: Mapped[bool | None] = mapped_column(nullable=True)
    high_risk: Mapped[bool] = mapped_column(default=False)
    approved_by: Mapped[str | None] = mapped_column(String(36))
    before_screenshot: Mapped[str | None] = mapped_column(String(300))
    after_screenshot: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class WorkspaceFile(Base):
    """沙箱内文件。"""
    __tablename__ = "workspace_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("computer_workspaces.id", ondelete="CASCADE"), nullable=False,
        index=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


# 高风险动作清单
HIGH_RISK_ACTIONS = {"delete_file", "submit_form", "send_email",
                     "modify_important_data", "run_shell"}
MOCK_SCREENSHOT = "/tmp/mock_screen_{phase}_{n}.png"
