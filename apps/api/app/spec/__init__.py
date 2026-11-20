"""workflow-spec：把工作流变成可入 Git、可分享、可给外部 Agent 消费的文件。

    from app.spec import validate, parse_spec, compile_spec, serialize_workflow

    issues = validate(spec, ctx)              # 不写库
    wf = await compile_spec(db, spec, ...)    # 校验 + 落库
    spec2 = await serialize_workflow(db, wf)  # 落库 + 还原

三条约束（写在文档里，也写在代码里）：

* **文件是契约，DB 是真相**。导入是显式动作，导出只读。
* **先校验后写库**。校验不过一行都不落。
* **往返幂等**。导出→导入→导出必须字节一致，否则说明有隐式状态没清干净。
"""
from .compiler import (SpecInvalid, SpecParseError, build_context, compile_spec,
                       parse_spec, validate_spec)
from .schema import MAX_NODES, NODE_TYPES, SPEC_VERSION, build_json_schema
from .serializer import dump_spec, serialize_workflow
from .validator import Issue, SpecContext, errors, summary, validate

__all__ = [
    "Issue", "MAX_NODES", "NODE_TYPES", "SPEC_VERSION", "SpecContext",
    "SpecInvalid", "SpecParseError", "build_context", "build_json_schema",
    "compile_spec", "dump_spec", "errors", "parse_spec",
    "serialize_workflow", "summary", "validate", "validate_spec",
]
