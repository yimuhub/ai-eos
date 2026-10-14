"""公文写作技能 (P4)。

为数字员工注入 GB/T 9704-2012 公文排版能力：
- 公文结构化提示词模板（请示/报告/方案/通知）
- 排版规则（字号/页边距/行距规范）
- 常见公文错误自检清单
"""
from app.core.logging_config import get_logger

logger = get_logger(__name__)

PUBLIC_DOC_TEMPLATES = {
    "报告": """# 报告

## 一、基本情况
（背景、数据、现状）

## 二、主要做法
（采取的措施、实施过程）

## 三、取得成效
（量化成果、典型经验）

## 四、存在问题
（客观分析短板）

## 五、下一步打算
（目标、措施、时限）

---
**排版规范 GB/T 9704-2012：**
- 标题：二号小标宋体，居中
- 正文：三号仿宋体，首行缩进两字符
- 一级标题：黑体，二级标题：楷体
- 行距约 29.45 磅（10.39mm），A4 纸，上 37mm/下 25mm/左 28mm/右 26mm
""",

    "请示": """# 请示

一、请示缘由
（背景、必要性、政策依据）

二、请示事项
（具体请求内容，一条一请）

三、建议方案
（提供 2-3 个可行方案并推荐其一）

四、结语
（妥否，请批示 / 以上请示如无不妥，请批准）

---
**排版规范：** 同上。请示应一事一请，结尾明确标注"妥否，请批示"。
""",

    "方案": """# 实施方案

## 一、总体要求
（指导思想、基本原则、工作目标）

## 二、重点任务
（分项列出具体措施，可含时间表）

## 三、保障措施
（组织、资金、技术、考核）

## 四、实施步骤
（准备阶段 → 实施阶段 → 总结阶段）

---
**排版规范：** 同上。方案应可执行、可考核、有预算。
""",

    "会议纪要": """# 会议纪要

会议时间：XXXX年XX月XX日
会议地点：XXXX
主持人：XXX
出席人员：XXX、XXX、XXX
记录人：XXX

一、会议议题
（简述）

二、会议内容
（逐项记录讨论要点、结论）

三、会议决定
（明确的决议事项、责任主体、完成时限）

---
**排版规范：** 纪要标题用二号小标宋，正文三号仿宋。
""",
}


def get_public_doc_system_prompt(role: str | None = None, doc_type: str = "报告") -> str:
    """生成公文写作专用的系统提示词。"""
    tpl = PUBLIC_DOC_TEMPLATES.get(doc_type, PUBLIC_DOC_TEMPLATES["报告"])
    role_hint = f"（岗位：{role}）" if role else ""
    return f"""你是「{role or '专业员工'}」{role_hint}，具备规范的公文写作能力。

## 公文写作标准（GB/T 9704-2012）
- 标题用「# 标题」格式，字号二号小标宋（最终 Word 渲染时由 doc_generator 处理）
- 正文分段清晰，使用一级/二级标题
- 数据准确，来源可标注（如"据统计局 2025 年数据"）
- 涉及金额、时间、数字统一使用阿拉伯数字

## 输出格式
按以下模板结构输出：
{tpl}

## 自检清单
- [ ] 标题是否符合文种要求
- [ ] 数据是否有来源
- [ ] 措施是否可执行、可考核
- [ ] 格式是否符合国标 GB/T 9704-2012
- [ ] 是否存在歧义或矛盾表述
"""


def generate_ppt_from_report(title: str, content: str) -> dict:
    """将报告内容转成 PPT 格式（markdown 分页）。"""
    lines = []
    for para in content.split("\n"):
        if para.strip().startswith("#"):
            lines.append(para)
    slides = []
    cur = []
    for line in lines:
        if line.startswith("## ") or line.startswith("# "):
            if cur:
                slides.append("\n".join(cur))
                cur = []
            cur.append(line)
        else:
            cur.append(line)
    if cur:
        slides.append("\n".join(cur))

    ppt_content = ""
    for i, slide in enumerate(slides[:10]):  # 最多 10 页
        # 提取标题和要点
        parts = [p.strip() for p in slide.split("\n") if p.strip()]
        if parts:
            st = parts[0].lstrip("# ")
            bullets = [f"- {p}" for p in parts[1:6]]
            ppt_content += f"\n## {st}\n" + "\n".join(bullets)

    if not ppt_content:
        ppt_content = f"## {title}\n- 详见报告内容"

    return {"title": title, "content": ppt_content}


