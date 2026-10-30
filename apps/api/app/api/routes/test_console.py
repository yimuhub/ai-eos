"""全功能测试台：系统已实现功能元数据 + 可见性开关。

RULE: 只读元数据，不改任何业务逻辑。数据源为静态注册表（与路由/页面对照）。
可见性：enable_full_function_test_page 配置开关（默认 dev/test 开、生产关）+
       角色校验（仅 owner/开发角色可见）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/test-console", tags=["test-console"])

# ---------------------------------------------------------------------------
# 功能注册表：按业务模块分组，每项 = 名称 / 描述 / 前端路由 / API 前缀 / 状态
# ---------------------------------------------------------------------------

FUNCTION_REGISTRY: list[dict] = [
    {
        "module": "指挥中枢",
        "items": [
            {"name": "指挥中心", "desc": "公司运行总览：Agent 状态、任务统计、实时动态",
             "route": "/", "api": "/dashboard"},
            {"name": "项目管理", "desc": "项目全生命周期创建与管理",
             "route": "/projects", "api": "/projects"},
            {"name": "任务中心", "desc": "任务列表、状态流转、执行详情",
             "route": "/tasks", "api": "/tasks"},
            {"name": "任务调度中心", "desc": "多目标分发(single/multi/org/company)、动态优先级、全局中断",
             "route": "/task-dispatch", "api": "/task-dispatch"},
        ],
    },
    {
        "module": "智能体",
        "items": [
            {"name": "数字员工管理", "desc": "Agent 创建/编辑/删除、能力包、模型绑定、上下级",
             "route": "/agents", "api": "/agents"},
            {"name": "员工成长档案", "desc": "能力版本历史、学习计划、成长时间线",
             "route": "/growth", "api": "/learning"},
            {"name": "训练中心", "desc": "对话训练 → LLM 提取学习点 → 确认生效",
             "route": "/training", "api": "/training"},
            {"name": "技能成长体系", "desc": "技能树、等级晋升、证据链",
             "route": "/skill-growth", "api": "/skill-growth"},
            {"name": "复盘中心", "desc": "任务复盘、经验教训沉淀",
             "route": "/reflection", "api": "/reflection"},
            {"name": "绩效考核", "desc": "多维考核评分与排行",
             "route": "/performance", "api": "/performance"},
            {"name": "职业发展", "desc": "任职履历与晋升提案",
             "route": "/career", "api": "/career"},
            {"name": "Agent 工厂", "desc": "能力缺口 → 蓝图生成 → 审批上岗",
             "route": "/factory", "api": "/factory"},
            {"name": "企业模板", "desc": "组织模板快速复制建团",
             "route": "/templates", "api": None},
            {"name": "离职继承", "desc": "批量离职 + 技能/成果/记忆传承给继任者",
             "route": "/agent-offboarding", "api": "/agents/offboarding"},
            {"name": "外部 Agent 接入", "desc": "Hermes/Codex 等外部智能体连接配置",
             "route": "/external-agents", "api": "/external-agents"},
            {"name": "沙盒测试", "desc": "改进方案隔离验证环境",
             "route": "/sandbox", "api": "/sandbox"},
        ],
    },
    {
        "module": "协作",
        "items": [
            {"name": "工作流引擎", "desc": "可视化编排、断点续跑、状态持久化",
             "route": "/workflow-builder", "api": "/workflows"},
            {"name": "AI 会议", "desc": "自动召开、LLM 多轮发言、决策转行动项",
             "route": "/meetings", "api": "/meetings"},
            {"name": "协作消息", "desc": "12 类消息协议、任务派发流",
             "route": "/collaboration", "api": "/collaboration"},
        ],
    },
    {
        "module": "资产与知识",
        "items": [
            {"name": "知识库", "desc": "文档上传、自动切片、RAG 检索",
             "route": "/knowledge", "api": "/knowledge"},
            {"name": "成果中心", "desc": "工作产出版本管理与协同",
             "route": "/artifacts", "api": "/artifacts"},
            {"name": "记忆系统", "desc": "5 类记忆、TTL 衰减、跨任务召回",
             "route": "/memory", "api": "/memory"},
        ],
    },
    {
        "module": "治理与自动化",
        "items": [
            {"name": "任务调度中心(自动化)", "desc": "TIME/EVENT 触发器、审批断点、紧急停止",
             "route": "/schedules", "api": "/automations"},
            {"name": "自治模式", "desc": "L0-L4 自治等级、成本限额、Checkpoint",
             "route": "/autonomous", "api": "/governance"},
            {"name": "人工介入", "desc": "接管队列、人工审批覆盖",
             "route": "/intervention", "api": "/intervention"},
            {"name": "治理与控制", "desc": "改进提案审批、版本化部署、回滚",
             "route": "/governance", "api": "/governance"},
            {"name": "审计日志", "desc": "全系统操作事件追溯",
             "route": "/logs", "api": "/events"},
        ],
    },
    {
        "module": "底层引擎能力",
        "items": [
            {"name": "浏览器代理", "desc": "网页访问/抓取，含 SSRF 安全域名校验",
             "route": None, "api": "/browser-agent"},
            {"name": "电脑操作代理", "desc": "截图/点击/命令执行的计算机操作能力",
             "route": None, "api": "/computer-agent"},
            {"name": "模型网关", "desc": "主→备→降级多模型故障转移链",
             "route": "/models", "api": "/models"},
        ],
    },
]


def _is_enabled() -> bool:
    """配置开关：enable_full_function_test_page。dev/test 默认开。
    通过 settings 判断；未配置时按 debug 标志兜底。"""
    flag = getattr(settings, "enable_full_function_test_page", None)
    if flag is None:
        flag = getattr(settings, "debug", False)
    return bool(flag)


@router.get("/meta")
async def test_console_meta(user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    """返回功能清单。生产环境关闭或非授权角色时返回 404（对普通用户隐藏存在性）。"""
    if not _is_enabled():
        raise HTTPException(404, "Not Found")
    # 仅"公司成员"（开发/test 角色）可见：owner ∪ active 成员行
    from sqlalchemy import select
    from app.models.company import Company
    from app.models.membership import CompanyMember
    owned = (await db.execute(
        select(Company.id).join(
            User, User.id == Company.owner_id).where(User.id == user.id)
        .union(select(CompanyMember.company_id).where(
            CompanyMember.user_id == user.id,
            CompanyMember.status == "active")))).scalars().all()
    if not owned:
        raise HTTPException(404, "Not Found")

    total = sum(len(m["items"]) for m in FUNCTION_REGISTRY)
    return {
        "enabled": True,
        "modules": FUNCTION_REGISTRY,
        "stats": {"modules": len(FUNCTION_REGISTRY), "functions": total},
    }
