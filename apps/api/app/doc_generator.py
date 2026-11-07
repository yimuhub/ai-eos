"""文档生成引擎 (P4)：Agent 产出 PPT / Word / Excel / MD。

LLM 输出结构化 JSON → 本模块渲染为二进制文档 → 存入该 Agent 的
专属交付文件夹（storage_root/artifacts/{agent_id}/）→ 登记 Asset。

聊天生成：POST /agents/{aid}/chat/generate-doc
任务生成：agent_runtime 检测 output 中 [GENERATE_DOC] 标记时自动调用。

渲染器按 Markdown 子集解析（标题 / 要点 / 有序列表 / 表格 / 加粗），
不是"把整段文本塞进一个段落"——报告类交付物的价值很大一部分在
表格与层级上，丢失它们等于交付一个打不开的排版。
"""
import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from app.core.config import settings


def storage_root() -> Path:
    p = Path(settings.storage_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / settings.storage_dir
    (p / "artifacts").mkdir(parents=True, exist_ok=True)
    return p


def _now():
    return datetime.now()


# ---------------- Markdown 结构解析 ----------------

_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET = re.compile(r"^[-*•]\s+(.*)$")
_ORDERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_blocks(content: str) -> list[tuple]:
    """把 Markdown 拆成 ('h', level, text) / ('p', text) / ('bullet', text)
    / ('ordered', no, text) / ('table', rows)。"""
    lines = (content or "").split("\n")
    blocks: list[tuple] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue

        if line.lstrip().startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cur = lines[i].strip()
                if not _TABLE_SEP.match(cur):
                    rows.append(_split_row(cur))
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue

        m = _HEADING.match(line.strip())
        if m:
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue

        m = _BULLET.match(line.strip())
        if m:
            blocks.append(("bullet", m.group(1).strip()))
            i += 1
            continue

        m = _ORDERED.match(line.strip())
        if m:
            blocks.append(("ordered", int(m.group(1)), m.group(2).strip()))
            i += 1
            continue

        blocks.append(("p", line.strip().lstrip(">").strip()))
        i += 1
    return blocks


def _add_rich_text(paragraph, text: str) -> None:
    """把 **加粗** 拆成独立 run，其余原样。"""
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        run = paragraph.add_run(m.group(1))
        run.bold = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


# ---------------- 渲染器 ----------------

def render_md(title: str, content: str) -> bytes:
    header = f"# {title}\n\n"
    return (header + content).encode("utf-8")


def _docx_set_east_asian(document) -> None:
    """中文字体：python-docx 默认只设西文字体，中文会回退成等线/宋体，
    在不同机器上版式不一致。这里显式指定中文为宋体、西文 Times New Roman。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    for name, size in (("Normal", 10.5), ("Heading 1", 16),
                       ("Heading 2", 14), ("Heading 3", 12)):
        try:
            style = document.styles[name]
        except KeyError:
            continue
        style.font.name = "Times New Roman"
        style.font.size = Pt(size)
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn("w:eastAsia"), "宋体")


def render_docx(title: str, content: str) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    _docx_set_east_asian(doc)
    for section in doc.sections:
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)
        section.left_margin = Cm(3.17)
        section.right_margin = Cm(3.17)

    doc.add_heading(title, level=0)

    for block in parse_blocks(content):
        kind = block[0]
        if kind == "h":
            _, level, text = block
            doc.add_heading(text, level=min(level, 4))
        elif kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            _add_rich_text(p, block[1])
        elif kind == "ordered":
            p = doc.add_paragraph(style="List Number")
            _add_rich_text(p, block[2])
        elif kind == "table":
            _, rows = block
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            table.autofit = True
            for ri, row in enumerate(rows):
                for ci in range(cols):
                    cell = table.cell(ri, ci)
                    text = row[ci] if ci < len(row) else ""
                    cell.text = ""
                    _add_rich_text(cell.paragraphs[0], text)
                    cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
                    if ri == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
                            run.font.size = Pt(10)
            # 表后留一个空段，避免两张表粘连
            doc.add_paragraph()
        else:
            p = doc.add_paragraph()
            _add_rich_text(p, block[1])

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_PCT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)%$")


def _coerce_cell(value: str):
    """把 '1,234' / '12.5%' / '89' 还原成数值，Excel 才能求和排序。
    全部转字符串会让报表失去可计算性——这是报表最基本的要求。"""
    s = (value or "").strip()
    if not s:
        return ""
    plain = s.replace(",", "").replace("，", "").replace(" ", "")
    if _NUM_RE.match(plain):
        return float(plain) if "." in plain else int(plain)
    m = _PCT_RE.match(plain)
    if m:
        return float(m.group(1)) / 100
    return s


def _markdown_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.split("\n"):
        cur = line.strip()
        if not cur.startswith("|"):
            continue
        if _TABLE_SEP.match(cur):
            continue
        rows.append(_split_row(cur))
    return rows


def _split_sections(content: str) -> list[tuple[str, str]]:
    """按 `## 标题` 切块；没有二级标题时整体作为一节。"""
    sections: list[tuple[str, list[str]]] = []
    cur_name, cur_lines = "", []
    for line in content.split("\n"):
        m = re.match(r"^##\s+(.*)$", line.strip())
        if m:
            if cur_lines:
                sections.append((cur_name, cur_lines))
            cur_name, cur_lines = m.group(1).strip(), []
            continue
        cur_lines.append(line)
    if cur_lines:
        sections.append((cur_name, cur_lines))
    return [(name or "数据", "\n".join(lines)) for name, lines in sections]


def _safe_sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:28] or "Sheet"
    candidate = base
    n = 2
    while candidate in used:
        suffix = f"_{n}"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def _fill_sheet(ws, rows: list[list[str]], title: str) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cols = max((len(r) for r in rows), default=1)
    for row in rows:
        ws.append([_coerce_cell(row[c]) if c < len(row) else "" for c in range(cols)])

    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4F46E5", end_color="4F46E5",
                                fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 百分比列：单元格存的是小数，显示成百分比
    percent_cols = set()
    for ci in range(cols):
        for row in rows[1:]:
            raw = row[ci] if ci < len(row) else ""
            if str(raw).strip().endswith("%"):
                percent_cols.add(ci + 1)
                break
    for c in percent_cols:
        for r in range(2, len(rows) + 1):
            ws.cell(row=r, column=c).number_format = "0.0%"

    ws.freeze_panes = "A2"
    if cols and len(rows) > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(cols)}{len(rows)}"

    for c in range(1, cols + 1):
        width = max((len(str(ws.cell(row=r, column=c).value or ""))
                     for r in range(1, len(rows) + 1)), default=8)
        ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 10), 50)


def render_xlsx(title: str, content: str) -> bytes:
    """content：Markdown 表格或 JSON 数组。

    多个 `## 小节` 各自含表时，每个小节一个 sheet（经营分析/周报常见：
    指标表、渠道表、计划表分表呈现），否则单 sheet。
    """
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set[str] = set()

    sections = _split_sections(content)
    table_sections = [(name, _markdown_rows(body)) for name, body in sections]
    table_sections = [(name, rows) for name, rows in table_sections if len(rows) >= 2]

    if len(table_sections) >= 2:
        for name, rows in table_sections:
            ws = wb.create_sheet(_safe_sheet_name(name, used))
            _fill_sheet(ws, rows, name)
    else:
        rows = table_sections[0][1] if table_sections else []
        if not rows:
            try:
                m = re.search(r"\[.*\]", content, re.S)
                data = json.loads(m.group()) if m else []
                if data and isinstance(data[0], dict):
                    headers = list(data[0].keys())
                    rows = [headers] + [[str(item.get(h, "")) for h in headers]
                                        for item in data]
                elif data:
                    rows = [["值"]] + [[str(x)] for x in data]
            except Exception:
                rows = []
        if not rows:
            lines = [l for l in content.strip().split("\n") if l.strip()]
            rows = [["内容"], *[[l] for l in lines[:500]]]
        ws = wb.create_sheet(_safe_sheet_name(title, used))
        _fill_sheet(ws, rows, title)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _add_pptx_table(slide, rows: list[list[str]], top_in: float) -> None:
    from pptx.util import Inches, Pt

    cols = max(len(r) for r in rows)
    shape = slide.shapes.add_table(
        len(rows), cols, Inches(0.6), Inches(top_in),
        Inches(9.0), Inches(0.4 * len(rows)))
    table = shape.table
    for ri, row in enumerate(rows):
        for ci in range(cols):
            cell = table.cell(ri, ci)
            cell.text = row[ci] if ci < len(row) else ""
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(11)
                    if ri == 0:
                        run.font.bold = True


def render_pptx(title: str, content: str) -> bytes:
    """按 `##` 分页；页内表格渲染成 PPT 原生表格，而不是竖线文本。"""
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    slide.placeholders[1].text = f"由 AI-EOS 数字员工生成 · {_now():%Y-%m-%d}"

    pages: list[tuple[str, list[tuple]]] = []
    cur_title, cur_blocks = None, []
    for block in parse_blocks(content):
        # h1 与封面标题重复，且它不是分页符——只按 `##` 分页
        if block[0] == "h" and block[1] == 1:
            continue
        if block[0] == "h" and block[1] == 2:
            if cur_title:
                pages.append((cur_title, cur_blocks))
            cur_title, cur_blocks = block[2], []
            continue
        cur_blocks.append(block)
    if cur_title or cur_blocks:
        pages.append((cur_title or "内容", cur_blocks))

    for page_title, blocks in pages:
        layout = prs.slide_layouts[1] if any(
            b[0] in ("bullet", "ordered") for b in blocks) else prs.slide_layouts[5]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = page_title[:80]
        bullets = [b[1] if b[0] == "bullet" else
                   (f"{b[1]}. {b[2]}" if b[0] == "ordered" else b[1])
                   for b in blocks if b[0] in ("bullet", "ordered", "p")]
        tables = [b[1] for b in blocks if b[0] == "table"]
        if bullets and len(slide.placeholders) > 1 and slide.placeholders[1].has_text_frame:
            tf = slide.placeholders[1].text_frame
            tf.text = bullets[0]
            for b in bullets[1:12]:
                p = tf.add_paragraph()
                p.text = b
                p.font.size = Pt(16)
        for i, rows in enumerate(tables[:1]):
            _add_pptx_table(slide, rows, top_in=1.6 if not bullets else 3.2)

    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


RENDERERS = {
    ".md": render_md,
    ".docx": render_docx,
    ".xlsx": render_xlsx,
    ".pptx": render_pptx,
}

DOC_TYPES = {
    "word": {"ext": ".docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "excel": {"ext": ".xlsx", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "ppt": {"ext": ".pptx", "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "md": {"ext": ".md", "mime": "text/markdown"},
}


def agent_artifact_dir(agent_id: str | None) -> Path:
    """每个 Agent 的专属交付文件夹。"""
    root = storage_root() / "artifacts"
    d = root / (agent_id or "shared")
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_document(doc_type: str, title: str, content: str,
                      agent_id: str | None = None) -> dict:
    """生成文档并写入 Agent 专属交付文件夹。
    返回 {file_path, file_name, size, mime}。抛异常表示类型不支持或渲染失败。"""
    spec = DOC_TYPES.get((doc_type or "").lower())
    if not spec:
        raise ValueError(f"unsupported doc_type: {doc_type} (use word/excel/ppt/md)")

    safe_title = re.sub(r"[^\w\u4e00-\u9fff-]", "_", title)[:40] or "document"
    ts = _now().strftime("%Y%m%d_%H%M%S")
    renderer = RENDERERS[spec["ext"]]
    data = renderer(title, content)
    file_name = f"{safe_title}_{ts}{spec['ext']}"

    out_dir = agent_artifact_dir(agent_id)
    out_path = out_dir / file_name
    out_path.write_bytes(data)

    return {"file_path": str(out_path.resolve()), "file_name": file_name,
            "size": len(data), "mime": spec["mime"],
            "agent_dir": str(out_dir)}


# ---------------- LLM 输出中的文档标记解析 ----------------

def parse_doc_markers(llm_output: str) -> list[dict]:
    """从 LLM 回复中提取 [GENERATE_DOC] 标记。
    格式：
    [GENERATE_DOC]
    {"type":"ppt","title":"...","content":"## 页1\\n- 要点..."}
    [/GENERATE_DOC]
    """
    docs = []
    pattern = re.compile(
        r"\[GENERATE_DOC\]\s*(\{.*?\})\s*\[/GENERATE_DOC\]", re.S)
    for m in pattern.finditer(llm_output or ""):
        raw = m.group(1)
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            # 容错：字段级提取（模型可能输出不严格JSON），并反转义
            def _grab(field):
                fm = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                return fm.group(1) if fm else None
            t, ti, c = _grab("type"), _grab("title"), _grab("content")
            if not (t and ti and c):
                continue
            # 修复：此前 encode("utf-8").decode("unicode_escape") 按 latin-1
            # 映射字节，中文全部变乱码；字面量 \n 统一交给下方 replace 处理。
            spec = {"type": t, "title": ti, "content": c}
        if spec.get("type") and spec.get("title") and spec.get("content"):
            if isinstance(spec["content"], str):
                # 模型有时把换行写成字面量 \n
                spec["content"] = spec["content"].replace("\\n", "\n")
            docs.append(spec)
    return docs
