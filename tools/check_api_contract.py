#!/usr/bin/env python3
"""前后端接口契约校验。

## 为什么需要这个工具

改造前的真实情况：前端有 11 处调用对不上后端（404 / 405），
而因为后端"能启动"、前端"能编译"，没有任何自动化检查会告诉你。

本脚本用 FastAPI 的 OpenAPI schema 作为**后端唯一权威来源**
（不要手工维护路由清单，否则又会和代码脱节），
再扫描前端所有 `api.get/post/put/patch/del(...)` 调用，
以及移动端 `mobile/**` 里 `call(base, token, "/path", {method})` 的
第 3 个实参（字面量），逐个比对"路径模板 + HTTP 方法"。

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
  移动端 `app/server.tsx` 的 2 处例外（探活 `/health`、升级 `/clients/update/check`）
  是"任意用户填的地址 + 固定路径"，走不到 Api 层，属于已知盲区。
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
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
# 移动端把全部后端路径集中在一个客户端文件里（mobile/lib/api.ts 的 call(...)），
# 因此可以同样静态比对——历史上它的 404（把 company_id 当 query 传给
# 只接受路径参数的 /dashboard）只能靠人肉发现。
MOBILE_DIR = REPO_DIR / "mobile"

# 动态动作名候选：末段为变量时，先按候选动作名定位资源族，再用「文件内实际用到的
# 动作字面量」逐个校验（见 _harvest_actions）。这里的候选只用于兜底，
# 拼出的完整路径必须真实存在于后端路由表，命中候选不等于通过。
ACTION_CANDIDATES = [
    "pause", "resume", "cancel", "start", "end", "decide", "approve",
    "reject", "rollback", "install", "uninstall", "deploy", "revoke",
    "convert", "sync", "test", "run", "enable", "disable",
    "retry", "takeover",
]

_CALL_RE = re.compile(r'api\.(get|post|put|patch|del)\s*\(\s*[`"\']([^`"\']+)')
_METHOD_MAP = {"del": "DELETE"}

# 模板串里的变量段：`${ep}` / `${id}` / `${detail!.id}`
_VAR_SEG_RE = re.compile(r'\$\{[^}]*\}')
# 函数定义行（用于定位动态调用所在的函数）
_FN_DEF_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z0-9_]+)\s*\('
)

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


_MOBILE_CALL_RE = re.compile(r"\bcall\(")
_MOBILE_METHOD_RE = re.compile(r'method:\s*"([A-Za-z]+)"')
_MOBILE_LITERAL_RE = re.compile(r"""^\s*[`"']([^`"']*)[`"']\s*$""")


def _call_args(text: str, open_at: int) -> list[str]:
    """取 `(` 处那次调用的顶层实参（引号原样保留，用于区分字面量与变量）。"""
    args: list[str] = []
    buf: list[str] = []
    depth = 1
    quote = ""
    i = open_at + 1
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote in "\"'" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(buf))
                return args
            buf.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    return args


def mobile_calls() -> list[tuple[str, int, str, str]]:
    """移动端 lib/api.ts 里的 `call(base, token, "/path", {method})` 清单。

    第 3 个实参不是字符串字面量的（`call(base, token, path)` 这种透传）跳过——
    它的路径由调用点决定，静态看不到。
    """
    out: list[tuple[str, int, str, str]] = []
    for f in sorted(list(MOBILE_DIR.rglob("*.ts")) + list(MOBILE_DIR.rglob("*.tsx"))):
        s = str(f)
        if any(x in s for x in ("node_modules", "/android/", "/dist/", "/build/")):
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        for m in _MOBILE_CALL_RE.finditer(src):
            args = _call_args(src, m.end() - 1)
            if len(args) < 3:
                continue
            literal = _MOBILE_LITERAL_RE.match(args[2])
            if not literal:
                continue
            method = "GET"
            mm = _MOBILE_METHOD_RE.search(args[3] if len(args) > 3 else "")
            if mm:
                method = mm.group(1).upper()
            out.append((str(f.relative_to(REPO_DIR)),
                        src[: m.start()].count("\n") + 1,
                        method, literal.group(1)))
    return out


def _read_web(rel_file: str) -> str:
    """按相对路径读回前端源文件（供动态动作字面量还原用）。"""
    for base in (WEB_DIR, REPO_DIR):
        p = base / rel_file
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""


def _ends_with_var(raw: str) -> bool:
    """模板末段是否为变量（`/tasks/${id}/${ep}` → True；`/tasks/${id}/takeover` → False）。"""
    return raw.rstrip().endswith("}")


def _last_var(raw: str):
    segs = _VAR_SEG_RE.findall(raw)
    if not segs:
        return None
    m = re.match(r'[A-Za-z_$][A-Za-z0-9_$]*', segs[-1][2:-1])
    return m.group(0) if m else None


