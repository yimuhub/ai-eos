"""确定性评分表：能用规则算的先算，模型只补主观分。

六个维度全部可复算，客户拿到报告能自己数：

| 维度 | 权重 | 判据 |
|---|---|---|
| conclusion | 0.25 | 有"结论/建议/综上"之类收口段 |
| citations | 0.20 | 至少 1 个可点来源（URL / 来源：xxx） |
| length | 0.15 | 有效字 ≥ 300（CJK 逐字 + 英文逐词） |
| structure | 0.15 | ≥ 2 个小标题（## / 一、/ 1.） |
| no_cliche | 0.10 | 不出现"作为 AI / 总而言之 / 赋能"等套话 |
| no_gap | 0.15 | 不出现"待补充 / TODO / 缺件"等未完成标记 |
"""
from __future__ import annotations

import re

WEIGHTS = {"conclusion": 0.25, "citations": 0.20, "length": 0.15,
           "structure": 0.15, "no_cliche": 0.10, "no_gap": 0.15}
CLICHES = ("作为ai", "作为人工智能", "总而言之", "赋能", "闭环抓手", "底层逻辑")
GAPS = ("待补充", "todo", "缺件", "尚未提供", "此处省略")


def effective_chars(text: str) -> int:
    """CJK 逐字 + 英文逐词（与 M1 的 min_chars 口径一致）。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    words = len(re.findall(r"[A-Za-z0-9]+", text or ""))
    return cjk + words


def _has_conclusion(text: str) -> bool:
    return bool(re.search(r"(^|\n)\s*(#{1,3}\s*)?(结论|综上|建议|小结|最终判断)", text or ""))


def _has_citation(text: str) -> bool:
    return bool(re.search(r"https?://|来源[:：]|出处[:：]", text or ""))


def _has_structure(text: str) -> bool:
    heads = re.findall(r"(?m)^\s*(#{2,3}\s+\S+|[一二三四五六七八九十]+、|第[一二三四五六七八九十]+[章节])", text or "")
    return len(heads) >= 2


def score_text(text: str) -> dict:
    text = text or ""
    checks = {
        "conclusion": _has_conclusion(text),
        "citations": _has_citation(text),
        "length": effective_chars(text) >= 300,
        "structure": _has_structure(text),
        "no_cliche": not any(c in text.lower() for c in CLICHES),
        "no_gap": not any(g in text.lower() for g in GAPS),
    }
    dims = {k: (1.0 if v else 0.0) for k, v in checks.items()}
    total = round(sum(WEIGHTS[k] * dims[k] for k in WEIGHTS), 4)
    return {"total": total, "dims": dims, "checks": checks,
            "effective_chars": effective_chars(text),
            "weights": WEIGHTS}
