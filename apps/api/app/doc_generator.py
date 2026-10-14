"""文档生成引擎 (P4)：Agent 产出 PPT / Word / Excel / MD。

LLM 输出结构化 JSON → 本模块渲染为二进制文档 → 存入该 Agent 的
专属交付文件夹（storage_root/artifacts/{agent_id}/）→ 登记 Asset。

聊天生成：POST /agents/{aid}/chat/generate-doc
任务生成：agent_runtime 检测 output 中 [GENERATE_DOC] 标记时自动调用。
"""
import json
import re
import uuid
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


# ---------------- 渲染器 ----------------

def render_md(title: str, content: str) -> bytes:
    header = f"# {title}\n\n"
    return (header + content).encode("utf-8")


def render_docx(title: str, content: str) -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    h = doc.add_heading(title, level=0)
    for para in re.split(r"\n\s*\n|\n", content):
        p = para.strip()
        if not p:
            continue
        if p.startswith("# "):
            doc.add_heading(p[2:], level=1)
        elif p.startswith("## "):
            doc.add_heading(p[3:], level=2)
        elif p.startswith("- ") or p.startswith("• "):
            doc.add_paragraph(p[2:], style="List Bullet")
        else:
            pa = doc.add_paragraph(p)
            pa.runs[0].font.size = Pt(11) if pa.runs else None
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_xlsx(title: str, content: str) -> bytes:
    """content 格式：markdown 表格或 JSON 数组。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title[:28] or "Sheet1"

    rows = []
    # 尝试 markdown 表格解析
    lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
    table_lines = [l for l in lines if l.startswith("|")]
    if len(table_lines) >= 2:
        for l in table_lines:
            cells = [c.strip() for c in l.strip("|").split("|")]
            if all(re.match(r"^[-: ]+$", c) for c in cells):  # 分隔行跳过
                continue
            rows.append(cells)
    else:
        # 尝试 JSON
        try:
            m = re.search(r"\[.*\]", content, re.S)
            data = json.loads(m.group()) if m else []
            if data and isinstance(data[0], dict):
                headers = list(data[0].keys())
                rows.append(headers)
                for item in data:
                    rows.append([str(item.get(h, "")) for h in headers])
            else:
                rows = [[str(x)] for x in data]
        except Exception:
            # 纯文本按行输出
            rows = [[l] for l in lines]

    if not rows:
        rows = [[title], [content[:500]]]

    for r in rows:
        ws.append(r)
    # 表头样式
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4F46E5", end_color="4F46E5",
                                fill_type="solid")
    # 列宽自适应
    for col in ws.columns:
        width = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 50)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_pptx(title: str, content: str) -> bytes:
    """content 按 markdown 标题分页：## 为新页标题，正文为要点。"""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    # 封面
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    slide.placeholders[1].text = f"由 AI-EOS 数字员工生成 · {_now():%Y-%m-%d}"

    # 解析分页
    slides_data: list[tuple[str, list[str]]] = []
    cur_title, cur_bullets = None, []
    for line in content.split("\n"):
        s = line.rstrip()
        if s.startswith("## "):
            if cur_title:
                slides_data.append((cur_title, cur_bullets))
            cur_title, cur_bullets = s[3:].strip(), []
        elif s.startswith("# "):
            continue  # 主标题已在封面
        elif s.strip():
            clean = s.lstrip("- •").strip()
            if clean:
                cur_bullets.append(clean)
    if cur_title or cur_bullets:
        slides_data.append((cur_title or "内容", cur_bullets))

    for st, bullets in slides_data:
        layout = prs.slide_layouts[1] if bullets else prs.slide_layouts[5]
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = st[:80]
        if bullets and slide.placeholders[1].has_text_frame:
            tf = slide.placeholders[1].text_frame
            tf.text = bullets[0]
            for b in bullets[1:]:
                p = tf.add_paragraph()
                p.text = b
                p.font.size = Pt(18)

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
    spec = DOC_TYPES.get(doc_type.lower())
    if not spec:
        raise ValueError(f"unsupported doc_type: {doc_type} (use word/excel/ppt/md)")

    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]", "_", title)[:40]
    ts = _now().strftime("%Y%m%d_%H%M%S")
    renderer = RENDERERS[spec["ext"]]
    data = renderer(title, content)

    safe_title = re.sub(r"[^\w\u4e00-\u9fff-]", "_", title)[:40]
    ts = _now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{safe_name}_{ts}{spec['ext']}"

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
    for m in pattern.finditer(llm_output):
        raw = m.group(1)
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            # 容错：字段级提取（模型可能输出不严格JSON）
            def _grab(field):
                fm = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                return fm.group(1) if fm else None
            t, ti, c = _grab("type"), _grab("title"), _grab("content")
            if not (t and ti and c):
                continue
            spec = {"type": t, "title": ti, "content": c}
        if spec.get("type") and spec.get("title") and spec.get("content"):
            docs.append(spec)
    return docs
