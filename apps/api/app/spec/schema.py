"""workflow-spec v1 的字段契约（唯一真相源）。

`validator` / `compiler` / `serializer` / 编辑器 JSON Schema 全部从这里读，
避免"文档写一套、代码写一套"。改字段只改这里。

## 为什么是显式 edges，不是 depends_on

我们的执行器是**边触发**的：一个节点完成就把所有出边目标加入下一批，
只有 MERGE 节点做汇聚，且是"先到先过"。

如果照抄 depends_on 语义，一个"依赖 A 和 B"的节点会在 A 完成后就被调度一次、
B 完成后又被调度一次（或更糟：带着半成品输入执行）。那不是并行，是错误。

所以 spec v1 只写显式边；要表达"等 A 和 B 都完成"，就显式插入 MERGE 节点。
代价是多两个节点，收益是不会静默跑错。
"""
from __future__ import annotations

SPEC_VERSION = "v1"

# 允许的节点类型：与 app.models.workflow.NODE_TYPES 完全对齐（不许多写、不许少写）
NODE_TYPES = (
    "START", "END", "TASK", "AGENT", "CONDITION", "PARALLEL", "MERGE",
    "WAIT", "APPROVAL", "HUMAN_INPUT", "SUB_WORKFLOW", "HTTP", "TOOL",
    "TRANSFORM",
)

CONTROL_TYPES = ("START", "END", "MERGE", "PARALLEL")

# 顶层字段（严格白名单：写错一个字段名要报错，不能静默忽略）
TOP_FIELDS = {
    "spec", "key", "name", "description", "inputs", "nodes", "edges", "policy",
}

INPUT_FIELDS = {"name", "type", "required", "default", "description"}
INPUT_TYPES = {"text", "number", "boolean", "json", "file"}

EDGE_FIELDS = {"from", "to", "label"}

POLICY_FIELDS = {"max_nodes", "concurrency", "budget_tokens", "allow_partial"}

NODE_COMMON_FIELDS = {"key", "type", "name", "x", "y"}

# 各类型节点额外允许的字段（多了要报"该类型不支持此字段"）
NODE_TYPE_FIELDS = {
    "START": set(),
    "END": set(),
    "TASK": {"task"},
    "AGENT": {"task", "agent", "agent_id", "output", "gate", "config"},
    "CONDITION": {"keyword"},
    "PARALLEL": set(),
    "MERGE": set(),
    "WAIT": {"seconds"},
    "APPROVAL": {"approver_hint"},
    "HUMAN_INPUT": {"prompt"},
    "SUB_WORKFLOW": {"workflow_id", "workflow_key"},
    "HTTP": {"config"},
    "TOOL": {"config"},
    "TRANSFORM": {"config"},
}

# 必须提供值的字段（编译期会再校验一次，这里用于尽早给出可读报错）
REQUIRED_FIELDS = {
    "AGENT": ("agent",),
    "CONDITION": ("keyword",),
    "SUB_WORKFLOW": (),   # workflow_id 或 workflow_key 二选一
    "HTTP": ("url",),
    "TOOL": ("tool_name",),
    "TRANSFORM": ("set",),
}

MAX_NODES = 24          # 与 workflow_authoring.MAX_NODES 一致
MAX_CONCURRENCY = 8
MAX_BUDGET_TOKENS = 2_000_000

KEY_RE = r"^[a-z][a-z0-9_-]{0,63}$"
VAR_RE = r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}"


def build_json_schema() -> dict:
    """给编辑器的 JSON Schema（VS Code / JetBrains 补全用）。

    刻意只做"形状提示"：跨字段检查（环、变量未定义、员工不存在）靠
    `POST /workflows/validate`，Schema 查不出来。
    """
    node_props = {
        "key": {"type": "string", "pattern": KEY_RE,
                "description": "图内唯一标识，小写字母开头"},
        "type": {"type": "string", "enum": list(NODE_TYPES)},
        "name": {"type": "string", "maxLength": 300},
        "x": {"type": "number"}, "y": {"type": "number"},
        "task": {"type": "string", "description": "支持 {{变量}}，仅 AGENT"},
        "agent": {"type": "string", "description": "员工姓名或 id"},
        "agent_id": {"type": "string"},
        "output": {"type": "string", "description": "产出别名，下游用 {{别名}} 引用"},
        "gate": {
            "type": "object",
            "properties": {
                "acceptance": {"type": "string"},
                "assert": {"type": "object"},
                "max_rework": {"type": "integer", "minimum": 0, "maximum": 1},
                "on_fail": {"type": "string", "enum": ["fail", "manual", "observe"]},
                "judge_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                "allow_cliche": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "config": {"type": "object", "description": "HTTP/TOOL/TRANSFORM 的配置"},
        "keyword": {"type": "string"},
        "seconds": {"type": "integer", "minimum": 0, "maximum": 60},
        "approver_hint": {"type": "string"},
        "prompt": {"type": "string"},
        "workflow_id": {"type": "string"},
        "workflow_key": {"type": "string"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ai-eos.local/schemas/workflow.schema.json",
        "title": "AI-EOS workflow-spec v1",
        "type": "object",
        "required": ["spec", "name", "nodes", "edges"],
        "properties": {
            "spec": {"const": SPEC_VERSION},
            "key": {"type": "string", "pattern": KEY_RE},
            "name": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": {"type": "string", "maxLength": 2000},
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string",
                                 "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
                        "type": {"type": "string", "enum": sorted(INPUT_TYPES)},
                        "required": {"type": "boolean"},
                        "default": {},
                        "description": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "nodes": {"type": "array", "items": {
                "type": "object",
                "required": ["key", "type"],
                "properties": node_props,
                "additionalProperties": False,
            }},
            "edges": {"type": "array", "items": {
                "type": "object",
                "required": ["from", "to"],
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "label": {"type": "string"},
                },
                "additionalProperties": False,
            }},
            "policy": {
                "type": "object",
                "properties": {
                    "max_nodes": {"type": "integer", "maximum": MAX_NODES},
                    "concurrency": {"type": "integer", "maximum": MAX_CONCURRENCY},
                    "budget_tokens": {"type": "integer", "maximum": MAX_BUDGET_TOKENS},
                    "allow_partial": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
