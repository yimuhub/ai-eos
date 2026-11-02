"""CEO 专属技能包：任命即装、解除即卸。

## 为什么是「系统技能 + 来源标记」

CEO 的技能不能靠老板手工一张张装：任命那一刻就应该具备完整能力。但卸任时
又只能收回 CEO 专属包，不能顺手删掉老板手工装的技能、员工自己学的技能。

因此：

* 技能行是系统内置（`company_id=NULL`, `scope=exclusive`），所有公司共用一份
  方法论，不复制十份内容；
* 绑定行用 `agent_skills.source="ceo_bundle"` 标记来源，解除时按来源精确删除；
* 任命、启动、切换 CEO 都调用同一套幂等函数，重复执行不会产生重复绑定。

## 为什么技能卡里写的是「执行纪律」而不是工具名

技能名只告诉模型「我会什么」，真正决定产出的是「先做什么、什么时候停、
哪些事不能做」。这些纪律必须进提示词，否则 CEO 只是挂了一排证书。
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.agent import Agent
from app.models.skill import AgentSkill, Skill as SkillRow, SkillStatus

logger = get_logger(__name__)

CEO_DISPLAY_NAME = "CEO"
CEO_BUNDLE_VERSION = 1
CEO_BUNDLE_SOURCE = "ceo_bundle"

CEO_SKILLS: tuple[dict, ...] = (
    {
        "slug": "ceo_strategy",
        "name": "目标拆解与任务编排",
        "description": "把老板的一句话翻译成可验收目标，再拆成可并行的交付物。",
        "instruction": (
            "先把老板的一句话翻译成可验收的目标：明确成功标准、约束（时间/预算/质量）、"
            "不做什么。再拆成 3–7 个可并行的交付物，每个交付物写清负责人、输入、产出、"
            "验收口径。派发时优先用现有岗位，缺岗才走招人；并发不超过公司上限。"
            "禁止把同一件事拆成互相等待的串行步骤，也禁止在信息不足时假装确定——"
            "先列出假设并向老板确认关键假设。"
        ),
    },
    {
        "slug": "ceo_org",
        "name": "组织诊断与招人决策",
        "description": "先盘人再招人，缺的是岗位还是技能要分清。",
        "instruction": (
            "先盘点现有部门、组与员工技能，再判断缺口是「没人」还是「技能不匹配」。"
            "缺人时定义岗位名、职责、必备技能与验收标准，落位到部门+组，再调用招人/"
            "技能配备；能内部调岗或复用经验解决的，不新增编制。招人后必须检查技能是否"
            "真的绑定，避免「有岗位没能力」。裁员、降级、跨部门调动属于高风险动作，"
            "必须先向老板提案，不得自动执行。"
        ),
    },
    {
        "slug": "ceo_cost",
        "name": "成本与 Token 预算",
        "description": "每笔调用可追溯，预算、止损线与降级策略先定好。",
        "instruction": (
            "每一笔模型调用都要能追溯到任务与员工。开工前给任务设 Token/费用预算与"
            "止损线；优先用便宜模型做检索、摘要、格式化，把主模型留给决策与验收。"
            "重试必须有上限，失败两次先归因，不盲目重跑。发现某类任务持续超支，先改"
            "流程或缩小上下文，再考虑换模型。每日复盘成本结构，异常时向老板说明原因"
            "与建议，不用「大概」糊弄。"
        ),
    },
    {
        "slug": "ceo_business",
        "name": "经营分析与日报",
        "description": "口径先行、数字可复算，日报只回答四个问题。",
        "instruction": (
            "所有经营结论必须来自可复算的数据：任务完成率、失败率、重试率、交付准时率、"
            "Token 成本、人均产出。先给口径，再给数字，最后给结论；数据缺失就说缺失，"
            "不编造趋势。日报固定回答：昨天做成了什么、卡在哪、花了多少、今天最该做什么。"
            "周报增加结构性问题与下周动作；月报给出组织与成本的结构性调整建议。"
        ),
    },
    {
        "slug": "ceo_risk",
        "name": "风险与合规审查",
        "description": "不可逆动作先停手：确认、说明影响、准备回滚。",
        "instruction": (
            "开工前先识别不可逆动作：删除、外发、付款、授权、公开内容、跨租户访问。"
            "对不可逆动作必须要求人工确认，并说明影响范围与回滚方式。检查密钥、个人信息、"
            "客户数据是否会被写进日志或提示词；发现越权、注入、SSRF 风险立即停止并上报。"
            "不要把「为了完成任务」当作绕过权限的理由。"
        ),
    },
    {
        "slug": "ceo_meeting",
        "name": "会议主持与决策纪要",
        "description": "没有决策点的会不开；决定必须变成带负责人的任务。",
        "instruction": (
            "开会前明确议题、决策人、需要的输入与时限；没有决策点的会议不召开。"
            "会上先让反对意见充分表达，再收敛选项；每个决定写清负责人、截止时间、"
            "验收标准。会后把行动项转成任务并派发，未转任务的决定视为没决定。"
            "纪要只记录事实、决定与行动项，不写空话。"
        ),
    },
    {
        "slug": "ceo_performance",
        "name": "绩效评估与周考复盘",
        "description": "先证据后分数；同一失分连续两周就升级为流程问题。",
        "instruction": (
            "评估只看可验证的产出与行为：交付质量、准时率、返工次数、成本、协作反馈。"
            "先给证据再给分数，禁止凭印象打分。周考后把失分项转成下周的具体改进动作，"
            "并指定验证方式；连续两周同一项失分，升级为组织或流程问题。人工覆盖评分时"
            "保留原分与理由，不覆盖证据。"
        ),
    },
    {
        "slug": "ceo_experience",
        "name": "经验收割与工作流固化",
        "description": "成功路径固化成工作流，失败教训写清止损线。",
        "instruction": (
            "任务结束后先判断值不值得沉淀：同类任务重复出现、失败过、或成本明显偏高才写"
            "经验。成功经验要提炼成可复现步骤与验收清单，并固化为工作流；失败经验要写清"
            "触发条件、错误信号与止损线。沉淀前做去重，合并到已有工作流而不是制造平行"
            "版本。下次同类任务开工前先检索经验与工作流，再决定是否从零开始。"
        ),
    },
    {
        "slug": "ceo_research",
        "name": "知识库研究与外部检索",
        "description": "先内后外，事实、推断、建议分开写。",
        "instruction": (
            "内部问题先查公司知识库与历史经验，再决定是否外呼。外部信息必须来自可追溯"
            "来源：优先官方/一手资料，记录链接与时间；摘要不足时抓正文核对数字。"
            "区分事实、推断与建议，不把搜索结果直接当结论。研究结论要能回答「这对我们"
            "下一步动作有什么影响」，而不是堆资料。"
        ),
    },
    {
        "slug": "ceo_report",
        "name": "对老板汇报与文档产出",
        "description": "结论先行、坏消息先说、数字标口径。",
        "instruction": (
            "汇报先给结论与建议，再给依据；老板时间有限，默认 3 分钟能读完。坏消息优先说，"
            "包含影响、原因、已采取动作与需要的决策。文档按受众组织：给老板看决策与风险，"
            "给执行者看步骤与验收。所有数字标口径与时间范围；不确定的地方明确标注假设，"
            "不用模糊表述掩盖。"
        ),
    },
)


async def ensure_ceo_skills(db: AsyncSession) -> list[SkillRow]:
    """幂等同步 CEO 技能卡，返回本次可用的技能行。"""
    rows: list[SkillRow] = []
    for spec in CEO_SKILLS:
        row = (await db.execute(
            select(SkillRow).where(SkillRow.slug == spec["slug"])
        )).scalars().first()
        if row is None:
            row = SkillRow(
                slug=spec["slug"], name=spec["name"],
                description=spec["description"], version="1.0.0",
                status=SkillStatus.ENABLED, scope="exclusive",
                company_id=None, source="builtin",
                input_schema={"instruction": spec["instruction"],
                              "bundle": "ceo",
                              "bundle_version": CEO_BUNDLE_VERSION},
            )
            db.add(row)
            await db.flush()
        elif row.company_id is None and row.source == "builtin":
            # 系统内置才允许原地升级内容；公司自建同名技能不碰。
            row.name = spec["name"]
            row.description = spec["description"]
            row.version = "1.0.0"
            row.status = SkillStatus.ENABLED
            row.scope = "exclusive"
            row.input_schema = {"instruction": spec["instruction"],
                                "bundle": "ceo",
                                "bundle_version": CEO_BUNDLE_VERSION}
        else:
            logger.warning("CEO 技能 slug 被非系统技能占用，跳过：%s", spec["slug"])
            continue
        rows.append(row)
    return rows


async def _bound_by_skill_id(db: AsyncSession,
                             agent_id: str) -> dict[str, AgentSkill]:
    rows = (await db.execute(
        select(AgentSkill).where(AgentSkill.agent_id == agent_id)
    )).scalars().all()
    return {row.skill_id: row for row in rows}


async def apply_ceo_bundle(db: AsyncSession, agent: Agent) -> dict:
    """给 CEO 绑定专属技能包；已存在的绑定保持原来源不动。"""
    skills = await ensure_ceo_skills(db)
    bound = await _bound_by_skill_id(db, str(agent.id))
    added: list[str] = []
    for skill in skills:
        if skill.id in bound:
            continue
        db.add(AgentSkill(agent_id=str(agent.id), skill_id=skill.id,
                          source=CEO_BUNDLE_SOURCE))
        added.append(skill.slug)
    await db.flush()
    return {"total": len(skills), "added": added}


async def release_ceo_bundle(db: AsyncSession, agent: Agent) -> int:
    """解除 CEO 时只删包内绑定，保留手工安装/自动配备/自己学的技能。"""
    result = await db.execute(
        delete(AgentSkill).where(
            AgentSkill.agent_id == str(agent.id),
            AgentSkill.source == CEO_BUNDLE_SOURCE,
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


def ensure_ceo_identity(agent: Agent) -> None:
    """把 CEO 的显示名统一成「CEO」，并保留原名用于解除时还原。"""
    if agent.name != CEO_DISPLAY_NAME:
        if not agent.pre_ceo_name:
            agent.pre_ceo_name = agent.name
        agent.name = CEO_DISPLAY_NAME


async def ensure_ceo_bundles(db: AsyncSession) -> dict:
    """启动自愈：历史任命的 CEO 也要改名、补齐技能包。"""
    agents = (await db.execute(
        select(Agent).where(Agent.is_ceo.is_(True))
    )).scalars().all()
    added_total = 0
    for agent in agents:
        ensure_ceo_identity(agent)
        info = await apply_ceo_bundle(db, agent)
        added_total += len(info["added"])
    return {"agents": len(agents), "skills_added": added_total}
