"""Computer Agent Foundation (P2-14).

本机操作的基础架构：接口 + 权限 + Mock 执行器。
真实执行器（截图/键鼠控制）留接口，默认 MOCK 模式返回模拟结果。

能力（action_type）：
- screenshot: 截屏
- type_text: 键入文本
- click: 点击坐标
- run_app: 启动应用
权限模型：每公司一张许可表，未授权的能力直接拒绝；所有调用落审计日志。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ComputerActionLog(Base):
    __tablename__ = "computer_action_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                  index=True)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deny_reason: Mapped[str | None] = mapped_column(Text)
    mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)


# 全部可授权能力
KNOWN_CAPABILITIES = ["screenshot", "type_text", "click", "run_app"]


class ComputerPermission(Base):
    """公司级能力授权。"""
    __tablename__ = "computer_permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(30), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    granted_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
