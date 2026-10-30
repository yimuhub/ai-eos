"""免费联网搜索的解析与降级链。

背景：2026-10 线上表现为"网络明明是通的，Agent 却说搜不到天气"。
根因不是没接线，而是搜索后端的选择/解析问题：
* html.duckduckgo.com 用 GET 会被反爬挡成 202 空页（旧实现正是 GET）；
* Brave 连发几次 429、360 变"访问异常"、Bing 对中文查询丢词。

所以这里钉住：DDG HTML POST 的解析正确、202 时能重试/换后端、
命中后写缓存不重复打后端。
"""
from __future__ import annotations

import asyncio

import pytest

from app.skill_system import (WebSearchSkill, _dedupe_results, parse_brave_html,
                              parse_ddg_html)

_DDG_PAGE = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="https://www.weather.com.cn/weather/101010100.shtml">北京天气预报,北京7天天气预报</a>
  </h2>
  <a class="result__snippet" href="https://www.weather.com.cn/weather/101010100.shtml">
    北京天气预报，及时准确发布中央气象台天气信息。</a>
</div>
<div class="result results_links results_links_deep result--ad">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://ads.example.com/weather">广告位</a>
  </h2>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nmc.cn%2Fpublish%2Fforecast%2FABJ%2Fbeijing.html&amp;rut=abc">北京-天气预报</a>
  </h2>
  <a class="result__snippet" href="#">中国气象局发布北京今日天气。</a>
</div>
"""

_BRAVE_PAGE = """
<div class="snippet svelte-x" data-type="web">
  <a href="https://example.com/a" title="示例结果一">
    <div class="generic-snippet svelte-x">摘要一</div>
  </a>
</div>
"""


def test_parse_ddg_html_extracts_real_results_and_skips_ads():
    results = parse_ddg_html(_DDG_PAGE, limit=5)
    assert [r["title"] for r in results] == ["北京天气预报,北京7天天气预报", "北京-天气预报"]
    assert results[1]["href"] == (
        "https://www.nmc.cn/publish/forecast/ABJ/beijing.html")
    assert "中央气象台" in results[0]["body"]


def test_parse_brave_html_still_works():
    results = parse_brave_html(_BRAVE_PAGE)
    assert results == [{"title": "示例结果一", "href": "https://example.com/a",
                        "body": "摘要一"}]


def test_dedupe_ignores_anchor_and_trailing_slash():
    results = _dedupe_results([
        {"title": "a", "href": "https://x.com/p#top", "body": ""},
        {"title": "b", "href": "https://x.com/p/", "body": ""},
        {"title": "", "href": "https://x.com/q", "body": ""},
        {"title": "c", "href": "", "body": ""},
    ], limit=5)
    assert len(results) == 1
    assert results[0]["title"] == "a"


@pytest.fixture(autouse=True)
def _reset_search_state(monkeypatch):
    WebSearchSkill._cache.clear()
    WebSearchSkill._last_request_at = 0.0

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def test_search_retries_on_202_then_uses_fallback(monkeypatch):
    """DDG 反爬 202 时先重试，仍无结果就换 Brave，不能直接判"搜不到"。"""
    calls: list[str] = []

    def fake_fetch(url, *, data=None, timeout=8.0):
        calls.append(url)
        if "duckduckgo" in url:
            return ("<html>anomaly</html>", 202)
        return (_BRAVE_PAGE, 200)

    monkeypatch.setattr("app.skill_system._http_fetch", fake_fetch)
    res = (asyncio.run(WebSearchSkill().execute({"query": "北京今天天气"}))
           ["result"])
    assert res["backend"] == "brave"
    assert res["results"][0]["href"] == "https://example.com/a"
    assert sum("duckduckgo" in u for u in calls) == 3


def test_search_caches_successful_query(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url, *, data=None, timeout=8.0):
        calls.append(url)
        return (_DDG_PAGE, 200)

    monkeypatch.setattr("app.skill_system._http_fetch", fake_fetch)
    skill = WebSearchSkill()
    first = (asyncio.run(skill.execute({"query": "北京今天天气"}))["result"])
    second = (asyncio.run(skill.execute({"query": "  北京今天天气 "}))["result"])
    assert first["count"] == 2 and not first.get("cached")
    assert second["cached"] is True
    assert len(calls) == 1


def test_search_without_query_returns_structured_error():
    res = asyncio.run(WebSearchSkill().execute({}))["result"]
    assert res["error"] == "缺少 query"
    assert res["results"] == []
