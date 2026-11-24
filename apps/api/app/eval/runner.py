"""评测 runner：严格指标校验、可复算哈希、双向盲评与套件聚合。"""
from __future__ import annotations

from .judge import (BLIND_PROMPT_VERSION, VERDICTS, blind_compare,
                    canonical_sha256, sha256_text)

RUBRIC_VERSION = "deterministic-six-dimensions-v1"


def nonnegative_int(value, field: str) -> int:
    """Token/latency are counts: booleans, floats and numeric strings are invalid."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def canonical_snapshot(workflow_snapshot) -> dict | None:
    """Copy a graph snapshot and canonicalise order-insensitive node/edge arrays."""
    if not isinstance(workflow_snapshot, dict):
        return workflow_snapshot
    snapshot = dict(workflow_snapshot)
    nodes = snapshot.get("nodes")
    if isinstance(nodes, list):
        snapshot["nodes"] = sorted(nodes, key=lambda n: canonical_sha256(n))
    edges = snapshot.get("edges")
    if isinstance(edges, list):
        snapshot["edges"] = sorted(edges, key=lambda e: canonical_sha256(e))
    return snapshot


def content_fingerprint(*, prompt: str, workflow_snapshot, source_input_data,
                        model_identity: dict, rubric_version: str = RUBRIC_VERSION,
                        baseline_kind: str = "single_agent") -> str:
    """Fingerprint every source input before outputs exist.

    The complete execution input (not just its prompt) and a canonical graph
    snapshot are bound to the model/rubric/judge identity. Output hashes remain
    independent evidence recorded after execution.
    """
    return canonical_sha256({
        "prompt": prompt, "source_input_data": source_input_data,
        "workflow_snapshot": canonical_snapshot(workflow_snapshot),
        "model": model_identity, "rubric_version": rubric_version,
        "baseline_kind": baseline_kind, "judge_prompt_version": BLIND_PROMPT_VERSION,
    })


async def run_case(case: dict, judge_fn=None) -> dict:
    multi = case.get("multi", "")
    baseline = case.get("baseline", "")
    if not isinstance(multi, str) or not isinstance(baseline, str):
        raise ValueError("multi and baseline outputs must be strings")
    metrics = {
        name: nonnegative_int(case.get(name, 0), name)
        for name in ("multi_tokens", "baseline_tokens",
                     "multi_latency_ms", "baseline_latency_ms")
    }
    prompt = case.get("prompt", "")
    snapshot = canonical_snapshot(case.get("workflow_snapshot"))
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")

    cmp = await blind_compare(multi, baseline, judge_fn=judge_fn)
    if cmp["verdict"] not in VERDICTS:
        raise ValueError("invalid verdict returned by judge")
    return {
        "case_key": case.get("key"),
        "model_tier": case.get("model_tier", "unknown"),
        "verdict": cmp["verdict"],
        "scores": cmp["scores"],
        "blind": cmp["blind"],
        "judge": cmp["judge"],
        "tokens": {"multi": metrics["multi_tokens"],
                   "baseline": metrics["baseline_tokens"]},
        "latency_ms": {"multi": metrics["multi_latency_ms"],
                       "baseline": metrics["baseline_latency_ms"]},
        "hashes": {
            "prompt": sha256_text(prompt) if "prompt" in case else None,
            "workflow_snapshot": (canonical_sha256(snapshot)
                                  if "workflow_snapshot" in case else None),
            "multi_output": sha256_text(multi),
            "baseline_output": sha256_text(baseline),
        },
        "content_fingerprint": (content_fingerprint(
            prompt=prompt, source_input_data=case.get("source_input_data"),
            workflow_snapshot=snapshot,
            model_identity=case.get("model_identity") or {},
            rubric_version=case.get("rubric_version", RUBRIC_VERSION),
            baseline_kind=case.get("baseline_kind", "single_agent"))
            if "workflow_snapshot" in case and "model_identity" in case else None),
        "rubric_version": case.get("rubric_version", RUBRIC_VERSION),
    }


async def run_suite(cases: list[dict], judge_fn=None) -> dict:
    results = [await run_case(c, judge_fn) for c in cases]
    by_tier: dict[str, dict] = {}
    for r in results:
        t = by_tier.setdefault(r["model_tier"], {
            "cases": 0, "multi_better": 0, "tie": 0, "baseline_better": 0,
            "unavailable": 0, "multi_tokens": 0, "baseline_tokens": 0,
        })
        t["cases"] += 1
        t[r["verdict"]] += 1
        t["multi_tokens"] += r["tokens"]["multi"]
        t["baseline_tokens"] += r["tokens"]["baseline"]
    for t in by_tier.values():
        decided = t["multi_better"] + t["baseline_better"]
        t["win_rate"] = (t["multi_better"] / decided) if decided else None
        t["token_ratio"] = (round(t["multi_tokens"] / t["baseline_tokens"], 3)
                            if t["baseline_tokens"] else None)
    return {"cases": results, "by_tier": by_tier, "total": len(results)}
