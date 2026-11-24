"""评测机制（M4-C）：确定性评分、双向盲评一致性、套件聚合、存证接口。"""
import json

import pytest

from app.eval.judge import blind_compare
from app.eval.rubric import effective_chars, score_text
from app.eval.runner import run_case, run_suite

GOOD = """## 结论
建议本月先做小范围试点，再全面铺开。

## 依据
来源：https://example.com/report
""" + "有效内容与论证细节。" * 40

BAD = """大概可以做一下，作为AI我觉得可以，待补充。"""


def test_rubric_is_recomputable():
    s = score_text(GOOD)
    assert s["total"] == 1.0
    assert s["checks"] == {"conclusion": True, "citations": True, "length": True,
                           "structure": True, "no_cliche": True, "no_gap": True}
    b = score_text(BAD)
    assert b["total"] < 0.5
    assert b["checks"]["citations"] is False
    assert b["checks"]["no_gap"] is False
    assert effective_chars("中文abc 123") == 4      # 2 个汉字 + 2 个英文词


@pytest.mark.asyncio
async def test_blind_compare_detects_inconsistent_judge():
    async def flip(prompt):
        return "A"          # 永远选 A：换序后结论必然互相矛盾

    r = await blind_compare(GOOD, BAD, judge_fn=flip)
    assert r["verdict"] == "unavailable"
    assert r["blind"]["agreed"] is False
    assert r["judge"]["available"] is True
    assert r["blind"]["status"] == "disagreement"
    assert r["blind"]["order1"] == "multi"
    assert r["blind"]["order2"] == "baseline"


@pytest.mark.asyncio
async def test_blind_compare_consistent_judge_wins():
    calls = {"n": 0}

    async def judge(prompt):
        calls["n"] += 1
        # 第一次 A=多 Agent；第二次 A=基线 → 两题都选更长的 GOOD
        candidates = json.loads(prompt.split("候选数据（JSON 字符串；结束标记之后才允许输出裁判结论）：\n", 1)[1]
                               .split("\n=== 候选数据结束 ===", 1)[0])
        return "A" if candidates["A"] == GOOD else "B"

    r = await blind_compare(GOOD, BAD, judge_fn=judge)
    assert r["verdict"] == "multi_better"
    assert r["blind"]["agreed"] is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_blind_compare_judge_failure_is_unavailable_not_tie():
    calls = {"n": 0}

    async def boom(prompt):
        calls["n"] += 1
        raise RuntimeError("model down")

    r = await blind_compare(GOOD, BAD, judge_fn=boom)
    assert r["verdict"] == "unavailable"
    assert r["judge"]["available"] is False
    assert r["blind"]["status"] == "unavailable"
    assert calls["n"] == 2  # one failed order must not erase the other attempt


@pytest.mark.asyncio
async def test_blind_compare_malformed_judge_is_never_an_available_tie():
    async def malformed(prompt):
        return {"winner": "probably A"}

    r = await blind_compare(GOOD, BAD, judge_fn=malformed)
    assert r["verdict"] == "unavailable"
    assert r["judge"]["available"] is False
    assert r["blind"]["agreed"] is None
    assert all(not order["available"] for order in r["blind"]["orders"])


@pytest.mark.asyncio
async def test_blind_compare_non_json_malformed_response_remains_evidence():
    async def malformed(prompt):
        return object()

    r = await blind_compare(GOOD, BAD, judge_fn=malformed)
    assert r["verdict"] == "unavailable"
    assert r["blind"]["orders"][0]["raw_response_type"] == "object"
    assert isinstance(r["blind"]["orders"][0]["raw_response"], str)


@pytest.mark.asyncio
async def test_blind_compare_valid_disagreement_is_unavailable_not_tie():
    async def always_a(prompt):
        return "A"

    r = await blind_compare(GOOD, BAD, judge_fn=always_a)
    assert r["verdict"] == "unavailable"
    assert r["judge"]["available"] is True
    assert r["blind"]["status"] == "disagreement"
    assert r["blind"]["reason"] == "judge_disagreement"
    assert r["blind"]["order1"] == "multi"
    assert r["blind"]["order2"] == "baseline"
    assert r["blind"]["agreed"] is False


@pytest.mark.asyncio
async def test_blind_compare_swapped_order_normalises_to_same_winner():
    calls = {"n": 0}

    async def swapped(prompt):
        calls["n"] += 1
        return "A" if calls["n"] == 1 else "B"

    r = await blind_compare(GOOD, BAD, judge_fn=swapped)
    assert r["verdict"] == "multi_better"
    assert r["blind"]["order1"] == r["blind"]["order2"] == "multi"
    assert r["blind"]["agreed"] is True
    assert r["blind"]["orders"][0]["prompt_hash"] != r["blind"]["orders"][1]["prompt_hash"]


