"""Agent Factory 2.0 (P3-08).

Blueprint → Proposal → 用户 Edit/Approve → 创建 Agent →
Model/Skill 绑定 → Test Task → 通过才 ACTIVE。
配额限制 + 审计，禁止无限创建。
"""
import enum
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactoryJobStatus(str, enum.Enum):
    BLUEPRINT = "BLUEPRINT"     # 已生成蓝图，待用户确认
    APPROVED = "APPROVED"       # 已批准，创建中
    TESTING = "TESTING"         # 测试任务执行中
    ACTIVE = "ACTIVE"           # 测试通过，Agent 上岗
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class AgentFactoryJob(Base):
    __tablename__ = "agent_factory_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)  # 部门/项目目标
    blueprint: Mapped[dict | None] = mapped_column(JSON)  # Agent 蓝图
    status: Mapped[FactoryJobStatus] = mapped_column(
        Enum(FactoryJobStatus), nullable=False, default=FactoryJobStatus.BLUEPRINT,
        index=True)
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    test_task_id: Mapped[str | None] = mapped_column(String(36))
    test_result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # 📎 附件引用


# 每公司每日创建上限（防无限创建）
DAILY_QUOTA = 5


async def check_quota(db: AsyncSession, company_id: str) -> bool:
    from sqlalchemy import func, select
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                microsecond=0, tzinfo=None)
    q = await db.execute(select(func.count()).select_from(AgentFactoryJob).where(
        AgentFactoryJob.company_id == company_id,
        AgentFactoryJob.created_at >= today))
    return (q.scalar() or 0) < DAILY_QUOTA


async def generate_blueprint(db: AsyncSession, company_id: str,
                             goal: str) -> dict:
    """用 LLM 从目标生成 Agent 蓝图。"""
    from app.model_gateway import ModelGateway
    from app.models.agent import Agent, AgentModelConfig
    from sqlalchemy import select as _sel

    # 只在本公司已绑定的模型里挑：全表取第一条会跨租户借用别家的模型配置
    mq = await db.execute(
        _sel(AgentModelConfig)
        .join(Agent, Agent.id == AgentModelConfig.agent_id)
        .where(Agent.company_id == company_id)
        .order_by(AgentModelConfig.created_at)
        .limit(1))
    m = mq.scalars().first()
    if not m:
        raise RuntimeError("本公司还没有可用模型，请先给员工绑定模型")

    sys_prompt = (
        "你是组织设计专家。根据部门目标设计一个 AI Agent，输出JSON：\n"
        '{"name":"Agent名字","role":"岗位角色","responsibilities":["职责1","职责2"],'
        '"required_skills":["技能1","技能2"],"recommended_model_hint":"primary",'
        '"system_prompt":"该Agent的系统提示词","knowledge_scope":["知识领域"],'
        '"permission_level":"standard"}\n只输出JSON。')
    gw = ModelGateway(db)
    from app.model_gateway import usage_context
    with usage_context(company_id=company_id, scenario="FACTORY",
                       detail={"factory": True}):
        result = await gw.chat_with_agent_models(
            [m.model_id],
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": f"部门/项目目标：{goal}"}],
            config={"max_tokens": 1000})
    raw = result.get("content") or "{}"
    start, end = raw.find("{"), raw.rfind("}")
    # 修复：模型输出半个 {（end=-1）时 raw[start:0] 是空串 → json.loads 必炸；
    # 解析出非 dict（如数组）时 setdefault 抛 AttributeError → 500。
    # 两种都按"模型没给出有效蓝图"归一为空 dict，交给后续兜底。
    try:
        data = json.loads(raw[start:end + 1]) if (start >= 0 and end > start) else {}
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    # 补全默认字段
    data.setdefault("required_skills", [])
    data.setdefault("knowledge_scope", [])
    data.setdefault("permission_level", "standard")
    return data
