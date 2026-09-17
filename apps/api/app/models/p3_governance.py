"""P3-13 Governance + P3-14 Marketplace + P3-15 Enterprise Templates + P3-16 Autonomous Mode.

Governance: Improvement Proposal → Sandbox Clone → Test → Compare → Approval → Deploy（版本化+可回滚）。
Marketplace: 内部 Package 系统（manifest/version/deps/permissions/install/rollback）。
Enterprise Template: 一键创建公司模板。
Autonomous Mode: 公司自治等级 L0-L5 + Emergency Stop + Checkpoint 恢复。
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- P3-13 Governance ----------

class AgentSandbox(Base):
    """Agent 沙盒克隆（改进测试用）。"""
    __tablename__ = "agent_sandboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(36), index=True)
    sandbox_config: Mapped[dict | None] = mapped_column(JSON)  # 克隆的配置
    test_results: Mapped[dict | None] = mapped_column(JSON)
    comparison: Mapped[dict | None] = mapped_column(JSON)  # 与 baseline 对比
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentVersion(Base):
    """Agent 配置版本（部署/回滚单位）。"""
    __tablename__ = "agent_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict | None] = mapped_column(JSON)
    deployed_by: Mapped[str | None] = mapped_column(String(36))
    change_note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class SelfImprovementCase(Base):
    __tablename__ = "self_improvement_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    reflection_id: Mapped[str | None] = mapped_column(String(36))
    what_improved: Mapped[str | None] = mapped_column(Text)
    why: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    risk: Mapped[str | None] = mapped_column(Text)
    expected_impact: Mapped[str | None] = mapped_column(Text)
    sandbox_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="PROPOSED", index=True)
    # PROPOSED/SANDBOX_TESTING/TESTED/APPROVED/DEPLOYED/REJECTED/ROLLED_BACK
    decided_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)


# ---------- P3-14 Marketplace ----------

class MarketplacePackage(Base):
    __tablename__ = "marketplace_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    package_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # AGENT / SKILL / WORKFLOW / DEPARTMENT_TEMPLATE
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(100))
    latest_version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    permissions_required: Mapped[list | None] = mapped_column(JSON, default=list)
    dependencies: Mapped[list | None] = mapped_column(JSON, default=list)
    manifest: Mapped[dict | None] = mapped_column(JSON)
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class PackageInstallation(Base):
    __tablename__ = "package_installations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("marketplace_packages.id", ondelete="CASCADE"), nullable=False,
        index=True)
    version: Mapped[str] = mapped_column(String(20))
    installed_by: Mapped[str | None] = mapped_column(String(36))
    install_log: Mapped[dict | None] = mapped_column(JSON)
    rolled_back_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


# ---------- P3-15 Enterprise Templates ----------

class EnterpriseTemplate(Base):
    __tablename__ = "enterprise_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    template_data: Mapped[dict | None] = mapped_column(JSON)  # 完整公司结构
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class CompanyCreationJob(Base):
    __tablename__ = "company_creation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    template_key: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    steps_done: Mapped[list | None] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)


# ---------- P3-16 Autonomous Mode ----------

class AutonomousSettings(Base):
    """公司/部门/项目级自治等级。"""
    __tablename__ = "autonomous_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="COMPANY")
    # COMPANY / DEPARTMENT / PROJECT / WORKFLOW / AGENT
    scope_id: Mapped[str | None] = mapped_column(String(36), index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=2)  # 0-5
    cost_limit: Mapped[float | None] = mapped_column(nullable=True)
    time_limit_minutes: Mapped[int | None] = mapped_column(nullable=True)
    action_limit: Mapped[int | None] = mapped_column(nullable=True)
    emergency_stopped: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class AutonomousExecutionLog(Base):
    __tablename__ = "autonomous_execution_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str | None] = mapped_column(String(36))
    level: Mapped[int] = mapped_column(Integer)
    why: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[dict | None] = mapped_column(JSON)
    actions_taken: Mapped[list | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    cost_estimate: Mapped[float | None] = mapped_column(nullable=True)
    human_override: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint: Mapped[dict | None] = mapped_column(JSON)  # Emergency Stop 时的状态
    created_at: Mapped[datetime] = mapped_column(default=_now)


def new_id():
    return str(uuid.uuid4())
