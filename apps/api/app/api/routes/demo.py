"""演示公司种子（Demo Seed）：新用户一键预置一家可直接浏览的演示公司。

* POST /demo/seed   给当前用户预置一家演示公司（幂等：已存在即返回，不重复建行）

安全边界：种子数据全部归属**当前用户自己**（Company.owner_id = me），
不写任何跨租户/他人数据，因此不需要 CompanyMember 镜像行（owner 即可，
resolve_role 按 companies.owner_id 事实源判 OWNER），也不存在越权面。

幂等：按「owner=me 且 name=演示公司名」判重。重复点击、前端超时重试
都不会翻倍建行；用户把演示公司改过名之后再种，会得到一家新的演示公司
（改名 = 他把它当成自己的公司在用了，这是有意的语义）。

演示员工**不绑定任何模型**（不建 AgentModelConfig 行）：演示公司不需要
配置任何密钥即可浏览；首次点"运行"时再由用户自己选择模型。
"""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent import Agent, AgentStatus
from app.models.artifact import Artifact, ArtifactStatus, ArtifactVersion
from app.models.company import Company, Department
from app.models.task import Task, TaskEventType, TaskPriority, TaskStatus
from app.models.user import User
from app.services.event_system import log_event
from app.task_engine import add_task_event

router = APIRouter(prefix="/demo", tags=["demo"])

# 幂等判重的唯一键：同一名下最多一家同名演示公司
DEMO_COMPANY_NAME = "示例公司（演示）"

# ---------------------------------------------------------------------------
# 演示文案：内容较长，收拢成模块级常量，路由主体只保留「建行 + 返回」。
# ---------------------------------------------------------------------------

COMPANY_DESC = ("预置演示数据：一家内容创意工作室，供新用户快速浏览组织、"
                "交付与任务功能；所有数据均可随时修改或删除。")

# 创意总监：演示团队的 leader（manager_agent_id=None，其余员工向他汇报）
PROMPT_CHENMO = ("你是创意总监陈墨，十二年的广告人，擅长把模糊的想法收敛成能落地的"
                 "创意方案。你负责定方向、审产出、带团队：接到需求先追问目标与受众，"
                 "评审时直接给结论和理由，重视洞察而不是华丽辞藻。")
PROMPT_LIN = ("你是文案策划林小满，擅长社媒种草文与品牌文案，笔感轻盈、像真人说话。"
              "写稿前先想清楚给谁看、在什么场景刷到；动笔先立钩子，再铺种草点，"
              "最后落到行动号召，交付前自查一遍错别字。")
PROMPT_GU = ("你是视觉设计师顾小舟，负责海报、封面与版式设计，审美克制、信息优先。"
             "每次交付都会说明构图逻辑与配色理由，拒绝为了好看牺牲可读性；"
             "遇到模糊需求，先出两版方向再和对方对齐。")
PROMPT_ALAN = ("你是运营专员阿岚，负责账号运营与数据复盘，习惯用数据说话。"
               "每周整理互动、涨粉与转化数据，先给结论再给证据，最后附上可执行的"
               "下一步建议；发现异常波动会第一时间上报，而不是自行脑补原因。")

# 演示交付单 1：小红书种草文案（IN_REVIEW，钩子 + 3 个种草点 + 行动号召 + 标签）
DEMO_XHS_COPY = """姐妹们！国庆别再往网红城市冲了！！今年我反着来，挑了座高铁 2h 直达的小城，5 天下来人均不到 1500，人少、便宜、出片，一次全满足🍂

🏞 种草点 1：体验感直接拉满
老街的青石板路清晨只有扫地的阿婆，随手一拍都是空镜大片。民宿老板娘还带我们去赶早市，挑刚出锅的梅花糕——这种体验在大景区花钱都买不到～

🍜 种草点 2：美食密度高到离谱
一条街从街头吃到街尾：现拆蟹粉小笼、炭烤梅干菜烧饼、冰镇桂花米酒，人均 40 撑到扶墙。我们把每天的行程排成「早茶—老馆子—夜宵」三段式，胃和心情全程满格📈

🚲 种草点 3：性价比是真的高
高铁往返 400 出头，设计感民宿国庆价只要 280/晚，比热门城市省出一半，省下的钱刚好够拎两盒伴手礼回家🎁

真的建议国庆别躺家里刷手机了！收藏这条路线现在订票还来得及，评论区蹲个同款搭子，一起去小城发呆～

#国庆出游 #小众旅行地 #旅行攻略 #国庆去哪玩 #治愈系小城"""

