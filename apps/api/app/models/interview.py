"""部门访谈（V6.2）：企业把日常工作总结成文字/语音转写，喂给总控提炼专家技能。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class ExpertInterview(Base):
    __tablename__ = "expert_interviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    # 目标：公司专家模板（优先）或已上岗 Agent（digest 后同步其人设）
    expert_template_id: Mapped[str | None] = mapped_column(String(36),
                                                           nullable=True,
                                                           index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                 index=True)
    department: Mapped[str | None] = mapped_column(String(120))
    interviewee: Mapped[str | None] = mapped_column(String(120))
    # 语音先转文字（STT 接入前 v1 只收文本）；音频原件可另存 media 留档
    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    audio_media_id: Mapped[str | None] = mapped_column(String(36))
    # uploaded | digested | applied | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="uploaded")
    digest: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
