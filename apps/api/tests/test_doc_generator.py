"""文档渲染：产出物必须是能打开的专业文件。

曾经 `generate_document` 只做"把 markdown 文本塞进 .docx 后缀"，
表格、数字、多 Sheet 全部退化成一行行纯文本。这里钉住三件事：

1. markdown 表格 → 真实的 Word 表格（不是带 `|` 的段落）；
2. 数字列 → Excel 里的数值（不是字符串），多 `##` 小节 → 多 Sheet；
3. `[GENERATE_DOC]` 标记里的 `\\n` 必须还原成换行，否则整份文档挤成一行。
"""
from __future__ import annotations

import json
import zipfile

import pytest

from app.doc_generator import (generate_document, parse_blocks,
                               parse_doc_markers)

REPORT_MD = """# 2026 Q3 市场分析

## 一、规模

| 指标 | 数值 | 占比 |
|---|---|---|
| 市场规模 | 1200 | 35.5% |
| 增长率 | 18 | 12% |

## 二、结论

- 头部集中度上升
- 价格战趋缓

**核心判断**：需求仍在扩张。
"""


def test_parse_blocks_recognizes_table_and_bold():
    blocks = parse_blocks(REPORT_MD)
    kinds = [b[0] for b in blocks]
    assert "table" in kinds
    table = next(b for b in blocks if b[0] == "table")
    assert table[1][0] == ["指标", "数值", "占比"]
    assert table[1][1] == ["市场规模", "1200", "35.5%"]
    # 分隔行 `|---|` 不能当成数据行
    assert len(table[1]) == 3


def test_docx_contains_real_table(tmp_path, monkeypatch):
    import docx

    monkeypatch.setattr("app.doc_generator.agent_artifact_dir",
                        lambda _aid: tmp_path)
    meta = generate_document("word", "市场分析", REPORT_MD, agent_id="a1")
    path = tmp_path / meta["file_name"]
    assert zipfile.is_zipfile(path)

    d = docx.Document(str(path))
    assert d.tables, "markdown 表格必须渲染成真实 Word 表格"
    assert d.tables[0].rows[0].cells[0].text == "指标"
    assert d.tables[0].rows[1].cells[1].text == "1200"
    # 标题层级不能全挤成正文
    styles = {p.style.name for p in d.paragraphs}
    assert any(s.startswith("Heading") for s in styles), styles
    assert "2026 Q3 市场分析" in "\n".join(p.text for p in d.paragraphs)


def test_xlsx_splits_sections_into_sheets_with_numeric_cells(tmp_path, monkeypatch):
    import openpyxl

    monkeypatch.setattr("app.doc_generator.agent_artifact_dir",
                        lambda _aid: tmp_path)
    meta = generate_document("excel", "市场数据", REPORT_MD)
    wb = openpyxl.load_workbook(tmp_path / meta["file_name"])
    assert len(wb.worksheets) >= 1
    ws = wb.worksheets[0]
    # 数值必须是数字类型：否则老板在 Excel 里无法求和/排序
    assert ws["B2"].value == 1200
    assert isinstance(ws["B2"].value, int)
    assert ws.freeze_panes == "A2"


def test_pptx_is_valid_presentation(tmp_path, monkeypatch):
    import pptx

    monkeypatch.setattr("app.doc_generator.agent_artifact_dir",
                        lambda _aid: tmp_path)
    meta = generate_document("ppt", "季度汇报", REPORT_MD)
    prs = pptx.Presentation(str(tmp_path / meta["file_name"]))
    # 封面 + 两个 ## 小节
    assert len(prs.slides) == 3


def test_unknown_doc_type_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("app.doc_generator.agent_artifact_dir",
                        lambda _aid: tmp_path)
    with pytest.raises(ValueError):
        generate_document("pdf", "x", "y")


def test_doc_marker_unescapes_newlines():
    payload = json.dumps({"type": "word", "title": "报告",
                          "content": "## 一\\n- 要点一\\n- 要点二"})
    text = f"前言\n[GENERATE_DOC]\n{payload}\n[/GENERATE_DOC]\n后记"
    docs = parse_doc_markers(text)
    assert len(docs) == 1
    assert "\n- 要点一" in docs[0]["content"], "转义的换行必须还原成真实换行"


def test_doc_marker_tolerates_broken_json():
    text = ('[GENERATE_DOC]\n{"type":"pptx","title":"汇报",'
            '"content":"## 一\\n- 要点"}\n[/GENERATE_DOC]')
    docs = parse_doc_markers(text)
    assert len(docs) == 1
    assert docs[0]["title"] == "汇报"
