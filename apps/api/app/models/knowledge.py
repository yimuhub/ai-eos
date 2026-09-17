"""Knowledge Base System (P2-06).

Documents -> chunks (with embeddings placeholder) -> retrieval (keyword +
vector-ready). RAG interface returns top-k chunks for a query.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class KBDocumentStatus(str, enum.Enum):
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False,
        index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
        index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False,
                                              default="text")  # text/url/file
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[KBDocumentStatus] = mapped_column(
        Enum(KBDocumentStatus, native_enum=False, length=50), nullable=False, default=KBDocumentStatus.PROCESSING)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    # ---- P4: 两级知识库 + 双格式存储 ----
    scope: Mapped[str] = mapped_column(
        String(10), nullable=False, default="global",
        server_default="global")  # global=全局公用 | private=Agent私人
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True, index=True)  # scope=private 时的属主 Agent
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 分类标签
    original_path: Mapped[str | None] = mapped_column(String(1000))  # 原始文件保留路径
    original_name: Mapped[str | None] = mapped_column(String(500))   # 原始文件名
    summary: Mapped[str | None] = mapped_column(Text)                # 文档摘要（压缩用）
    keywords: Mapped[list | None] = mapped_column(JSON)              # 全文关键词
    skill_template: Mapped[str | None] = mapped_column(String(50))   # 来自哪个技能模板


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False,
        index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # embedding 向量以 JSON 存储（pgvector 可后续替换）
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
