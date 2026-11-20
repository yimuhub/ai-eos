"""评测机制（M4-C）：确定性评分、双向盲评一致性、套件聚合、存证接口。"""
import pytest

from app.eval.judge import blind_compare
from app.eval.rubric import effective_chars, score_text
from app.eval.runner import run_suite

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
    assert r["verdict"] == "tie"
    assert r["blind"]["agreed"] is False


@pytest.mark.asyncio
async def test_blind_compare_consistent_judge_wins():
    calls = {"n": 0}

    async def judge(prompt):
        calls["n"] += 1
        # 第一次 A=多 Agent；第二次 A=基线 → 两题都选更长的 GOOD
        return "A" if GOOD in prompt.split("\n\nB:")[0] else "B"

    r = await blind_compare(GOOD, BAD, judge_fn=judge)
    assert r["verdict"] == "multi_better"
    assert r["blind"]["agreed"] is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_blind_compare_judge_failure_is_tie_not_loss():
    async def boom(prompt):
        raise RuntimeError("model down")

    r = await blind_compare(GOOD, BAD, judge_fn=boom)
    assert r["verdict"] == "tie"
    assert r["judge"]["available"] is False


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
    strong = summary["by_tier"]["strong"]
    assert strong["win_rate"] == 0.5
    assert strong["token_ratio"] == 1.0          # (900+100) / 1000

    single = (await client.get(f"/evals/{body['id']}", headers=h)).json()
    assert single["case_key"] == "c1" and single["blind_order"] == "A_first"
