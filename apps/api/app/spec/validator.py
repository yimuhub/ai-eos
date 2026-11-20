"""workflow-spec 校验器：三层检查，全部纯函数（不查库、不调模型）。

1. **结构层**：字段白名单、类型、取值域。写错一个字段名就报错——
   静默忽略字段，等于让作者以为配上了、实际没生效。
2. **语义层**：START/END、非 END 必有出边、可达、无环、变量必须有来源、
   员工必须存在、各节点类型的必填项。
3. **策略层**：节点数上限、并发上限、预算上限。

报错必须能定位：`path` 形如 `nodes[2].task`、`edges[5].to`，前端可直接高亮。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.gates.rules import KNOWN_RULES

from .schema import (EDGE_FIELDS, INPUT_FIELDS, INPUT_TYPES, KEY_RE, MAX_BUDGET_TOKENS,
                     MAX_CONCURRENCY, MAX_NODES, NODE_COMMON_FIELDS, NODE_TYPE_FIELDS,
                     NODE_TYPES, POLICY_FIELDS, REQUIRED_FIELDS, SPEC_VERSION,
                     TOP_FIELDS, VAR_RE)

_KEY_COMPILED = re.compile(KEY_RE)
_VAR_COMPILED = re.compile(VAR_RE)
_INPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# gate 允许的字段与断言规则，和 app/gates/step_gate.py 保持一致
_GATE_FIELDS = {"acceptance", "assert", "asserts", "max_rework", "on_fail",
                "judge_threshold", "allow_cliche", "judge"}
_GATE_ON_FAIL = {"fail", "manual", "observe"}


@dataclass
class Issue:
    code: str          # 机器可读：missing_end / cycle / unknown_var ...
    message: str       # 中文，能直接展示给作者
    path: str = "$"
    level: str = "error"   # error | warning

    @property
    def blocking(self) -> bool:
        return self.level == "error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "path": self.path, "level": self.level}


@dataclass
class SpecContext:
    """校验时用到的"库内事实"。纯函数不给 ctx 就跳过对应检查。"""

    agent_names: dict[str, str] = field(default_factory=dict)  # 归一化姓名 -> id
    agent_ids: set[str] = field(default_factory=set)
    workflow_ids: set[str] = field(default_factory=set)
    workflow_keys: dict[str, str] = field(default_factory=dict)  # spec key -> id


def slugify(name: str) -> str:
    """把工作流名压成合法的 spec key（供导入时没写 key 的情况兜底）。"""
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (ascii_part or "wf")[:48] or "wf"


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _iter_nodes(spec: dict) -> Iterable[tuple[int, dict]]:
    nodes = spec.get("nodes")
    if isinstance(nodes, list):
        for i, n in enumerate(nodes):
            if isinstance(n, dict):
                yield i, n


def _ancestors(keys: list[str], in_edges: dict[str, list[str]]) -> dict[str, set[str]]:
    """每个节点的全部前驱集合（含传递闭包）。图小（≤24 节点），朴素 DFS 足够。"""
    memo: dict[str, set[str]] = {}

    def visit(k: str, stack: frozenset[str]) -> set[str]:
        if k in memo:
            return memo[k]
        if k in stack:          # 环：调用方已单独报环，这里只防递归爆栈
            return set()
        acc: set[str] = set()
        for p in in_edges.get(k, []):
            acc.add(p)
            acc |= visit(p, stack | {k})
        memo[k] = acc
        return acc

    for k in keys:
        visit(k, frozenset())
    return memo


def _check_structure(spec: dict) -> list[Issue]:
    out: list[Issue] = []
    if not isinstance(spec, dict):
        return [Issue("not_object", "spec 必须是对象（YAML 映射或 JSON 对象）", "$")]

    version = str(spec.get("spec") or "")
    if version != SPEC_VERSION:
        out.append(Issue("bad_version",
                         f"spec 版本必须是 {SPEC_VERSION!r}，实际 {version!r}", "$.spec"))

    unknown = set(spec) - TOP_FIELDS
    if unknown:
        out.append(Issue("unknown_field",
                         f"顶层出现未知字段 {sorted(unknown)}；可用 {sorted(TOP_FIELDS)}", "$"))

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        out.append(Issue("missing_name", "name 必填且不能为空", "$.name"))
    elif len(name) > 300:
        out.append(Issue("name_too_long", "name 超过 300 字", "$.name"))

    key = spec.get("key")
    if key is not None:
        if not isinstance(key, str) or not _KEY_COMPILED.match(key):
            out.append(Issue("bad_key",
                             "key 只能用小写字母开头、含 a-z 0-9 _ -，最长 64", "$.key"))

    # ---- inputs ----
    inputs = spec.get("inputs", [])
    if not isinstance(inputs, list):
        out.append(Issue("bad_inputs", "inputs 必须是数组", "$.inputs"))
        inputs = []
    seen_inputs: set[str] = set()
    for i, item in enumerate(inputs):
        path = f"inputs[{i}]"
        if not isinstance(item, dict):
            out.append(Issue("bad_input", "输入项必须是对象", path))
            continue
        unknown_fields = set(item) - INPUT_FIELDS
        if unknown_fields:
            out.append(Issue("unknown_field",
                             f"输入项出现未知字段 {sorted(unknown_fields)}", path))
        iname = item.get("name")
        if not isinstance(iname, str) or not _INPUT_NAME_RE.match(iname):
            out.append(Issue("bad_input_name",
                             "输入名必须是字母/下划线开头的标识符", f"{path}.name"))
        elif iname in seen_inputs:
            out.append(Issue("dup_input", f"输入名重复：{iname}", f"{path}.name"))
        else:
            seen_inputs.add(iname)
        itype = item.get("type", "text")
        if itype not in INPUT_TYPES:
            out.append(Issue("bad_input_type",
                             f"输入类型只能是 {sorted(INPUT_TYPES)}", f"{path}.type"))

    # ---- nodes ----
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        out.append(Issue("missing_nodes", "nodes 必填且不能为空", "$.nodes"))
        nodes = []

    seen_keys: dict[str, int] = {}
    for i, n in _iter_nodes(spec):
        path = f"nodes[{i}]"
        k = n.get("key")
        if not isinstance(k, str) or not k:
            out.append(Issue("bad_node_key", "节点必须有 key", f"{path}.key"))
        elif not _KEY_COMPILED.match(k):
            out.append(Issue("bad_node_key",
                             f"节点 key 不合法：{k}（小写字母开头，含 a-z 0-9 _ -）",
                             f"{path}.key"))
        elif k in seen_keys:
            out.append(Issue("dup_node_key", f"节点 key 重复：{k}", f"{path}.key"))
        else:
            seen_keys[k] = i

        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            out.append(Issue("bad_node_type",
                             f"未知节点类型 {ntype!r}；可用 {list(NODE_TYPES)}",
                             f"{path}.type"))
            continue

        allowed = NODE_COMMON_FIELDS | NODE_TYPE_FIELDS.get(ntype, set())
        unknown_fields = set(n) - allowed
        if unknown_fields:
            out.append(Issue("unknown_field",
                             f"{ntype} 节点不支持字段 {sorted(unknown_fields)}", path))

        nm = n.get("name")
        if nm is not None and (not isinstance(nm, str) or len(nm) > 300):
            out.append(Issue("bad_node_name", "name 必须是不超过 300 字的字符串",
                             f"{path}.name"))
        for axis in ("x", "y"):
            if axis in n and not isinstance(n[axis], (int, float)):
                out.append(Issue("bad_position", f"{axis} 必须是数字", f"{path}.{axis}"))

        # 各类型必填
        for req in REQUIRED_FIELDS.get(ntype, ()):
            if req == "url":
                cfg = n.get("config") or {}
                if not (isinstance(cfg, dict) and str(cfg.get("url") or "").strip()):
                    out.append(Issue("missing_config",
                                     "HTTP 节点必须在 config.url 给出地址", f"{path}.config"))
            elif req == "tool_name":
                cfg = n.get("config") or {}
                if not (isinstance(cfg, dict) and str(cfg.get("tool_name") or "").strip()):
                    out.append(Issue("missing_config",
                                     "TOOL 节点必须在 config.tool_name 给出工具名", f"{path}.config"))
            elif req == "set":
                cfg = n.get("config") or {}
                if not (isinstance(cfg, dict) and isinstance(cfg.get("set"), dict)):
                    out.append(Issue("missing_config",
                                     "TRANSFORM 节点必须在 config.set 给出映射", f"{path}.config"))
            else:
                val = n.get(req)
                if not isinstance(val, str) or not val.strip():
                    out.append(Issue("missing_field", f"{ntype} 节点必须提供 {req}",
                                     f"{path}.{req}"))

        if ntype == "CONDITION":
            kw = n.get("keyword")
            if not isinstance(kw, str) or len(kw) > 100:
                out.append(Issue("bad_keyword", "keyword 必须是不超过 100 字的字符串",
                                 f"{path}.keyword"))
        if ntype == "WAIT":
            sec = n.get("seconds", 5)
            if not isinstance(sec, int) or not (0 <= sec <= 60):
                out.append(Issue("bad_seconds", "seconds 必须是 0~60 的整数",
                                 f"{path}.seconds"))
        if ntype == "SUB_WORKFLOW":
            if not (n.get("workflow_id") or n.get("workflow_key")):
                out.append(Issue("missing_field",
                                 "SUB_WORKFLOW 节点必须给 workflow_id 或 workflow_key", path))
        if ntype == "AGENT":
            if not isinstance(n.get("task"), str) or not n["task"].strip():
                out.append(Issue("missing_field", "AGENT 节点必须提供 task",
                                 f"{path}.task"))
            _check_gate(n.get("gate"), f"{path}.gate", out)

    # ---- edges ----
    edges = spec.get("edges")
    if not isinstance(edges, list):
        out.append(Issue("bad_edges", "edges 必须是数组", "$.edges"))
        edges = []
    seen_edges: set[tuple] = set()
    for i, e in enumerate(edges):
        path = f"edges[{i}]"
        if not isinstance(e, dict):
            out.append(Issue("bad_edge", "连线必须是对象", path))
            continue
        unknown_fields = set(e) - EDGE_FIELDS
        if unknown_fields:
            out.append(Issue("unknown_field",
                             f"连线出现未知字段 {sorted(unknown_fields)}", path))
        f, t = e.get("from"), e.get("to")
        for label, v in (("from", f), ("to", t)):
            if not isinstance(v, str):
                out.append(Issue("bad_edge", f"连线缺少 {label}", f"{path}.{label}"))
            elif v not in seen_keys:
                out.append(Issue("dangling_edge",
                                 f"连线指向不存在的节点：{v}", f"{path}.{label}"))
        if isinstance(f, str) and isinstance(t, str):
            if f == t:
                out.append(Issue("self_loop", f"节点 {f} 连到了自己", path))
            pair = (f, t, e.get("label"))
            if pair in seen_edges:
                out.append(Issue("dup_edge", "重复连线", path, "warning"))
            seen_edges.add(pair)

    policy = spec.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            out.append(Issue("bad_policy", "policy 必须是对象", "$.policy"))
        else:
            unknown = set(policy) - POLICY_FIELDS
            if unknown:
                out.append(Issue("unknown_field",
                                 f"policy 出现未知字段 {sorted(unknown)}", "$.policy"))
    return out


def _check_gate(gate, path: str, out: list[Issue]) -> None:
    if gate is None:
        return
    if not isinstance(gate, dict):
        out.append(Issue("bad_gate", "gate 必须是对象", path))
        return
    unknown = set(gate) - _GATE_FIELDS
    if unknown:
        out.append(Issue("bad_gate", f"gate 出现未知字段 {sorted(unknown)}", path))
    asserts = gate.get("assert") if "assert" in gate else gate.get("asserts")
    if asserts is not None:
        if not isinstance(asserts, dict):
            out.append(Issue("bad_gate", "gate.assert 必须是对象", f"{path}.assert"))
        else:
            bad = set(asserts) - KNOWN_RULES
            if bad:
                out.append(Issue("unknown_assert",
                                 f"未知断言规则 {sorted(bad)}；可用 {sorted(KNOWN_RULES)}",
                                 f"{path}.assert"))
    on_fail = gate.get("on_fail")
    if on_fail is not None and on_fail not in _GATE_ON_FAIL:
        out.append(Issue("bad_gate", f"on_fail 只能是 {sorted(_GATE_ON_FAIL)}",
                         f"{path}.on_fail"))
    mr = gate.get("max_rework")
    if mr is not None and (not isinstance(mr, int) or not (0 <= mr <= 1)):
        out.append(Issue("bad_gate", "max_rework 只能是 0 或 1", f"{path}.max_rework"))
    th = gate.get("judge_threshold")
    if th is not None and not isinstance(th, (int, float)):
        out.append(Issue("bad_gate", "judge_threshold 必须是 0~1 的数字",
                         f"{path}.judge_threshold"))


def _check_semantics(spec: dict, ctx: SpecContext | None) -> list[Issue]:
    out: list[Issue] = []
    nodes = [n for _, n in _iter_nodes(spec)]
    by_key = {n["key"]: n for n in nodes if isinstance(n.get("key"), str)}
    edges = [e for e in (spec.get("edges") or []) if isinstance(e, dict)]

    starts = [k for k, n in by_key.items() if n.get("type") == "START"]
    ends = [k for k, n in by_key.items() if n.get("type") == "END"]
    if len(starts) != 1:
        out.append(Issue("bad_start", f"必须有且只有一个 START 节点，当前 {len(starts)} 个",
                         "$.nodes"))
    if not ends:
        out.append(Issue("missing_end", "必须至少有一个 END 节点", "$.nodes"))

    out_edges: dict[str, list[dict]] = {}
    in_edges: dict[str, list[str]] = {}
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if isinstance(f, str) and isinstance(t, str) and f in by_key and t in by_key:
            out_edges.setdefault(f, []).append(e)
            in_edges.setdefault(t, []).append(f)

    for k, n in by_key.items():
        if n.get("type") != "END" and not out_edges.get(k):
            out.append(Issue("dead_end",
                             f"节点 {k} 没有连到下一步（每个非 END 节点都要有出边）",
                             f"nodes[{_index_of(nodes, k)}]"))

    # 可达性：START 出发
    reachable: set[str] = set()
    if len(starts) == 1:
        stack = [starts[0]]
        while stack:
            k = stack.pop()
            if k in reachable:
                continue
            reachable.add(k)
            stack.extend(e["to"] for e in out_edges.get(k, [])
                         if isinstance(e.get("to"), str))
        for k, n in by_key.items():
            if k not in reachable and n.get("type") != "START":
                out.append(Issue("unreachable",
                                 f"节点 {k} 从 START 不可达（不会被执行）",
                                 f"nodes[{_index_of(nodes, k)}]", "warning"))

    # 环检测（Kahn）：工作流必须是有向无环图
    indeg = {k: len(set(in_edges.get(k, []))) for k in by_key}
    queue = [k for k, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        k = queue.pop()
        visited += 1
        for e in out_edges.get(k, []):
            t = e.get("to")
            if isinstance(t, str) and t in indeg:
                indeg[t] -= 1
                if indeg[t] == 0:
                    queue.append(t)
    if visited != len(by_key) and by_key:
        stuck = sorted(k for k, d in indeg.items() if d > 0)
        out.append(Issue("cycle",
                         f"工作流存在环（{', '.join(stuck[:6])}），执行器无法推进；"
                         "需要迭代请用显式回边 + 条件节点，或拆分工作流", "$.edges"))

    # 变量来源
    anc = _ancestors(list(by_key.keys()), in_edges)
    inputs = {str(i.get("name")) for i in (spec.get("inputs") or [])
              if isinstance(i, dict)}
    aliases = {str(n["output"]): k for k, n in by_key.items() if n.get("output")}
    for i, n in _iter_nodes(spec):
        text = n.get("task")
        if not isinstance(text, str):
            continue
        node_key = n.get("key")
        for ref in _VAR_COMPILED.findall(text):
            root = ref.split(".")[0]
            if root == "input":
                if len(ref.split(".")) < 2 or ref.split(".")[1] not in inputs:
                    out.append(Issue("unknown_var",
                                     f"引用了未定义的输入：{{{{{ref}}}}}", f"nodes[{i}].task"))
                continue
            if root in inputs:
                continue
            if root in aliases:
                producer = aliases[root]
            elif root in by_key:
                producer = root
            else:
                out.append(Issue("unknown_var",
                                 f"变量 {{{{{root}}}}} 既不是输入、也不是任何节点的 key/别名",
                                 f"nodes[{i}].task"))
                continue
            if producer not in anc.get(node_key, set()):
                out.append(Issue("var_not_upstream",
                                 f"变量 {{{{{root}}}}} 的来源节点 {producer} 不是 {node_key} 的上游",
                                 f"nodes[{i}].task"))

    # 条件节点必须给 yes/no 标签，否则两个分支都会走
    for k, n in by_key.items():
        if n.get("type") != "CONDITION":
            continue
        labels = {(e.get("label") or "").lower() for e in out_edges.get(k, [])}
        if not ({"yes", "no"} & labels):
            out.append(Issue("condition_without_label",
                             f"条件节点 {k} 的出边没有 yes/no 标签，执行时两个分支都会走",
                             f"nodes[{_index_of(nodes, k)}]", "warning"))

    # 员工与子工作流
    if ctx is not None:
        for i, n in _iter_nodes(spec):
            if n.get("type") != "AGENT":
                continue
            aid = n.get("agent_id")
            aname = n.get("agent")
            if isinstance(aid, str) and aid:
                if ctx.agent_ids and aid not in ctx.agent_ids:
                    out.append(Issue("agent_not_found",
                                     f"agent_id {aid} 不属于本公司", f"nodes[{i}].agent_id"))
            elif isinstance(aname, str) and aname:
                if ctx.agent_names and _norm_name(aname) not in ctx.agent_names:
                    out.append(Issue("agent_not_found",
                                     f"本公司没有叫「{aname}」的员工", f"nodes[{i}].agent"))
            else:
                out.append(Issue("missing_field", "AGENT 节点必须指定执行员工",
                                 f"nodes[{i}].agent"))
        for i, n in _iter_nodes(spec):
            if n.get("type") != "SUB_WORKFLOW":
                continue
            wid = n.get("workflow_id")
            if isinstance(wid, str) and wid and ctx.workflow_ids and wid not in ctx.workflow_ids:
                out.append(Issue("subflow_not_found",
                                 f"子工作流 {wid} 不存在或不可见", f"nodes[{i}].workflow_id"))
            wkey = n.get("workflow_key")
            if (isinstance(wkey, str) and wkey and not wid
                    and ctx.workflow_keys and wkey not in ctx.workflow_keys):
                out.append(Issue("subflow_not_found",
                                 f"子工作流 key={wkey} 不存在或不可见",
                                 f"nodes[{i}].workflow_key"))

    return out


def _check_policy(spec: dict) -> list[Issue]:
    out: list[Issue] = []
    nodes = [n for _, n in _iter_nodes(spec)]
    policy = spec.get("policy") if isinstance(spec.get("policy"), dict) else {}
    max_nodes = min(int(policy.get("max_nodes") or MAX_NODES), MAX_NODES)
    if len(nodes) > max_nodes:
        out.append(Issue("too_many_nodes",
                         f"节点数 {len(nodes)} 超过上限 {max_nodes}", "$.nodes"))
    conc = policy.get("concurrency")
    if conc is not None and (not isinstance(conc, int) or not (1 <= conc <= MAX_CONCURRENCY)):
        out.append(Issue("bad_concurrency", f"concurrency 必须在 1~{MAX_CONCURRENCY}",
                         "$.policy.concurrency"))
    budget = policy.get("budget_tokens")
    if budget is not None and (not isinstance(budget, int) or budget > MAX_BUDGET_TOKENS):
        out.append(Issue("bad_budget", f"budget_tokens 不能超过 {MAX_BUDGET_TOKENS}",
                         "$.policy.budget_tokens"))
    return out


def _index_of(nodes: list[dict], key: str) -> int:
    for i, n in enumerate(nodes):
        if n.get("key") == key:
            return i
    return 0


def validate(spec: dict, ctx: SpecContext | None = None) -> list[Issue]:
    """跑全部三层检查。返回空列表 = 可以导入。"""
    issues = _check_structure(spec)
    # 结构都没过就不要跑语义层，否则全是"级联噪声"（例如边指向不存在的节点）
    if any(i.blocking and i.code in ("not_object", "bad_version") for i in issues):
        return issues
    issues += _check_semantics(spec, ctx)
    issues += _check_policy(spec)
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.blocking]


def summary(issues: list[Issue]) -> str:
    """压成一段可读文本（HTTP 422 的 detail 用）。"""
    lines = [f"- {i.path}: {i.message}" for i in issues]
    return "\n".join(lines[:20])
