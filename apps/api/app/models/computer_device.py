"""远程执行设备（sidecar）与下发到设备的命令。

本机直连的执行器跑在后端进程里，只能操作「后端所在的那台电脑」。要让员工
操作老板的 MacBook、操作一台没有后端的 Windows 机器，就得把「执行」这件事
搬到目标机器上：后端只负责判断该不该做，设备端进程（sidecar）负责真做。

两张表：
* `computer_devices`：登记「哪台机器可以被操作」。接入码一次性、明文只出现
  一次，换出一个长期设备密钥，库里只存 SHA-256；在线状态由心跳时刻推导，
  不落库（落了就会和真实状态打架）。
* `computer_dispatches`：一次待执行命令。领取用 CAS（见 service 层），
  两个 sidecar 同时轮询只可能有一个拿到同一条命令。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    """DateTime 列在 SQLite 下是 naive、在 PG 下可能是 aware；混用时区直接
    相减会抛 TypeError 或算出错一天的差值，先统一成 naive UTC。"""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# 心跳/领取都会刷新 last_seen_at；超过这个窗口没动静就按离线处理。
ONLINE_WINDOW = timedelta(seconds=45)


class ComputerDevice(TimestampMixin, Base):
    __tablename__ = "computer_devices"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_device_company_name"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    # 老板起的名字（"老张的 MacBook"），同公司内唯一——默认执行机靠它选，重名会选错
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 设备自己上报的运行时事实，接入前为空
    platform: Mapped[str | None] = mapped_column(String(40))
    hostname: Mapped[str | None] = mapped_column(String(120))
    agent_version: Mapped[str | None] = mapped_column(String(40))
    # 这台机器声明「我能执行哪些能力」。没声明的一律不下发：
    # 服务端策略管该不该做，能力清单管这台机器做不做得了。
    capabilities: Mapped[list | None] = mapped_column(JSON, default=list)
    secret_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    enroll_code_hash: Mapped[str | None] = mapped_column(String(64), unique=True,
                                                         index=True)
    enroll_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(String(36))

    def is_online(self, now: datetime | None = None) -> bool:
        if self.revoked or self.enrolled_at is None or self.last_seen_at is None:
            return False
        return (_as_naive_utc(now or _now())
                - _as_naive_utc(self.last_seen_at)) < ONLINE_WINDOW

    @property
    def has_enrollment(self) -> bool:
        """接入码还没用且没过期——此时启动命令才有意义。"""
        return (bool(self.enroll_code_hash)
                and self.enroll_expires_at is not None
                and _as_naive_utc(self.enroll_expires_at) > _now())


# 状态机：pending → claimed → succeeded / failed；
# pending 或 claimed 超过 deadline_at 一律判 expired（领取与查询时惰性判定，
# 不依赖后台清理任务——便携形态可能根本没有跑定时任务）。
DISPATCH_STATUSES = ("pending", "claimed", "succeeded", "failed", "expired")


class ComputerDispatch(TimestampMixin, Base):
    __tablename__ = "computer_dispatches"
    __table_args__ = (
        Index("ix_dispatch_claim", "device_id", "status", "created_at"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("computer_devices.id", ondelete="CASCADE"), nullable=False,
        index=True)
    capability: Mapped[str] = mapped_column(String(40), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON, default=dict)
    agent_id: Mapped[str | None] = mapped_column(String(36))
    # 审计先落、执行后补：命令派出去时就把 ComputerActionLog 行建好并记住 id，
    # 设备回报时更新它。设备永远不回，日志里也留着一条「派发到 X 但未回报」，
    # 而不是事后补一条时间戳造假的记录。
    log_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    @property
    def terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "expired")
