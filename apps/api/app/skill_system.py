"""Skill plugin system — Skills are plugins, never hardcoded into Runtime."""
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.logging_config import get_logger

logger = get_logger(__name__)


class BaseSkill(ABC):
    """Skill Plugin interface."""

    id: str = ""
    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    input_schema: dict = {}
    output_schema: dict = {}
    permissions: list[str] = []

    @abstractmethod
    async def execute(self, input_data: dict) -> dict: ...


class TextProcessingSkill(BaseSkill):
    id = "text_processing"
    name = "Text Processing"
    description = "Summarize / clean / transform text."

    async def execute(self, input_data: dict) -> dict:
        text = input_data.get("text", "")
        op = input_data.get("operation", "word_count")
        if op == "word_count":
            return {"result": {"characters": len(text), "words": len(text.split())}}
        if op == "extract_urls":
            urls = re.findall(r"https?://[^\s\)\>]+", text)
            return {"result": {"urls": urls}}
        return {"result": {"text": text.strip()}}


class FileReaderSkill(BaseSkill):
    id = "file_reader"
    name = "File Reader"
    description = "Read a text/markdown/json file from the workspace storage."
    # 说明：本技能只能读取 storage 沙箱内的文件。
    # 路径由 LLM 生成，因此必须视为不可信输入——详见 app/core/sandbox.py。

    # 单文件读取上限，避免把巨型文件塞进上下文
    MAX_BYTES = 50_000
    ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml",
                        ".log", ".html", ".xml", ".ini", ".conf", ".rst"}

    async def execute(self, input_data: dict) -> dict:
        from app.core.sandbox import SandboxViolation, resolve_safe_path

        raw_path = (input_data or {}).get("path")
        if not raw_path:
            # 改造前这里是 input_data["path"] → KeyError → 500。
            # 参数缺失是可预期的调用错误，应返回结构化错误而非崩溃。
            return {"result": {"ok": False, "error": "缺少参数 path"}}

        try:
            path = resolve_safe_path(str(raw_path), must_exist=True)
        except SandboxViolation as exc:
            return {"result": {"ok": False,
                               "error": f"拒绝访问：{exc}",
                               "path": str(raw_path)}}

        # 扩展名校验：明确区分"文件类型不支持"与"权限不足"
        if path.suffix.lower() not in self.ALLOWED_SUFFIXES:
            return {"result": {"ok": False,
                               "error": f"不支持的文件类型 {path.suffix}",
                               "allowed": sorted(self.ALLOWED_SUFFIXES)}}

        try:
            # 先按上限截断读取，避免把超大文件整个读进内存
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(self.MAX_BYTES + 1)
        except OSError as exc:
            return {"result": {"ok": False, "error": f"读取失败：{exc}"}}

        truncated = len(content) > self.MAX_BYTES
        return {"result": {"ok": True, "path": str(path),
                           "content": content[:self.MAX_BYTES],
                           "truncated": truncated,
                           "size": path.stat().st_size}}


class WebSearchSkill(BaseSkill):
    """真实 Web 搜索：Bing 中国（国内可达），无需 API key。"""

    id = "web_search"
    name = "Web Search"
    description = "Search the web to get real-time information."
    permissions = ["network"]

    async def execute(self, input_data: dict) -> dict:
        """Bing中国搜索（国内可达），HTML解析。"""
        import asyncio
        import re as _re
        import urllib.request as _uq
        from html import unescape as _unesc

        query = input_data.get("query", "")
        max_results = min(int(input_data.get("max_results", 5)), 10)

        def _fetch():
            url = ("https://cn.bing.com/search?q="
                   + _uq.quote(query) + "&count=" + str(max_results))
            req = _uq.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36"})
            with _uq.urlopen(req, timeout=12) as resp:
                return resp.read().decode("utf-8", errors="replace")

        try:
            html = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=15)
            results = []
            # 解析 b_algo 结果块
            for m in _re.finditer(r'<li class="b_algo".*?</li>', html, _re.S):
                block = m.group()
                tm = _re.search(r'<h2[^>]*>.*?<a[^>]*>(.*?)</a>', block, _re.S)
                hm = _re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"', block, _re.S)
                pm = _re.search(r'<p[^>]*>(.*?)</p>', block, _re.S)
                if tm:
                    title = _unesc(_re.sub(r"<[^>]+>", "", tm.group(1))).strip()
                    snippet = _unesc(_re.sub(r"<[^>]+>", "", pm.group(1))).strip() if pm else ""
                    results.append({"title": title,
                                    "href": hm.group(1) if hm else "",
                                    "body": snippet[:300]})
                if len(results) >= max_results:
                    break
            if not results:
                # 兜底：直接抓所有 h2 链接
                for m in _re.finditer(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>',
                                      html, _re.S):
                    results.append({
                        "title": _unesc(_re.sub(r"<[^>]+>", "", m.group(2))).strip(),
                        "href": m.group(1), "body": ""})
                    if len(results) >= max_results:
                        break
            if results:
                return {"result": {"query": query, "backend": "bing_cn",
                                   "results": results, "count": len(results)}}
            return {"result": {"query": query, "error": "no results parsed",
                               "results": []}}
        except Exception as e:
            # 兜底再试 ddgs
            try:
                from ddgs import DDGS
                results = DDGS().text(query, max_results=max_results)
                if results:
                    return {"result": {"query": query, "backend": "ddgs",
                                       "results": results, "count": len(results)}}
            except Exception as e2:
                # 两个搜索后端都失败：这是"搜索无结果"与"搜索坏了"的分界，
                # 必须区分开，否则排查时看不到到底是哪个后端挂了
                logger.warning("搜索降级后端 ddgs 亦失败 query=%r: %s（原始错误: %s）",
                               query[:80], e2, e)
            return {"result": {"query": query, "error": str(e), "results": []}}


# ---- Skill registry: future skills can be installed from plugins/skills/ directory ----
BUILTIN_SKILLS: list[type[BaseSkill]] = [TextProcessingSkill, FileReaderSkill, WebSearchSkill]

_registry: dict[str, type[BaseSkill]] = {s.id: s for s in BUILTIN_SKILLS}


def register_skill(cls: type[BaseSkill]):
    _registry[cls.id] = cls


def get_skill_class(skill_slug: str) -> type[BaseSkill] | None:
    return _registry.get(skill_slug)


async def execute_skill(skill_slug: str, input_data: dict) -> dict[str, Any]:
    # 自定义/导入技能：按 input_schema.instruction 方法论执行（LLM 按步骤指导）
    if skill_slug.startswith(("custom_", "imp_")):
        return {"result": {"skill": skill_slug,
                           "mode": "methodology",
                           "instruction": (input_data.get("instruction") or "")}}
    cls = _registry.get(skill_slug)
    if not cls:
        raise ValueError(f"Unknown skill: {skill_slug}")
    return await cls().execute(input_data)
