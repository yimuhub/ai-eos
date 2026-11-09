"""图片/视频生成记录（创作中心）。

模型目录早就能分类 image/video 模型，但产品一直没有生成路径——
「配而不用」。这张表让每次生成有始有终：提示词、参数、上游任务号、
结果文件都落库。结果下载到本地存储而不是只存上游链接：第三方 URL
可能过期，公司资产不该依赖别人的 CDN 存活。
"""
from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

from .base import TimestampMixin


class MediaGeneration(TimestampMixin, Base):
    __tablename__ = "media_generations"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 引用的模型配置行；模型被删时记录保留（SET NULL），名字冗余存一份
    model_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), nullable=True)
    model_name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(10))  # image | video
    prompt: Mapped[str] = mapped_column(Text)
    # processing | completed | failed
    status: Mapped[str] = mapped_column(String(20), default="processing",
                                        index=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True,
                                                default=dict)
    upstream_video_id: Mapped[str | None] = mapped_column(String(200),
                                                        nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)