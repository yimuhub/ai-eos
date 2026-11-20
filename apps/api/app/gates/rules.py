"""机械断言：不调模型、不联网、可单测的纯函数。

设计原则（与对标项目明确区分）：

1. **防注水优先**。它的 `min_bytes` 只看字节数，模型写 3000 字废话就能过。
   这里 `min_chars` 统计的是"CJK 字符 + 英文词"的有效量，另配
   `max_duplicate_ratio`（重复句占比）与 `no_ai_cliche`（套话/推脱话术），
   三条一起才挡得住凑字数。
2. **一个规则一个函数**，签名统一：(text, files, ctx) -> Check。
   新增规则只改 `_RULES` 表，不改求值器。
3. **未知规则名直接报错**，不静默跳过。工作流里写错一个字段名，
   在 validate 阶段就该标红，而不是上线后闸门形同虚设。

`files` 的来源有两处，按可信度排序：
* 节点输出里的结构化字段 `output["files"]`（工具/落盘节点会写）；
* 文本里的文件块标记（见 `extract_file_blocks`，给"模型直接写代码"的节点用）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

# 有效字数的口径：CJK 字符按字计，英文/数字按词计。
# 标点、空白、Markdown 控制符不计——这是和"字节数"最关键的区别。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_URL_RE = re.compile(r"https?://[^\s)）\]，。；;\"']+")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|.*$", re.M)

# 默认"推脱话术"：只匹配明确的拒答/免责句式，不做文风判断。
# 用词边界是关键——"作为AI企业"这类正常表述不能被"作为AI"误伤：
# 所以每条都带后接标点/空白的边界，而不是裸子串。
_CLICHE_RES = [
    (re.compile(r"作为(?:一个)?\s*AI[，,。：:！!？?\s]"), "作为AI"),
    (re.compile(r"我(?:无法|不能)(?:提供|访问|直接|给出|满足)"), "我无法…"),
    (re.compile(r"希望(?:以上|这(?:个|些)?)(?:回答|内容)?(?:对)?你有(?:所)?帮助"), "希望以上回答"),
    (re.compile(r"如需(?:进一步|更多)(?:的)?(?:帮助|信息|了解)"), "如需进一步"),
]

# 文件块：三种写法都认，避免不同角色的产出格式打架。
_FILE_MARKERS = (
    re.compile(r"^\s*(?:#{2,4}\s*)?(?:文件|File)[:：]\s*([^\s`|]+\.\w{1,8})\s*$", re.M),
    re.compile(r"^\s*【文件[:：]\s*([^\s】]+\.\w{1,8})\s*】", re.M),
    # ```python src/app.py  —— 围栏语言后面跟一个像文件名的 token
    re.compile(r"^\s*```[^\n`]*?\s([^\s`]+\.\w{1,8})\s*$", re.M),
)


@dataclass
class Check:
    """一条断言的求值结果。字段名即落库字段名。"""

    rule: str
    passed: bool
    expected: Any = None
    actual: Any = None
    blocking: bool = True
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateSpec:
    """解析后的闸门配置。`raw` 保留原样，便于审计。"""

    acceptance: str = ""
    asserts: dict = field(default_factory=dict)
    max_rework: int = 0
    on_fail: str = "fail"          # fail | manual（M1 只实现 fail）
    judge_threshold: float = 0.6
    allow_cliche: bool = False


# ---------------------------------------------------------------------------
# 基础度量
# ---------------------------------------------------------------------------

def effective_chars(text: str) -> int:
    """有效字数：CJK 逐字 + 英文/数字逐词。空白与标点不计。"""
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) + len(_WORD_RE.findall(text))


def duplicate_ratio(text: str, min_unit: int = 10) -> float:
    """重复率：按句切分后，重复句所占比例（0~1）。

    只统计长度 ≥ min_unit 的句子，避免"好的。""收到。"这类短句把比例拉爆。
    句子太少（<4）时返回 0，不做判断。
    """
    if not text:
        return 0.0
    units = [u.strip() for u in re.split(r"[。！？!?\n]+", text)]
    units = [re.sub(r"\s+", "", u) for u in units if len(u.strip()) >= min_unit]
    if len(units) < 4:
        return 0.0
    uniq = len(set(units))
    return round((len(units) - uniq) / len(units), 3)


def extract_file_blocks(text: str) -> list[str]:
    """从产出文本里提取文件块标记（去重、保序）。"""
    found: list[str] = []
    for pattern in _FILE_MARKERS:
        for m in pattern.finditer(text or ""):
            name = m.group(1).strip()
            if name and name not in found:
                found.append(name)
    return found


def citation_stats(text: str) -> tuple[int, int]:
    """(去重链接数, 去重域名数)。域名数用来识别"同一链接刷引用"。"""
    urls = _URL_RE.findall(text or "")
    uniq_urls = {u.rstrip(".,;)") for u in urls}
    domains = set()
    for u in uniq_urls:
        host = urlsplit(u).netloc.lower()
        if host:
            domains.add(host.split(":")[0])
    return len(uniq_urls), len(domains)


def table_data_rows(text: str) -> int:
    """表格数据行数 = 表格行 − 分隔行 − 每张表的表头行。"""
    rows = _TABLE_ROW_RE.findall(text or "")
    seps = [ln for ln in _TABLE_SEP_RE.findall(text or "") if "|" in ln]
    if not rows:
        return 0
    return max(0, len(rows) - len(seps) * 2) if seps else max(0, len(rows) - 1)


def _extract_json_payload(text: str) -> Any:
    """从产出里取 JSON：优先 ```json 围栏，其次整段，最后第一对花括号。"""
    if not text:
        return None
    m = re.search(r"```json\s*(.*?)```", text, re.S)
    candidates = [m.group(1)] if m else []
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        candidates.append(stripped)
    brace = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if brace:
        candidates.append(brace.group(1))
    for c in candidates:
        try:
            return json.loads(c)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# JSON Schema 子集校验（不引第三方依赖）
#
# 只支持闸门真正会用到的关键字：type / required / properties / items /
# minItems / maxItems / enum / minLength / maxLength。复杂 schema
# （oneOf、$ref、patternProperties）不在此列，写了会明确报"不支持"，
# 而不是悄悄放行。
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    "object": dict, "array": list, "string": str, "number": (int, float),
    "integer": int, "boolean": bool, "null": type(None),
}
_SUPPORTED_KEYS = {"type", "required", "properties", "items", "minItems",
                   "maxItems", "enum", "minLength", "maxLength", "description",
                   "title", "default"}


def _validate_subset(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    unknown = set(schema) - _SUPPORTED_KEYS
    if unknown:
        return [f"{path}: 不支持的 schema 关键字 {sorted(unknown)}"]

    expected_type = schema.get("type")
    if expected_type:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        ok = False
        for t in types:
            py = _TYPE_MAP.get(t)
            if py is None:
                return [f"{path}: 未知类型 {t}"]
            if py is bool:
                ok = ok or isinstance(value, bool)
            elif py is int:
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            elif py is (int, float):
                ok = ok or (isinstance(value, (int, float))
                            and not isinstance(value, bool))
            else:
                ok = ok or isinstance(value, py)
        if not ok:
            return [f"{path}: 期望 {types}，实际 {type(value).__name__}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值不在 {schema['enum']} 内")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度 {len(value)} < {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度 {len(value)} > {schema['maxLength']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素 {len(value)} < {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 元素 {len(value)} > {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(_validate_subset(item, schema["items"], f"{path}[{i}]"))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors.extend(_validate_subset(value[key], sub, f"{path}.{key}"))
    return errors


# ---------------------------------------------------------------------------
# 单条规则求值
# ---------------------------------------------------------------------------

def _check_min_chars(text, files, ctx, expected, spec) -> Check:
    actual = effective_chars(text)
    return Check("min_chars", actual >= int(expected), expected, actual,
                 detail="有效字（CJK 逐字 + 英文逐词）")


def _check_max_chars(text, files, ctx, expected, spec) -> Check:
    actual = effective_chars(text)
    return Check("max_chars", actual <= int(expected), expected, actual)


def _check_min_headings(text, files, ctx, expected, spec) -> Check:
    actual = len(_HEADING_RE.findall(text or ""))
    return Check("min_headings", actual >= int(expected), expected, actual)


def _check_contains(text, files, ctx, expected, spec) -> Check:
    missing = [s for s in (expected or []) if s not in (text or "")]
    return Check("contains", not missing, expected, missing,
                 detail=("缺少：" + "、".join(missing)) if missing else "")


def _check_not_contains(text, files, ctx, expected, spec) -> Check:
    hits = [s for s in (expected or []) if s in (text or "")]
    return Check("not_contains", not hits, expected, hits,
                 detail=("命中禁用语：" + "、".join(hits)) if hits else "")


def _check_matches(text, files, ctx, expected, spec) -> Check:
    if not isinstance(expected, dict):
        raise ValueError("matches 必须是 {pattern: 期望次数} 形式")
    failures = []
    actual = {}
    for pattern, want in expected.items():
        try:
            n = len(re.findall(pattern, text or "", re.M))
        except re.error as e:
            raise ValueError(f"matches 正则非法 {pattern!r}: {e}")
        actual[pattern] = n
        if int(want) == 0:
            if n > 0:
                failures.append(f"{pattern} 命中 {n} 次（要求 0）")
        elif n < int(want):
            failures.append(f"{pattern} 命中 {n} 次（要求 ≥{want}）")
    return Check("matches", not failures, expected, actual,
                 detail="；".join(failures))


def _check_duplicate(text, files, ctx, expected, spec) -> Check:
    actual = duplicate_ratio(text)
    return Check("max_duplicate_ratio", actual <= float(expected),
                 expected, actual, detail="重复句占比")


def _check_emits_files(text, files, ctx, expected, spec) -> Check:
    actual = len(files)
    return Check("emits_files", actual == int(expected), expected, actual,
                 detail="文件块数量（少一个即不过）")


def _check_file_size(text, files, ctx, expected, spec, rule="min_file_bytes"):
    if not files:
        return Check(rule, False, expected, 0, detail="没有可度量的文件块")
    sizes = [len(f.get("content", "").encode("utf-8")) if isinstance(f, dict)
             else len(str(f).encode("utf-8")) for f in files]
    actual = min(sizes) if rule == "min_file_bytes" else max(sizes)
    passed = actual >= int(expected) if rule == "min_file_bytes" else actual <= int(expected)
    return Check(rule, passed, expected, actual)


def _check_json_schema(text, files, ctx, expected, spec) -> Check:
    payload = _extract_json_payload(text)
    if payload is None:
        return Check("json_schema", False, "JSON 可解析", "解析失败",
                     detail="产出里没有可解析的 JSON")
    errors = _validate_subset(payload, expected)
    return Check("json_schema", not errors, "符合 schema",
                 errors or "符合", detail="；".join(errors[:5]))


def _check_min_citations(text, files, ctx, expected, spec) -> Check:
    n, _ = citation_stats(text)
    return Check("min_citations", n >= int(expected), expected, n)


def _check_min_domains(text, files, ctx, expected, spec) -> Check:
    _, d = citation_stats(text)
    return Check("min_source_domains", d >= int(expected), expected, d,
                 detail="不同域名数，防同一链接刷引用")


def _check_min_table_rows(text, files, ctx, expected, spec) -> Check:
    n = table_data_rows(text)
    return Check("min_table_rows", n >= int(expected), expected, n)


def _check_no_cliche(text, files, ctx, expected, spec) -> Check:
    hits = [label for pattern, label in _CLICHE_RES if pattern.search(text or "")]
    return Check("no_ai_cliche", not hits, "无推脱/免责话术", hits,
                 detail=("命中：" + "、".join(hits)) if hits else "")


def _check_custom(text, files, ctx, expected, spec):
    from .registry import CUSTOM_RULES
    fn = CUSTOM_RULES.get(str(expected))
    if fn is None:
        raise ValueError(f"未注册的自定义规则 {expected!r}（只允许白名单函数）")
    try:
        result = fn(text, files, ctx)
    except Exception as e:  # 自定义规则自身出错 = 不放行，且要能看到原因
        return Check("custom", False, expected, None, detail=f"执行异常：{e}")
    if isinstance(result, Check):
        return result
    passed = bool(result)
    return Check("custom", passed, expected, result)


_RULES = {
    "min_chars": _check_min_chars,
    "max_chars": _check_max_chars,
    "min_headings": _check_min_headings,
    "contains": _check_contains,
    "not_contains": _check_not_contains,
    "matches": _check_matches,
    "max_duplicate_ratio": _check_duplicate,
    "emits_files": _check_emits_files,
    "min_file_bytes": lambda t, f, c, e, s: _check_file_size(t, f, c, e, s, "min_file_bytes"),
    "max_file_bytes": lambda t, f, c, e, s: _check_file_size(t, f, c, e, s, "max_file_bytes"),
    "json_schema": _check_json_schema,
    "min_citations": _check_min_citations,
    "min_source_domains": _check_min_domains,
    "min_table_rows": _check_min_table_rows,
    "no_ai_cliche": _check_no_cliche,
    "custom": _check_custom,
}

KNOWN_RULES = frozenset(_RULES)


def evaluate(text: str, files: list, spec: GateSpec | dict,
             ctx: dict | None = None) -> list[Check]:
    """跑完全部机械断言。未知规则名抛 ValueError（由调用方给节点报错）。"""
    ctx = ctx or {}
    if isinstance(spec, GateSpec):
        asserts, allow_cliche = spec.asserts, spec.allow_cliche
    else:
        asserts, allow_cliche = dict(spec or {}), False

    # 调用方没给结构化文件时，从文本里的文件块标记兜底提取——
    # 让 `emits_files` 不依赖调用方是否记得传 files（少踩一次坑）。
    if not files:
        files = [{"name": n, "content": ""} for n in extract_file_blocks(text)]

    checks: list[Check] = []
    for rule, expected in asserts.items():
        fn = _RULES.get(rule)
        if fn is None:
            raise ValueError(
                f"未知的断言规则 {rule!r}；可用：{sorted(KNOWN_RULES)}")
        checks.append(fn(text, files, ctx, expected, asserts))

    # 套话检查默认开启（仅当节点没显式关掉）。
    if not allow_cliche and "no_ai_cliche" not in asserts:
        checks.append(_check_no_cliche(text, files, ctx, None, asserts))
    return checks


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking and not c.passed]


def summarize(checks: list[Check]) -> str:
    """把失败项压成一段给返工提示词用的话（必须具体到规则与实际值）。"""
    lines = []
    for c in blocking_failures(checks):
        exp = c.expected if c.expected is not None else "见规则说明"
        act = c.actual if c.actual is not None else "n/a"
        lines.append(f"- {c.rule}：实际 {act} / 要求 {exp}"
                     + (f"（{c.detail}）" if c.detail else ""))
    return "\n".join(lines)