# 演示交付单 2：周报体（APPROVED，数据为占位符）
DEMO_WEEKLY_REPORT = """「山屿内容工作室」品牌运营周报（10 月第 1 周）

本周围绕国庆节点开展内容与投放双线运营，整体数据稳中有升，小结如下。

一、核心数据（演示占位，以后台实际统计为准）
- 全渠道新增粉丝 3,240，环比 +12%
- 内容总曝光 8.6w，互动 1.2w，互动率 13.9%
- 私域新增 412 人，进群转化率 6.8%
- 小红书爆文 2 篇，最高单篇曝光 2.3w

二、本周动作
1. 上线「国庆出游」种草合集 3 篇，其中 1 篇进入商搜池；
2. 直播 2 场，GMV 占比 35%，客单价环比提升 9%；
3. 完成 10 月竞品投放监测简报，素材库更新 42 条。

三、问题与风险
- 中短视频完播率 45%，低于大盘（52%），脚本节奏需前移；
- 咨询高峰客服响应升至 3 分钟，建议增补 1 名排班人力。

四、下周计划
1. 上线国庆返程「错峰种草」第二波内容；
2. 复盘爆文结构，沉淀 3 套可复用选题模板；
3. 启动 11 月会员日预热排期与素材储备。

数据截至本周日 24:00。负责人：运营部·阿岚。"""

DEMO_TASK_DESC = ("面向 25~35 岁的年轻用户，写一篇国庆出游的小红书种草文案："
                  "开头要有吸引人的钩子，正文给出 3 个种草点（体验、美食、性价比"
                  "任选角度），结尾附行动号召与话题标签；全文 300~500 字，语气像"
                  "真人分享，避免广告腔。成稿请提交到交付中心送审。")


class DemoSeedOut(BaseModel):
    """POST /demo/seed 响应。幂等命中时只填前三项（配合 exclude_unset）。"""

    already_seeded: bool
    company_id: str
    company_name: str
    agents: int | None = None        # 本次新建的员工数（仅首次种子返回）
    deliverables: int | None = None  # 本次新建的交付单数（恒为 2）
    task_id: str | None = None       # 演示任务 id（仅首次种子返回）


def _uid() -> str:
    """显式生成 uuid4 hex 主键：下属行（部门/员工/交付/任务）互相引用时
    不依赖 flush 顺序，整个种子就是一个事务、一次 commit。"""
    return uuid.uuid4().hex


