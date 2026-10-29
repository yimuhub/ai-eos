"""专业报告骨架：让 Agent 写出来的不是一段散文，而是可交付的报告。

为什么需要：任务链只给模型一句"完成这个任务"，模型自由发挥的结果是
没有章节、没有数据表、没有出处的长文。行业里可交付的研究/经营报告
有稳定骨架（摘要→数据→结论→来源），把它变成提示词约束，比换模型
更能提升成品质感。

设计取舍：
* 不建表、不做模板编辑器 —— 模板是代码里的常量（随版本走），
  以后要做自定义模板再加表；
* 只按任务标题/描述做关键词识别，不做 LLM 二次分类（省一次调用、
  结果可预测）；命中就注入骨架，未命中不打扰。
"""
from __future__ import annotations

REPORT_TEMPLATES: dict[str, dict] = {
    "industry_research": {
        "name": "行业研究报告",
        "keywords": ("行业研究", "行业分析", "市场研究", "市场分析", "赛道",
                     "调研报告", "竞品", "市场规模"),
        "sections": [
            "执行摘要", "市场规模与增长", "政策与监管环境", "竞争格局",
            "目标客户与需求", "趋势与机会", "风险与不确定性", "结论与建议",
            "参考来源",
        ],
    },
    "business_analysis": {
        "name": "经营分析报告",
        "keywords": ("经营分析", "运营分析", "经营报告", "财务分析",
                     "营收", "成本结构", "月度经营"),
        "sections": [
            "核心结论", "关键指标概览", "收入与成本结构", "客户与渠道分析",
            "问题诊断", "行动计划", "数据附表", "参考来源",
        ],
    },
    "weekly_report": {
        "name": "周报/月报",
        "keywords": ("周报", "月报", "工作汇报", "工作总结", "周总结"),
        "sections": [
            "本期概述", "完成事项", "关键进展与数据", "问题与阻塞",
            "下期计划", "需要老板决策",
        ],
    },
    "project_review": {
        "name": "项目复盘",
        "keywords": ("复盘", "项目总结", "事后分析", "回顾"),
        "sections": [
            "目标与结果对照", "关键时间线", "做对了什么", "问题与根因",
            "改进项与责任人", "附录",
        ],
    },
    "proposal": {
        "name": "方案/计划书",
        "keywords": ("方案", "计划书", "商业计划", "提案", "策划案"),
        "sections": [
            "背景与目标", "核心方案", "实施步骤与里程碑", "资源与预算",
            "风险与应对", "预期收益", "附录",
        ],
    },
}

# 数据类报告额外要求：至少一张数据表
DATA_TEMPLATES = {"industry_research", "business_analysis", "weekly_report"}


def detect_report_template(title: str | None, description: str | None) -> str | None:
    """命中关键词返回模板 id；未命中返回 None。"""
    text = f"{title or ''} {description or ''}"
    best: tuple[int, str] | None = None
    for tid, tpl in REPORT_TEMPLATES.items():
        hits = sum(1 for kw in tpl["keywords"] if kw in text)
        if hits and (best is None or hits > best[0]):
            best = (hits, tid)
    return best[1] if best else None


def build_report_instruction(template_id: str) -> str:
    """报告骨架 + 交付规范（直接作为 system 消息注入）。"""
    tpl = REPORT_TEMPLATES[template_id]
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(tpl["sections"], 1))
    data_rule = (
        "报告必须至少包含一张 Markdown 数据表（指标｜数值｜口径/来源），"
        "数字必须来自搜索或抓取结果，不得虚构。"
        if template_id in DATA_TEMPLATES else
        "涉及数据时使用 Markdown 表格呈现，并标注口径与来源。"
    )
    return (
        f"【报告规范｜{tpl['name']}】本次任务是交付一份可对外使用的"
        f"{tpl['name']}，请严格按以下章节组织（可合并但不可缺失）：\n{sections}\n\n"
        "写作要求：\n"
        f"1. {data_rule}\n"
        "2. 先给结论，再给论据；每个关键判断后面标注依据（来源名称或 URL）。\n"
        "3. 没有取到的数据写「暂缺（建议采集方式：…）」，不要估算冒充事实。\n"
        "4. 正文用 Markdown：章节用 ## ，要点用 - ，表格用 | 竖线语法。\n"
        "5. 正文写完后，在最后输出一次文档标记，系统会渲染成真实 Word 文件：\n"
        "[GENERATE_DOC]\n"
        '{"type":"word","title":"报告标题","content":"（与正文相同的完整 Markdown）"}\n'
        "[/GENERATE_DOC]\n"
        + ("6. 数据表另存一份 Excel：再输出一个 type=excel 的文档标记，content 只放表格。\n"
           if template_id in DATA_TEMPLATES else "")
    )


def list_templates() -> list[dict]:
    return [{"id": tid, "name": tpl["name"], "sections": tpl["sections"]}
            for tid, tpl in REPORT_TEMPLATES.items()]
