"""机械断言纯函数单测（不调模型、不联网）。

重点不是"能跑通"，而是三条防线各自真的生效：
1. 有效字数挡不住注水时，必须有重复率兜底；
2. 文件块三种写法都要认（不同角色的产出格式不会统一）；
3. 未知规则名必须报错——静默跳过等于闸门虚设。
"""
from app.gates.rules import (Check, GateSpec, blocking_failures,
                             citation_stats, duplicate_ratio, effective_chars,
                             evaluate, extract_file_blocks, table_data_rows)


def test_effective_chars_ignores_markup_and_whitespace():
    # Markdown 符号、空白、标点都不计；中文逐字，英文逐词
    assert effective_chars("你好，世界") == 4
    assert effective_chars("# 标题\n\n**加粗**") == 4
    assert effective_chars("a b c") == 3
    assert effective_chars("") == 0


def test_duplicate_ratio_catches_padding():
    one = "这是一句完全独立的话，说明第一个要点。"
    text = (one * 4) + "另一句不同的话，说明第二个要点。第三句也各不相同的内容在此。"
    assert duplicate_ratio(text) >= 0.5


def test_duplicate_ratio_needs_enough_sentences():
    # 句子太少不做判断，避免把正常短文本误杀
    assert duplicate_ratio("第一句。第二句。第三句。") == 0.0


def test_extract_file_blocks_three_marker_styles():
    text = """### 文件：src/app.py
内容和代码

【文件：README.md】
说明

```python tests/test_app.py
def test_ok():
    pass
```
"""
    blocks = extract_file_blocks(text)
    assert blocks == ["src/app.py", "README.md", "tests/test_app.py"]


def test_citation_stats_dedupes_same_url():
    text = ("来源：https://a.example.com/x 与 https://a.example.com/x "
            "还有 https://b.example.com/y")
    count, domains = citation_stats(text)
    assert count == 2      # 同一链接重复出现只算一次
    assert domains == 2    # 域名数用来识别"同一链接刷引用"


def test_table_data_rows_excludes_header_and_separator():
    text = """| 方案 | 成本 |
|---|---|
| A | 100 |
| B | 200 |
"""
    assert table_data_rows(text) == 2


def test_evaluate_blocks_padded_output():
    spec = GateSpec(asserts={"min_chars": 600, "max_duplicate_ratio": 0.15})
    padded = ("我们要认真思考这个问题。" * 40)  # 字数够但有大量重复
    checks = evaluate(padded, [], spec)
    failed = {c.rule for c in blocking_failures(checks)}
    # 有效字数可能过、重复率必须过不去
    assert "max_duplicate_ratio" in failed


def test_evaluate_emits_files_counts_exactly():
    spec = GateSpec(asserts={"emits_files": 2})
    ok = evaluate("### 文件：a.py\n\n【文件：b.py】\n", [], spec)
    assert not blocking_failures(ok)
    short = evaluate("### 文件：a.py\n", [], spec)
    assert {c.rule for c in blocking_failures(short)} == {"emits_files"}


def test_json_schema_subset_pass_and_fail():
    schema = {"type": "object", "required": ["pick", "fix_list"],
              "properties": {"pick": {"enum": ["A", "B"]},
                             "fix_list": {"type": "array", "minItems": 3}}}
    good = '{"pick": "A", "fix_list": ["1", "2", "3"]}'
    bad = '{"pick": "C", "fix_list": ["1"]}'
    assert not blocking_failures(evaluate(good, [], GateSpec(asserts={"json_schema": schema})))
    fails = {c.rule for c in blocking_failures(
        evaluate(bad, [], GateSpec(asserts={"json_schema": schema})))}
    assert fails == {"json_schema"}


def test_no_ai_cliche_blocks_deflection():
    text = "作为AI，我无法提供这个信息。"
    fails = {c.rule for c in blocking_failures(
        evaluate(text, [], GateSpec(asserts={"min_chars": 1})))}
    assert "no_ai_cliche" in fails


def test_unknown_rule_raises_instead_of_silently_passing():
    try:
        evaluate("内容", [], GateSpec(asserts={"min_chr": 100}))
    except ValueError as e:
        assert "min_chr" in str(e)
    else:
        raise AssertionError("未知规则名必须报错")


def test_check_serializes_for_evidence_table():
    c = Check("min_chars", False, 800, 412, detail="有效字")
    d = c.to_dict()
    assert d["rule"] == "min_chars" and d["passed"] is False and d["actual"] == 412


def test_no_ai_cliche_has_word_boundary():
    """"作为AI企业"是正常表述，不能被"作为AI"误伤。"""
    ok = evaluate("作为AI企业，我们提供三类方案。", [],
                  GateSpec(asserts={"min_chars": 1}))
    assert not blocking_failures(ok)


def test_no_ai_cliche_can_be_disabled_per_node():
    text = "作为AI，我无法提供该数据。"
    checks = evaluate(text, [], GateSpec(asserts={"min_chars": 1}, allow_cliche=True))
    assert not [c for c in checks if c.rule == "no_ai_cliche"]