def _resolve_action(base: str, action: str, method: str, routes) -> bool:
    """拼出的完整路径（含正确的斜杠形态）是否真实存在于后端路由表。"""
    for cand in (base + action, base.rstrip("/") + "/" + action):
        if cand in routes and method in routes[cand]:
            return True
    return False


def _enclosing_fn(lines, lineno0: int):
    """从调用行向上找最近的 function 定义 → (起始行, 函数名, 签名行)。"""
    for i in range(lineno0, -1, -1):
        m = _FN_DEF_RE.match(lines[i])
        if m:
            return i, m.group("name"), lines[i]
    return None, None, None


def _harvest_actions(rel_file: str, lineno: int, raw: str) -> list[str]:
    """尽量静态还原末段变量的取值集合：本地 map 值 → 形参联合类型 → 调用点实参。"""
    var = _last_var(raw)
    if not var:
        return []
    text = _read_web(rel_file)
    lines = text.splitlines()
    start, fname, sig = _enclosing_fn(lines, lineno - 1)
    if start is None:
        return []
    chunk = "\n".join(lines[start: start + 90])

    tokens: list[str] = []
    # 1) 变量是本地 map：`const ep = { pause: "pause", ... }[op]` → 取冒号后的值
    m = re.search(re.escape(var) + r"\s*=\s*\{([^}]*)\}", chunk)
    if m:
        tokens = re.findall(r':\s*"([A-Za-z][A-Za-z0-9_-]*)"', m.group(1))
    # 2) 变量是形参且带字面量联合类型：`action: "a" | "b"` → 取签名里的引号串
    if not tokens and var and re.search(r'[(,]\s*' + re.escape(var) + r'\s*:', sig or ""):
        tokens = re.findall(r'"([A-Za-z][A-Za-z0-9_-]*)"', sig)
    # 3) 都不像 → 从调用点还原：`act("/start")` / `control("retry")`
    if not tokens and fname:
        tokens = re.findall(
            r'\b' + re.escape(fname) + r'\(\s*[`"\']/?([A-Za-z][A-Za-z0-9_-]*)', text)

    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def check() -> tuple[list[tuple], list[tuple], int, int]:
    """返回 (确凿的不匹配, 已校验/需人工确认的动态调用, 前端调用数, 移动端调用数)。"""
    routes = backend_routes()
    broken: list[tuple] = []
    dynamic: list[tuple] = []
    web_calls = frontend_calls()
    mob_calls = mobile_calls()

    for file, line, method, raw in web_calls + mob_calls:
        # 路径模板被截断 = 静态扫描看不懂（例如模板串里嵌了三元表达式，
        # 其中的引号会提前终止正则捕获）。这属于**校验盲区**，
        # 必须显式报错而不是当成"没发现问题"放过去。
        if raw.count("${") > raw.count("}"):
            broken.append((file, line, method, raw + "  ← 模板无法静态解析", []))
            continue

        norm = _normalize(raw)
        if norm in routes and method in routes[norm]:
            continue

        # 末段是变量、且整条不是后端已有路由 → 动态动作名调用。
        if "{}" in norm and _ends_with_var(raw):
            base = norm[: norm.rfind("{}")]
            actions = _harvest_actions(file, line, raw)
            if actions:
                missing = [a for a in actions
                           if not _resolve_action(base, a, method, routes)]
                if missing:
                    broken.append((file, line, method,
                                   raw + "  → 动作未命中后端路由："
                                   + "/".join(missing), []))
                    continue
                dynamic.append((file, line, method,
                                raw + "  → 已逐个校验：" + "/".join(actions)))
                continue
            # 拿不到具体动作字面量（变量来源无法静态判定）→ 沿用候选族兜底，
            # 但必须标注「需人工确认」，不再当成"任一候选命中即通过"。
            if any(_resolve_action(base, a, method, routes) for a in ACTION_CANDIDATES):
                dynamic.append((file, line, method,
                                raw + "  ← 动作变量无法静态判定，需人工确认"))
                continue

        broken.append((file, line, method, raw, sorted(routes.get(norm, []))))
    return broken, dynamic, len(web_calls), len(mob_calls)


def main() -> int:
    ap = argparse.ArgumentParser(description="前后端接口契约校验")
    ap.add_argument("--strict", action="store_true",
                    help="存在确凿不匹配时以退出码 1 结束（CI 门禁用）")
    ap.add_argument("--show-dynamic", action="store_true",
                    help="同时列出通过宽松匹配的动态调用（供人工复核）")
    args = ap.parse_args()

    broken, dynamic, n_web, n_mobile = check()

    if dynamic and args.show_dynamic:
        print(f"\n【需人工确认】动态拼接调用 {len(dynamic)} 处：")
        for file, line, method, raw in dynamic:
            print(f"  {file}:{line}  {method} {raw}")

    if not broken:
        print(f"✅ 接口契约校验通过：网页端 {n_web} 处 api.* 调用 + "
              f"移动端 {n_mobile} 处 call() 调用都能在后端找到对应路由。")
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