def generate_word_from_report(title: str, content: str) -> dict:
    """将报告内容包装成 Word 文档提示。"""
    return {"title": title, "content": content}


def generate_excel_from_data(data: list[dict], title: str) -> dict:
    """将结构化数据转成 Excel 提示。"""
    if not data:
        return {"title": title, "content": "（无数据）"}
    headers = list(data[0].keys())
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in data[:50]:  # 最多 50 行
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return {"title": title, "content": "\n".join(lines)}


# ---------------- Agent 专属格式风格 ----------------
# 有对应技能的 Agent 用专属排版；没有的用通用格式。

AGENT_DOC_STYLES = {
    "文案": {
        "style_name": "内容创作风",
        "hint": ("文风优美流畅，善用比喻和场景描写；标题有吸引力；"
                 "正文段落富有感染力；结尾有金句点题。避免表格化表达，多用叙事。"),
    },
    "战略分析师": {
        "style_name": "商业分析风",
        "hint": ("结构化为：执行摘要→市场数据→竞争分析→SWOT→结论建议。"
                 "大量使用数据表格呈现对比，每个判断附依据来源。"),
    },
    "数据分析": {
        "style_name": "数据报告风",
        "hint": ("以数据表格和指标为核心：先给关键指标摘要表，再逐项解读。"
                 "数字保留两位小数，同比环比明确标注。"),
    },
    "客服": {
        "style_name": "客户服务风",
        "hint": ("语气亲切专业，使用 FAQ 结构（问→答）；"
                 "每条建议都给出具体可操作的步骤；结尾附服务承诺。"),
    },
    "项目管理": {
        "style_name": "项目管理风",
        "hint": ("以任务清单和甘特式时间表为核心：任务-负责人-截止时间三列表格；"
                 "里程碑加粗标注；风险项单独列风险登记表。"),
    },
}

# 关键词 → 风格映射（Agent 无显式技能时按岗位名匹配）
_KEYWORD_STYLE = {
    "文案": "文案", "内容": "文案", "写作": "文案", "创意": "文案",
    "战略": "战略分析师", "分析": "数据分析", "数据": "数据分析",
    "客服": "客服", "项目": "项目管理", "PM": "项目管理",
}


def get_agent_doc_style(agent) -> dict:
    """检测 Agent 应使用的文档风格。
    优先级：已安装技能 > 岗位名关键词 > 通用格式。"""
    # 1. 按已安装技能判断（需要 db 会话时由调用方传入 skills 列表）
    return {}


def resolve_style(role: str | None, skill_names: list[str] | None = None,
                  custom_style: dict | None = None) -> dict:
    """返回 {style_name, hint, is_custom}。
    custom_style={"name","hint"} 为老板 DIY 的公司风格，最高优先级。
    is_custom=False 表示通用格式。"""
    # 0. 老板手动绑定的自定义风格（DIY）优先
    if custom_style and custom_style.get("hint"):
        return {"style_name": custom_style.get("name") or "自定义风格",
                "hint": custom_style["hint"], "is_custom": True}
    # 技能名优先
    if skill_names:
        for sn in skill_names:
            for kw, style_key in _KEYWORD_STYLE.items():
                if kw in (sn or "").lower() or kw in sn:
                    st = AGENT_DOC_STYLES.get(style_key)
                    if st:
                        return {**st, "is_custom": True}
    # 岗位名匹配
    if role:
        for kw, style_key in _KEYWORD_STYLE.items():
            if kw in role:
                st = AGENT_DOC_STYLES.get(style_key)
                if st:
                    return {**st, "is_custom": True}
    return {"style_name": "通用公文风", "is_custom": False,
            "hint": "符合 GB/T 9704-2012 的标准公文格式，结构清晰、用语规范。"}


async def get_bound_style(db, agent) -> dict | None:
    """读取 Agent 绑定的 DIY 风格（doc_style_id → doc_styles 表）。"""
    if not getattr(agent, "doc_style_id", None):
        return None
    try:
        from app.api.routes.doc_styles import DocStyle
        row = await db.get(DocStyle, agent.doc_style_id)
        if row:
            return {"name": row.name, "hint": row.hint}
    except Exception as style_err:
        # 读不到专属风格时应回落通用格式；但失败原因必须可见，
        # 否则用户会以为"我配的风格没生效"却查不到任何线索
        logger.warning("读取 Agent 专属文档风格失败 agent=%s style_id=%s: %s",
                       getattr(agent, "id", "?"), agent.doc_style_id,
                       style_err, exc_info=True)
    return None
