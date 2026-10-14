#!/usr/bin/env python3
"""前后端接口契约校验。

## 为什么需要这个工具

改造前的真实情况：前端有 11 处调用对不上后端（404 / 405），
而因为后端"能启动"、前端"能编译"，没有任何自动化检查会告诉你。

本脚本用 FastAPI 的 OpenAPI schema 作为**后端唯一权威来源**
（不要手工维护路由清单，否则又会和代码脱节），
再扫描前端所有 `api.get/post/put/patch/del(...)` 调用，
逐个比对"路径模板 + HTTP 方法"。

## 用法

    cd apps/api
    .venv/bin/python ../../tools/check_api_contract.py            # 报告
    .venv/bin/python ../../tools/check_api_contract.py --strict   # 有差异则退出码 1（用于门禁）

## 已知取舍

- 动态拼接的路径（`/tasks/${id}/${action}`）无法静态解析，
  这里对"末段是变量"的调用按候选动作名做宽松匹配，避免大量误报；
  宽松匹配通过的会列入"需人工确认"，不计为失败。
- raw `fetch()` 调用**不在**本工具扫描范围内（它们绕过了统一 API 层）。
  如果要用 raw fetch，请先确认它是否也该走 api 层。
- 模板串里嵌三元表达式（如 `/x/${a ? "b" : "c"}`）会因引号提前终止捕获，
  此类调用会被判为「无法静态解析」并计入失败——请改写成 if/else 两条字面调用。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

API_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps" / "api"
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps" / "web"

# 动态动作名候选：末段为变量时，只要有一个候选命中就视为通过
ACTION_CANDIDATES = [
    "pause", "resume", "cancel", "start", "end", "decide", "approve",
    "reject", "rollback", "install", "uninstall", "deploy", "revoke",
    "convert", "sync", "test", "run", "enable", "disable",
]

_CALL_RE = re.compile(r'api\.(get|post|put|patch|del)\s*\(\s*[`"\']([^`"\']+)')
_METHOD_MAP = {"del": "DELETE"}

# 字符串拼接续段：`api.get("/kb2/agents/" + agentId + "/documents")`
# 注意开头的 [`"'] 用于消费**首个字面量的收尾引号**（正则捕获到的路径
# 停在收尾引号之前），漏掉它就会永远匹配不上。
_CONCAT_TAIL_RE = re.compile(r'[`"\']\s*\+\s*[^"\'`]*?\+\s*[`"\']([^`"\']+)[`"\']')


def backend_routes() -> dict[str, set[str]]:
    """从 FastAPI OpenAPI schema 取后端 (路径模板 → 方法集合)。"""
    sys.path.insert(0, str(API_DIR))
    import app.main as main  # noqa: PLC0415

    schema = main.app.openapi()
    routes: dict[str, set[str]] = {}
    for path, ops in schema.get("paths", {}).items():
        key = _normalize(path)
        routes.setdefault(key, set()).update(
            m.upper() for m in ops
            if m.lower() in ("get", "post", "put", "patch", "delete"))
    return routes


def _normalize(path: str) -> str:
    """把路径里的变量统一成 {}，便于模板比对。"""
    p = path.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]+\}", "{}", p)
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def frontend_calls() -> list[tuple[str, int, str, str]]:
    """扫描前端 api.* 调用 → [(文件, 行号, 方法, 原始路径)]。

    对字符串拼接的路径（`"/kb2/agents/" + id + "/documents"`）会补上尾部字面量，
    还原成 `/kb2/agents/{}/documents` 再参与比对。
    """
    calls: list[tuple[str, int, str, str]] = []
    for f in list(WEB_DIR.rglob("*.tsx")) + list(WEB_DIR.rglob("*.ts")):
        s = str(f)
        if "node_modules" in s or "/.next/" in s:
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        for m in _CALL_RE.finditer(src):
            method = _METHOD_MAP.get(m.group(1), m.group(1).upper())
            path = m.group(2)

            # 续段拼接：紧随其后若形如 `+ xxx + "/tail"`，拼接成完整模板
            tail = _CONCAT_TAIL_RE.match(src, m.end())
            if tail:
                path = path + "{}" + tail.group(1)

            calls.append((str(f.relative_to(WEB_DIR)),
                          src[: m.start()].count("\n") + 1,
                          method, path))
    return calls


def check() -> tuple[list[tuple], list[tuple]]:
    """返回 (确凿的不匹配, 需人工确认的动态调用)。"""
    routes = backend_routes()
    broken: list[tuple] = []
    dynamic: list[tuple] = []

    for file, line, method, raw in frontend_calls():
        # 路径模板被截断 = 静态扫描看不懂（例如模板串里嵌了三元表达式，
        # 其中的引号会提前终止正则捕获）。这属于**校验盲区**，
        # 必须显式报错而不是当成"没发现问题"放过去。
        if raw.count("${") > raw.count("}"):
            broken.append((file, line, method, raw + "  ← 模板无法静态解析", []))
            continue

        norm = _normalize(raw)
        if norm in routes and method in routes[norm]:
            continue

        # 路径中存在变量段：末段可能是拼接出来的动作名，做宽松匹配。
        # 例如 `/meetings/{}{}`（实为 `${id}` + `${path}`）还原为 `/meetings/` + 动作名。
        if "{}" in norm:
            base = norm[: norm.rfind("{}")]
            cands = [f"{base}{a}" for a in ACTION_CANDIDATES]
            # 变量紧跟在斜杠后时，base 可能已含斜杠，两种都试
            cands += [f"{base.rstrip('/')}/{a}" for a in ACTION_CANDIDATES]
            if any(c in routes and method in routes[c] for c in cands):
                dynamic.append((file, line, method, raw))
                continue

        broken.append((file, line, method, raw, sorted(routes.get(norm, []))))
    return broken, dynamic


def main() -> int:
    ap = argparse.ArgumentParser(description="前后端接口契约校验")
    ap.add_argument("--strict", action="store_true",
                    help="存在确凿不匹配时以退出码 1 结束（CI 门禁用）")
    ap.add_argument("--show-dynamic", action="store_true",
                    help="同时列出通过宽松匹配的动态调用（供人工复核）")
    args = ap.parse_args()

    broken, dynamic = check()

    if dynamic and args.show_dynamic:
        print(f"\n【需人工确认】动态拼接调用 {len(dynamic)} 处：")
        for file, line, method, raw in dynamic:
            print(f"  {file}:{line}  {method} {raw}")

    if not broken:
        print("✅ 接口契约校验通过：前端所有 api.* 调用都能在后端找到对应路由。")
        return 0

    print(f"\n❌ 发现 {len(broken)} 处接口契约不匹配：\n")
    for file, line, method, raw, methods in broken:
        if methods:
            print(f"  [405 方法不匹配] {file}:{line}")
            print(f"       前端：{method} {raw}")
            print(f"       后端仅有：{methods}")
        else:
            print(f"  [404 路径不存在] {file}:{line}")
            print(f"       前端：{method} {raw}")
    print()
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
