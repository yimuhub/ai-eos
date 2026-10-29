"""Skill plugin system — Skills are plugins, never hardcoded into Runtime.

插件约定（真动态加载，见底部的 load_plugin_skills）：
`<仓库根>/plugins/skills/<skill_name>/manifest.json`，字段
name / version / description / module；module 是插件目录内的点分模块名
（如 `skill` 或 `pkg.skill`，不允许绝对路径与 `..`）。模块内需定义
BaseSkill 子类（id 建议与 manifest.name 一致）。

安全边界见 load_plugin_skills 上方的注释：插件目录不进 sys.path，
模块按文件路径以唯一模块名加载；**签名/发布者校验尚未实现**。
"""
import hashlib
import importlib.util
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
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


# ---- plugins/skills/ 真动态加载 -----------------------------------------
# 每个子目录的 manifest.json → 加载对应模块 → 注册技能类。
# 单个插件失败只记 warning，不中断其它插件与内置技能。
#
# 【安全限制｜审查 S8】当前**没有**插件签名、发布者校验、SHA-256 固定清单，
# 也没有"安装前用户显式确认"：任何能往 PLUGINS_DIR 写文件的人都能让 API
# 进程执行其代码（插件在进程内运行，等价于任意代码执行）。完整签名体系
# 需要密钥管理与发布链，尚未实现。
#
# 为此在读取侧做了这些收口（只能降低误加载面，不能替代签名）：
#   1. 不再 `sys.path.insert(0, plugin_path)`：插件目录不进模块搜索路径，
#      无法抢先注册同名模块覆盖内置实现；
#   2. 插件目录 resolve() 后必须位于插件根目录内（挡软链越界）；
#   3. manifest 必须有 name/version/module；入口必须是插件目录内的相对
#      模块名，绝对路径、盘符、`/`、`\`、`..` 一律拒绝；
#   4. 按文件路径加载，模块名唯一（aieos_plugin_<slug>_<hash>），且禁止
#      插件用 id 覆盖内置技能。
# 仍待实现：签名校验、安装确认、受限子进程运行。
PLUGINS_DIR = Path(__file__).resolve().parents[3] / "plugins" / "skills"
_MANIFEST_FIELDS = ("name", "version", "module")
_MANIFEST_MAX_BYTES = 64 * 1024
_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_within(root: Path, target: Path) -> bool:
    """target resolve() 后是否仍在 root 内（同时挡住软链越界）。"""
    root_r = root.resolve()
    target_r = target.resolve()
    return target_r == root_r or root_r in target_r.parents


def _plugin_entry(plugin_dir: Path, module: str) -> Path | None:
    """把 manifest.module 解析成插件目录内的入口文件；非法/越界返回 None。

    只接受点分模块名（`skill`、`pkg.skill`，可带 `.py` 后缀）；
    绝对路径、盘符、`/`、`\\` 与任何 `..` 段都会被拒绝。
    """
    if not isinstance(module, str):
        return None
    name = module.strip()
    if name.endswith(".py"):
        name = name[:-3]
    if not name or any(ch in name for ch in ("/", "\\", ":", "\x00")):
        return None
    parts = name.split(".")
    if not parts or any(not _MODULE_PART_RE.match(p) for p in parts):
        return None
    base = plugin_dir.joinpath(*parts)
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file() and _is_within(plugin_dir, cand):
            return cand.resolve()
    return None


def _load_module_from_path(entry: Path, unique_name: str):
    """按文件路径加载模块，模块名由调用方保证唯一（不进 sys.path）。"""
    spec = importlib.util.spec_from_file_location(unique_name, entry)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {entry.name} 构建模块 spec")
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：插件内部的自引用 import / dataclass 装饰器需要它
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(unique_name, None)
        raise
    return module


def _skill_class_from_module(module, manifest_name: str) -> type[BaseSkill] | None:
    """在已导入的插件模块里找出技能类（本模块内定义的 BaseSkill 子类）。"""
    candidates = [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, BaseSkill)
        and obj is not BaseSkill and obj.__module__ == module.__name__
    ]
    if not candidates:
        return None
    for cls in candidates:
        if cls.id == manifest_name:
            return cls
    if len(candidates) == 1:
        return candidates[0]
    logger.warning("技能插件模块 %s 内有多个技能类且 id 均不等于 manifest name=%r，取第一个",
                   module.__name__, manifest_name)
    return candidates[0]


