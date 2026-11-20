"""评测集执行：把成对产出喂给盲评，聚合成胜率/成本对比。

刻意不在这里调模型：产出由调用方生成（真实场景走工作流与单次 prompt，
测试传固定文本）。runner 只做"评分 → 盲评 → 汇总"，保证可复算。
"""
from __future__ import annotations

from .judge import blind_compare


async def run_case(case: dict, judge_fn=None) -> dict:
    cmp = await blind_compare(case.get("multi") or "", case.get("baseline") or "",
                              judge_fn=judge_fn)
    return {
        "case_key": case.get("key"),
        "model_tier": case.get("model_tier", "unknown"),
        "verdict": cmp["verdict"],
        "scores": cmp["scores"],
        "blind": cmp["blind"],
        "tokens": {"multi": int(case.get("multi_tokens") or 0),
                   "baseline": int(case.get("baseline_tokens") or 0)},
        "latency_ms": {"multi": int(case.get("multi_latency_ms") or 0),
                       "baseline": int(case.get("baseline_latency_ms") or 0)},
    }


async def run_suite(cases: list[dict], judge_fn=None) -> dict:
    results = [await run_case(c, judge_fn) for c in cases]
    by_tier: dict[str, dict] = {}
    for r in results:
        t = by_tier.setdefault(r["model_tier"], {"cases": 0, "multi_better": 0,
                                                 "tie": 0, "baseline_better": 0,
                                                 "multi_tokens": 0, "baseline_tokens": 0})
        t["cases"] += 1
        t[r["verdict"]] += 1
        t["multi_tokens"] += r["tokens"]["multi"]
        t["baseline_tokens"] += r["tokens"]["baseline"]
    for t in by_tier.values():
        decided = t["multi_better"] + t["baseline_better"]
        t["win_rate"] = (t["multi_better"] / decided) if decided else None
        t["token_ratio"] = (round(t["multi_tokens"] / t["baseline_tokens"], 3)
                            if t["baseline_tokens"] else None)
    return {"cases": results, "by_tier": by_tier,
            "total": len(results)}
