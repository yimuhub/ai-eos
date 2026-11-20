"""双向盲评：评审不知道哪份是多 Agent 产出，A/B 顺序互换评两次。

- 两次结论一致 → 采纳；
- 不一致 → `tie`（宁可说"没差别"，也不拿一次偏好当结论）。
- 评审函数由调用方注入（真实场景传模型调用，测试传假函数）；异常/格式不对
  一律视为"这次评不了"，最终落 `tie`，绝不因为评审挂了就判多 Agent 输。
"""
from __future__ import annotations

from .rubric import score_text

VERDICTS = ("multi_better", "tie", "baseline_better")


def _from_score(a: float, b: float, margin: float = 0.05) -> str:
    if a - b > margin:
        return "multi_better"
    if b - a > margin:
        return "baseline_better"
    return "tie"


async def blind_compare(multi_text: str, baseline_text: str,
                        judge_fn=None, margin: float = 0.05) -> dict:
    """返回 verdict + 两套分数 + 两次评审原始结论。

    `judge_fn(prompt) -> "A" | "B" | "tie" | {"winner": ...}`，
    由调用方决定用哪个模型、怎么解析；这里只负责"换序两次 + 一致性"。
    """
    s_multi = score_text(multi_text)
    s_base = score_text(baseline_text)

    if judge_fn is None:
        verdict = _from_score(s_multi["total"], s_base["total"], margin)
        return {"verdict": verdict, "scores": {"multi": s_multi, "baseline": s_base},
                "blind": {"order1": None, "order2": None, "agreed": None},
                "judge": None}

    def _winner(r) -> str:
        if isinstance(r, dict):
            r = r.get("winner") or r.get("verdict")
        r = str(r or "").strip().lower()
        if r in ("a", "multi", "multi_better"):
            return "multi"
        if r in ("b", "baseline", "baseline_better"):
            return "baseline"
        return "tie"

    prompt_1 = (f"A:\n{multi_text}\n\nB:\n{baseline_text}\n\n"
                "只判断哪份更好，输出 A 或 B 或 tie，不要解释。")
    prompt_2 = (f"A:\n{baseline_text}\n\nB:\n{multi_text}\n\n"
                "只判断哪份更好，输出 A 或 B 或 tie，不要解释。")

    first, second = "tie", "tie"
    try:
        first = _winner(await judge_fn(prompt_1))
        second = _winner(await judge_fn(prompt_2))
    except Exception:  # noqa: silent-except — 评审不可用不判死，按 tie 收口
        return {"verdict": "tie", "scores": {"multi": s_multi, "baseline": s_base},
                "blind": {"order1": first, "order2": second, "agreed": False},
                "judge": {"available": False}}

    # 第一次 A=multi，第二次 A=baseline：把第二次翻回同一视角再比
    flipped = {"multi": "baseline", "baseline": "multi", "tie": "tie"}[second]
    agreed = first == flipped
    verdict = {"multi": "multi_better", "baseline": "baseline_better"}.get(first, "tie") \
        if agreed else "tie"
    return {"verdict": verdict,
            "scores": {"multi": s_multi, "baseline": s_base},
            "blind": {"order1": first, "order2": second, "agreed": agreed},
            "judge": {"available": True}}