def load_plugin_skills() -> list[str]:
    """扫描 plugins/skills/*/manifest.json 并注册到 _registry。

    返回成功加载的技能 id 列表。目录不存在（纯内置部署）时静默返回 []。
    """
    loaded: list[str] = []
    if not PLUGINS_DIR.is_dir():
        return loaded
    plugin_root = PLUGINS_DIR.resolve()
    if not os.access(plugin_root, os.W_OK):
        # 不可写意味着"用户无法安装/更新插件，但已有插件仍会执行"，
        # 这是运维需要知道的状态，不能静默。
        logger.warning(
            "技能插件根目录 %s 不可写：无法安装/更新插件；"
            "已有插件仍会加载执行（当前无签名校验，见本段注释）", plugin_root)

    for plugin_dir in sorted(plugin_root.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name.startswith(("_", ".")):
            continue
        name_hint = plugin_dir.name
        try:
            real_dir = plugin_dir.resolve()
        except OSError as exc:
            logger.warning("技能插件 %s: 目录解析失败，已跳过：%s", name_hint, exc)
            continue
        if not _is_within(plugin_root, real_dir):
            logger.warning("技能插件 %s: 目录解析后不在插件根目录内，已跳过", name_hint)
            continue
        manifest_path = real_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            if manifest_path.stat().st_size > _MANIFEST_MAX_BYTES:
                logger.warning("技能插件 %s: manifest.json 超过 %d 字节，已跳过",
                               name_hint, _MANIFEST_MAX_BYTES)
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("技能插件 %s: manifest.json 解析失败，已跳过：%s",
                           name_hint, exc)
            continue
        if not isinstance(manifest, dict):
            logger.warning("技能插件 %s: manifest.json 不是对象，已跳过", name_hint)
            continue
        missing = [f for f in _MANIFEST_FIELDS
                   if not isinstance(manifest.get(f), str) or not manifest[f].strip()]
        if missing:
            logger.warning("技能插件 %s: manifest 缺字段或字段非字符串 %s，已跳过",
                           name_hint, missing)
            continue
        entry = _plugin_entry(real_dir, manifest["module"])
        if entry is None:
            logger.warning(
                "技能插件 %s: manifest.module=%r 非法（绝对路径/含 .. 或"
                "指向插件目录外/文件不存在），已跳过", name_hint, manifest["module"])
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", manifest["name"].lower()).strip("_")[:40]
        digest = hashlib.sha256(str(entry).encode("utf-8")).hexdigest()[:12]
        unique_name = f"aieos_plugin_{slug or 'plugin'}_{digest}"
        try:
            module = _load_module_from_path(entry, unique_name)
            cls = _skill_class_from_module(module, manifest["name"])
            if cls is None:
                logger.warning("技能插件 %s: 模块 %s 内未找到 BaseSkill 子类，已跳过",
                               name_hint, manifest["module"])
                continue
            if not cls.id:
                logger.warning("技能插件 %s: 技能类 %s 未设置 id，已跳过",
                               name_hint, cls.__name__)
                continue
            if cls.id in {s.id for s in BUILTIN_SKILLS}:
                logger.warning("技能插件 %s: id=%r 与内置技能冲突，已跳过"
                               "（插件不得覆盖内置实现）", name_hint, cls.id)
                continue
            if cls.id != manifest["name"]:
                logger.warning("技能插件 %s: manifest name=%r 与类 id=%r 不一致，按类 id 注册",
                               name_hint, manifest["name"], cls.id)
            if cls.id in _registry:
                logger.warning("技能插件 %s: id=%r 已存在，将覆盖原注册",
                               name_hint, cls.id)
            register_skill(cls)
            loaded.append(cls.id)
            logger.info("技能插件已加载：%s（%s v%s）",
                        cls.id, name_hint, manifest["version"])
        except Exception as exc:
            logger.warning("技能插件 %s 加载失败，已跳过：%s",
                           name_hint, exc, exc_info=True)
    return loaded


# 启动时加载一次：本模块随 app.api.routes 被 app.main 导入，
# 等价于应用启动期执行；失败不抛异常（load_plugin_skills 内部已消化）。
load_plugin_skills()