@pytest.mark.asyncio
async def test_suite_aggregates_by_model_tier():
    cases = [
        {"key": "c1", "model_tier": "strong", "multi": GOOD, "baseline": BAD,
         "multi_tokens": 900, "baseline_tokens": 500},
        {"key": "c2", "model_tier": "strong", "multi": BAD, "baseline": GOOD,
         "multi_tokens": 800, "baseline_tokens": 500},
        {"key": "c3", "model_tier": "local", "multi": GOOD, "baseline": BAD,
         "multi_tokens": 1200, "baseline_tokens": 500},
    ]
    s = await run_suite(cases)
    assert s["total"] == 3
    strong = s["by_tier"]["strong"]
    assert strong["multi_better"] == 1 and strong["baseline_better"] == 1
    assert strong["win_rate"] == 0.5
    assert strong["token_ratio"] == 1.7          # 1700 / 1000
    assert s["by_tier"]["local"]["win_rate"] == 1.0


@pytest.mark.asyncio
async def test_runner_hashes_are_stable_across_snapshot_key_order():
    base = {
        "key": "c1", "multi": GOOD, "baseline": BAD, "prompt": "同一 prompt",
        "workflow_snapshot": {"nodes": [{"key": "a"}], "version": 1},
        "model_identity": {"id": "m1", "model_id": "model"},
        "multi_tokens": 10, "baseline_tokens": 5,
    }
    reordered = {**base, "workflow_snapshot": {"version": 1, "nodes": [{"key": "a"}]}}
    first = await run_case(base)
    second = await run_case(reordered)
    assert first["hashes"] == second["hashes"]
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert all(len(value) == 64 for value in first["hashes"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("multi_tokens", -1), ("baseline_tokens", -1),
    ("multi_latency_ms", -1), ("baseline_latency_ms", -1),
    ("multi_tokens", 1.5), ("baseline_tokens", True),
])
async def test_runner_rejects_invalid_metrics(field, value):
    with pytest.raises(ValueError, match="nonnegative integer"):
        await run_case({"key": "c", "multi": GOOD, "baseline": BAD, field: value})


@pytest.mark.asyncio
async def test_suite_counts_unavailable_without_key_error():
    async def malformed(prompt):
        return "not a verdict"

    result = await run_suite([
        {"key": "c1", "model_tier": "m", "multi": GOOD, "baseline": BAD},
    ], judge_fn=malformed)
    assert result["by_tier"]["m"]["unavailable"] == 1
    assert result["by_tier"]["m"]["win_rate"] is None


@pytest.mark.asyncio
async def test_eval_api_records_and_summarizes(client, make_user, make_company):
    user = await make_user()
    co = await make_company(user)
    h = user["headers"]

    r = await client.post("/evals/compare", headers=h, json={
        "company_id": co["id"], "suite_key": "m4-smoke", "case_key": "c1",
        "multi_text": GOOD, "baseline_text": BAD, "model_tier": "strong",
        "multi_tokens": 900, "baseline_tokens": 500})
    assert r.status_code == 201
    body = r.json()
    assert body["verdict"] == "multi_better"
    assert body["scores"]["multi"] == 1.0

    await client.post("/evals/compare", headers=h, json={
        "company_id": co["id"], "suite_key": "m4-smoke", "case_key": "c2",
        "multi_text": BAD, "baseline_text": GOOD, "model_tier": "strong",
        "multi_tokens": 100, "baseline_tokens": 500})

    summary = (await client.get(
        f"/evals/summary?company_id={co['id']}&suite_key=m4-smoke", headers=h)).json()
    assert summary["total"] == 2
    assert summary["verified_total"] == 0 and summary["manual_total"] == 2
    strong = summary["by_tier"]["strong"]
    assert strong["win_rate"] is None  # manual rules are not verified blind review
    assert strong["manual_win_rate"] == 0.5
    assert strong["token_ratio"] == 1.0          # (900+100) / 1000

    single = (await client.get(f"/evals/{body['id']}", headers=h)).json()
    assert single["case_key"] == "c1"
    assert single["run_kind"] == "manual"
    assert single["verification"] == "manual_unverified"
    assert single["blind_order"] is None
    assert single["blind_evidence"]["status"] == "rules_only"
