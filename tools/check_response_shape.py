#!/usr/bin/env python3
"""响应**形状**契约：前端泛型声明的顶层形状 vs 后端 OpenAPI 的实际顶层形状。

## 为什么 check_api_contract 之外还要这一个

`check_api_contract.py` 管"路径与方法对不对"（404 / 405）。本工具管另一类同样
不会报错的错配：**路径对了、状态码 200、但取回来的东西不是那个形状**。

这次改造实测抓到两例，都是治理中心页：`/governance/marketplace/packages` 与
`/governance/templates` 都返回数组，前端按 `{installations}` / `{templates}` 对象读，
于是 `r.installations || []` 永远是空数组——面板上写着"暂无安装记录"，
接口却一直在返回数据。TypeScript 也拦不住，因为泛型是人手工写的断言，
`api.get<{templates?: Template[]}>("/governance/templates")` 里的类型从来没有和后端
对过账。

## 判定口径

- 后端形状从 `app.openapi()` 现场解析（不维护第二份清单），沿 `$ref` /
  `allOf` / `anyOf` 展开到顶层；`204`、非 JSON、自由字典算"未知"，跳过不判。
- 前端形状从 `api.get<T>(...)` 的 `T` 判定：`X[]` / `Array<X>` 是数组，
  `{...}` / `Record<...>` 是对象，具名类型回到源码里的 `type` / `interface` 定义再判
  （判不出就算"未知"，不猜）。
- 只有"两边都判得出来且不一致"才算违规。本工具刻意不做深层字段比对——
  那一层交给响应模型（`response_model`）与棘轮测试。

## 用法

    cd apps/api
    .venv/bin/python ../../tools/check_response_shape.py            # 报告
    .venv/bin/python ../../tools/check_response_shape.py --strict   # 门禁
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"

_METHOD_MAP = {"get": "GET", "post": "POST", "put": "PUT",
               "patch": "PATCH", "del": "DELETE"}

# api.get<...>(   —— 泛型用角度括号配平手工取出，正则扛不住嵌套泛型
_CALL_WITH_GENERIC = re.compile(r'\bapi\.(get|post|put|patch|del)\s*<')
_QUOTE = "\"'`"


def _web_sources() -> list[pathlib.Path]:
    out = []
    for f in list(WEB_DIR.rglob("*.tsx")) + list(WEB_DIR.rglob("*.ts")):
        s = str(f)
        if "node_modules" in s or "/.next/" in s:
            continue
        out.append(f)
    return out


def _match_angle(text: str, open_at: int) -> int:
    """从 `<` 处配平角度括号，返回闭合 `>` 的下标；配不平返回 -1。"""
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _normalize(path: str) -> str:
    p = path.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# 前端泛型 → 形状
# ---------------------------------------------------------------------------

_TYPE_ALIAS = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:interface|type)\s+"
                         r"([A-Za-z0-9_]+)[^{}=]*(?:=\s*)?", re.M)


def _type_index() -> dict[str, str]:
    """具名类型 → 定义右值（`type Case = {...}` / `interface Foo { ... }`）。"""
    index: dict[str, str] = {}
    for f in _web_sources():
        src = f.read_text(encoding="utf-8", errors="replace")
        for m in _TYPE_ALIAS.finditer(src):
            name = m.group(1)
            rest = src[m.end(): m.end() + 600]
            if name not in index:
                index[name] = rest
    return index


def _shape_of(generic: str, index: dict[str, str], depth: int = 0) -> str:
    """返回 "array" / "object" / "unknown"。"""
    g = " ".join(generic.split())
    if not g or depth > 4:
        return "unknown"
    if g.startswith("Promise<"):
        inner = _between(g, len("Promise"))
        return _shape_of(inner, index, depth + 1) if inner else "unknown"
    if g.startswith("("):          # 函数类型（回调返回值之类）
        return "unknown"
    if g in ("any", "unknown", "never", "null", "void", "string", "number", "boolean"):
        return "unknown"
    # 判定顺序很要紧：`{ id: string }[]` 既是 `{` 开头又是 `[]` 结尾，
    # 必须先认数组后缀，否则对象字面量数组会被判成对象——而"把数组当对象读"
    # 恰恰是本工具要抓的错配，判反了就会满屏误报。
    if "|" in g:
        shapes = {_shape_of(p, index, depth + 1) for p in _split_union(g)}
        shapes.discard("unknown")
        shapes.discard("null")
        return shapes.pop() if len(shapes) == 1 else "unknown"
    if g.startswith("readonly "):
        return _shape_of(g[len("readonly "):], index, depth + 1)
    if g.startswith("Array<") or g.startswith("ReadonlyArray<"):
        return "array"
    if g.endswith("[]"):
        return "array"
    if g.startswith("Record<") or g.startswith("{"):
        return "object"
    # 具名类型：回源码定义再判一次
    m = re.match(r"^([A-Za-z0-9_]+)(<.*>)?$", g)
    if m and m.group(1) in index:
        return _shape_of(index[m.group(1)], index, depth + 1)
    return "unknown"


def _between(text: str, at: int) -> str:
    """取 `Name<...>` 里尖括号中的内容。"""
    lt = text.find("<", at)
    if lt < 0:
        return ""
    end = _match_angle(text, lt)
    return text[lt + 1: end] if end > 0 else ""


def _split_union(g: str) -> list[str]:
    """按顶层 `|` 切分（忽略尖括号与花括号内部的）。"""
    parts, buf, angle, brace = [], [], 0, 0
    for ch in g:
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        if ch == "|" and angle == 0 and brace == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def frontend_generics() -> list[tuple[str, int, str, str, str]]:
    """[(文件, 行号, 方法, 归一化路径, 泛型形状)]，只含"泛型与路径都解析得出来"的调用。"""
    index = _type_index()
    out: list[tuple[str, int, str, str, str]] = []
    for f in _web_sources():
        src = f.read_text(encoding="utf-8", errors="replace")
        for m in _CALL_WITH_GENERIC.finditer(src):
            close = _match_angle(src, m.end() - 1)
            if close < 0:
                continue
            generic = src[m.end(): close]
            after = src[close + 1:].lstrip()
            if not after.startswith("("):
                continue
            qpos = next((i for i, ch in enumerate(after) if ch in _QUOTE), None)
            if qpos is None:
                continue
            q = after[qpos]
            end = after.find(q, qpos + 1)
            if end < 0:
                continue
            raw = after[qpos + 1: end]
            if raw.count("${") > raw.count("}"):   # 模板解析不了，跳过（另一个工具管）
                continue
            shape = _shape_of(generic, index)
            if shape == "unknown":
                continue
            out.append((str(f.relative_to(WEB_DIR)),
                        src[: m.start()].count("\n") + 1,
                        _METHOD_MAP[m.group(1)], _normalize(raw), shape))
    return out


# ---------------------------------------------------------------------------
# 后端 OpenAPI → 形状
# ---------------------------------------------------------------------------

def _resolve(schema: dict, components: dict, depth: int = 0) -> dict:
    if depth > 6 or not isinstance(schema, dict):
        return schema or {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(components.get(name, {}), components, depth + 1)
    return schema


def _top_shape(schema: dict, components: dict, depth: int = 0) -> str:
    schema = _resolve(schema, components, depth)
    if not isinstance(schema, dict) or not schema:
        return "unknown"
    if depth > 6:
        return "unknown"
    for key in ("oneOf", "anyOf", "allOf"):
        if key in schema:
            shapes = {_top_shape(s, components, depth + 1) for s in schema[key]}
            shapes.discard("unknown")
            return shapes.pop() if len(shapes) == 1 else "unknown"
    t = schema.get("type")
    if t == "object" or ("properties" in schema) or ("additionalProperties" in schema):
        return "object"
    if t == "array":
        return "array"
    return "unknown"


def backend_shapes() -> dict[tuple[str, str], str]:
    """[(归一化路径, 方法)] → 顶层形状。"""
    sys.path.insert(0, str(API_DIR))
    import app.main as main  # noqa: PLC0415

    spec = main.app.openapi()
    components = spec.get("components", {}).get("schemas", {})
    out: dict[tuple[str, str], str] = {}
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            resp = None
            for code in ("200", "201", "202"):
                if code in (op.get("responses") or {}):
                    resp = op["responses"][code]
                    break
            if resp is None:
                continue        # 204 / 无 JSON 正文：本来就没有形状可比
            content = (resp.get("content") or {}).get("application/json")
            if not content:
                continue
            out[(_normalize(path), method.upper())] = _top_shape(
                content.get("schema", {}), components)
    return out


# ---------------------------------------------------------------------------

def check() -> list[tuple[str, int, str, str, str, str]]:
    """返回确凿的形状错配 [(文件, 行号, 方法, 路径, 前端形状, 后端形状)]。"""
    routes = backend_shapes()
    bad = []
    for file, line, method, path, shape in frontend_generics():
        actual = routes.get((path, method))
        if actual in (None, "unknown") or actual == shape:
            continue
        bad.append((file, line, method, path, shape, actual))
    return bad


_LABEL = {"array": "数组", "object": "对象"}


def main() -> int:
    ap = argparse.ArgumentParser(description="前后端响应形状校验")
    ap.add_argument("--strict", action="store_true", help="有错配则退出码 1（门禁用）")
    args = ap.parse_args()

    bad = check()
    probed = len(frontend_generics())
    if not bad:
        print(f"✅ 响应形状校验通过：{probed} 处带泛型的 api.* 调用，"
              f"顶层形状都能对上后端。")
        return 0

    print(f"\n❌ 发现 {len(bad)} 处响应形状错配"
          f"（接口返回 200、前端按另一种形状读，于是永远取不到值）：\n")
    for file, line, method, path, shape, actual in bad:
        print(f"  {file}:{line}  {method} {path}")
        print(f"       前端按{_LABEL.get(shape, shape)}读，后端返回的是"
              f"{_LABEL.get(actual, actual)}")
    print()
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
