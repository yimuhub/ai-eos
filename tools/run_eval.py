#!/usr/bin/env python3
"""离线评测：对"成对产出（多 Agent vs 单次基线）"做确定性评分与汇总。

用法：
    cd apps/api
    .venv/bin/python ../../tools/run_eval.py cases.json [--json]

cases.json 形如：
```json
{"suite": "industry-research-v1",
 "cases": [
   {"key": "c1", "model_tier": "strong",
    "multi": "……多 Agent 产出……", "baseline": "……单次产出……",
    "multi_tokens": 1200, "baseline_tokens": 600}
 ]}
```

只做规则评分（六维全可复算），不调模型；需要模型盲评时把 verdict 写进
可选字段 `judge_verdict`（由人工或上层工具产生），脚本会如实标注来源。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from app.eval.runner import run_suite          # noqa: E402
from app.eval.rubric import WEIGHTS            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", help="cases.json 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    args = ap.parse_args()

    with open(args.cases, encoding="utf-8") as f:
        payload = json.load(f)
    suite = payload.get("suite") or os.path.basename(args.cases)
    cases = payload.get("cases") or []
    if not cases:
        print("没有 case", file=sys.stderr)
        return 2

    result = asyncio.run(run_suite(cases))
    if args.json:
        print(json.dumps({"suite": suite, **result}, ensure_ascii=False, indent=2))
        return 0

    print(f"# 评测：{suite}\n")
    print("评分口径（确定性，权重可复算）：" + "，".join(
        f"{k} {v}" for k, v in WEIGHTS.items()) + "\n")
    print("| case | 档位 | 多 Agent 分 | 基线分 | 结论 | token 比 |")
    print("|---|---|---|---|---|---|")
    for c in result["cases"]:
        mt, bt = c["tokens"]["multi"], c["tokens"]["baseline"]
        ratio = f"{mt / bt:.2f}" if bt else "—"
        print(f"| {c['case_key']} | {c['model_tier']} | "
              f"{c['scores']['multi']['total']:.2f} | "
              f"{c['scores']['baseline']['total']:.2f} | {c['verdict']} | {ratio} |")
    print("\n## 按模型档汇总\n")
    print("| 档位 | cases | multi_better | tie | baseline_better | 胜率 | token 比 |")
    print("|---|---|---|---|---|---|---|")
    for tier, t in result["by_tier"].items():
        rate = "—" if t["win_rate"] is None else f"{t['win_rate'] * 100:.0f}%"
        ratio = "—" if t["token_ratio"] is None else f"{t['token_ratio']:.2f}"
        print(f"| {tier} | {t['cases']} | {t['multi_better']} | {t['tie']} | "
              f"{t['baseline_better']} | {rate} | {ratio} |")
    print("\n> 胜率只统计明确胜负的 case；tie 不进分母。没有样本时不写 0%。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