@router.post("/seed", response_model=DemoSeedOut, response_model_exclude_unset=True)
async def seed_demo_company(db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """给当前用户预置一家可直接浏览的演示公司（幂等）。

    已有同名演示公司 → already_seeded=true，原样返回，不动任何数据；
    否则在一个事务里建齐：公司 + 2 部门 + 4 员工 + 2 交付单 + 1 任务。
    """
    # ---- 幂等闸门：先查再建，重复请求零写入 ----
    q = await db.execute(
        select(Company).where(Company.owner_id == user.id,
                              Company.name == DEMO_COMPANY_NAME)
        .order_by(Company.created_at).limit(1))
    existing = q.scalars().first()
    if existing:
        return DemoSeedOut(already_seeded=True, company_id=str(existing.id),
                           company_name=existing.name)

    # ---- 公司：一次请求、一个事务（全部 id 预生成，末尾统一 commit） ----
    # UOW 只按 relationship 排插入顺序，裸 FK 表之间的先后并不保证
    # （Company→Agent 没有声明关系，实测插入顺序会随会话内容漂移），
    # 因此每铺一层就显式 flush 一次：顺序自己说了算，仍在同一个事务里。
    company = Company(id=_uid(), owner_id=user.id, name=DEMO_COMPANY_NAME,
                      description=COMPANY_DESC)
    db.add(company)
    await db.flush()

    # ---- 部门：创意部 / 运营部。创意负责人即团队 leader，挂在部门上便于组织树展示 ----
    leader_id = _uid()
    creative = Department(id=_uid(), company_id=company.id, name="创意部",
                          description="负责文案、设计与创意产出",
                          manager_agent_id=leader_id)
    ops = Department(id=_uid(), company_id=company.id, name="运营部",
                     description="负责账号运营、数据复盘与用户增长")
    db.add_all([creative, ops])
    await db.flush()

    # ---- 员工：4 名，全部 IDLE、不绑模型（演示公司不需要密钥即可浏览） ----
    chenmo = Agent(id=leader_id, company_id=company.id,
                   department_id=creative.id,
                   name="创意总监·陈墨", role="创意总监",
                   description="定创意方向，审团队产出",
                   system_prompt=PROMPT_CHENMO,
                   status=AgentStatus.IDLE, manager_agent_id=None,
                   created_by_user_id=user.id)
    lin = Agent(id=_uid(), company_id=company.id, department_id=creative.id,
                name="文案策划·林小满", role="文案策划",
                description="社媒种草文与品牌文案",
                system_prompt=PROMPT_LIN,
                status=AgentStatus.IDLE, manager_agent_id=leader_id,
                created_by_user_id=user.id)
    gu = Agent(id=_uid(), company_id=company.id, department_id=creative.id,
               name="视觉设计师·顾小舟", role="视觉设计师",
               description="海报、封面与版式设计",
               system_prompt=PROMPT_GU,
               status=AgentStatus.IDLE, manager_agent_id=leader_id,
               created_by_user_id=user.id)
    alan = Agent(id=_uid(), company_id=company.id, department_id=ops.id,
                 name="运营专员·阿岚", role="运营专员",
                 description="账号运营与数据复盘",
                 system_prompt=PROMPT_ALAN,
                 status=AgentStatus.IDLE, manager_agent_id=leader_id,
                 created_by_user_id=user.id)
    # 跨部门汇报：阿岚（运营部）也向创意总监汇报，与演示团队设定一致
    db.add_all([chenmo, lin, gu, alan])
    await db.flush()

    # ---- 交付单：1 份 IN_REVIEW（种草文案）+ 1 份 APPROVED（运营周报） ----
    art_xhs = Artifact(id=_uid(), company_id=company.id, project_id=None,
                       name="国庆出游小红书种草文案（演示）",
                       artifact_type="document", current_version=1,
                       status=ArtifactStatus.IN_REVIEW, created_by=user.id)
    v_xhs = ArtifactVersion(id=_uid(), artifact_id=art_xhs.id, version=1,
                            content=DEMO_XHS_COPY, change_note="演示初始版本",
                            status=ArtifactStatus.IN_REVIEW,
                            submitted_by=lin.id)
    art_report = Artifact(id=_uid(), company_id=company.id, project_id=None,
                          name="品牌月度运营周报（演示）",
                          artifact_type="document", current_version=1,
                          status=ArtifactStatus.APPROVED, created_by=user.id)
    # 周报是运营产出、创意总监审过：把审核痕迹一并铺好，交付中心看起来更真实
    v_report = ArtifactVersion(id=_uid(), artifact_id=art_report.id, version=1,
                               content=DEMO_WEEKLY_REPORT,
                               change_note="演示初始版本",
                               status=ArtifactStatus.APPROVED,
                               submitted_by=alan.id, reviewed_by=chenmo.id,
                               review_comment="结构与数据口径清晰，通过。")
    db.add_all([art_xhs, art_report])
    await db.flush()
    db.add_all([v_xhs, v_report])

    # ---- 演示任务：TODO + auto_start=False，点"运行"才真正开工 ----
    task = Task(id=_uid(), company_id=company.id,
                title="写一篇国庆出游的小红书种草文案",
                description=DEMO_TASK_DESC,
                status=TaskStatus.TODO, priority=TaskPriority.NORMAL,
                assigned_agent_id=lin.id, created_by=user.id,
                auto_start=False)
    db.add(task)
    await db.flush()  # 任务行先落库，TaskEvent 外键才有着落
    await add_task_event(db, task.id, TaskEventType.CREATED, "user", user.id,
                         {"title": task.title})
    await log_event(db, "company.created", "company", None, "user", user.id,
                    {"name": DEMO_COMPANY_NAME, "demo": True})

    # ---- 单点提交：以上任何一步失败，get_db 的 session 关闭会整体回滚 ----
    await db.commit()
    return DemoSeedOut(already_seeded=False, company_id=str(company.id),
                       company_name=company.name, agents=4, deliverables=2,
                       task_id=str(task.id))
