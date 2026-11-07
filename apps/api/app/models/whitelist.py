"""工具级审批免批白名单（V5.6.1 学 Cline：审批细到工具，配"始终允许"防审批疲劳）。

老板在干预中心批某条高危操作时勾"始终允许"→ 记入白名单
（按 能力 + 参数模式 匹配）；之后同类操作直接放行并留审计
"auto-approved（白名单）"。撤销白名单立即恢复拦截。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)


def _now() -> datetime:
    # 与全项目 naive-UTC 口径一致（修复前用本地时间，UTC+8 下快 8 小时）
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ApprovalWhitelist(Base):
    """免批白名单：capability + 参数模式（子串匹配，老板看得懂的粒度）。"""
    __tablename__ = "approval_whitelists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                   default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(60), nullable=False)
    # run_shell / run_write / run_app / external_agent / ...
    param_pattern: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 空=该能力全部；否则参数 JSON 中需包含此子串（老板勾选时自动从 params 提取）
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    @staticmethod
    def matches(capability: str, params: dict, rows: list["ApprovalWhitelist"]) -> bool:
        """命中任意一条即放行（精确能力 + 参数子串包含）。"""
        for w in rows:
            if w.capability != capability:
                continue
            if not w.param_pattern:
                return True
            if w.param_pattern in _to_text(params):
                return True
        return False


def _to_text(params: dict) -> str:  # 模块级（matches 静态方法内引用）
    import json as _json
    try:
        return _json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        logger.debug("白名单参数无法序列化为 JSON（%s），降级为 str()",
                     type(params).__name__)
        return str(params)
