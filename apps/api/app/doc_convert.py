"""文档→Markdown 转换（公共工具）。"""
import io
from pathlib import Path


def extract_to_md(file_name: str, raw: bytes) -> tuple[str, bool]:
    """返回 (markdown文本或空, 是否成功)。docx/pdf/xlsx/pptx 自动转 MD 结构。"""
    suffix = Path(file_name).suffix.lower()
    if suffix in (".txt", ".md", ".csv", ".json"):
        return raw.decode("utf-8", errors="replace"), True
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            pages = []
            for i, pg in enumerate(reader.pages):
                t = pg.extract_text() or ""
                pages.append(f"## 第{i + 1}页\n\n{t}")
            return "\n\n".join(pages), True
        except Exception:
            return "", False
    if suffix == ".docx":
        try:
            from docx import Document as Docx
            dx = Docx(io.BytesIO(raw))
            lines = []
            for p in dx.paragraphs:
                st = (p.style.name or "").lower()
                txt = p.text.strip()
                if not txt:
                    continue
                if "heading 1" in st or st == "title":
                    lines.append(f"# {txt}")
                elif "heading 2" in st:
                    lines.append(f"## {txt}")
                elif "heading 3" in st:
                    lines.append(f"### {txt}")
                else:
                    lines.append(txt)
            for tbl in dx.tables:
                rows = ["| " + " | ".join(c.text.strip() for c in r.cells) + " |"
                        for r in tbl.rows]
                if rows:
                    lines.append("\n".join(rows))
            return "\n\n".join(lines), True
        except Exception:
            return "", False
    # 修复：openpyxl 不支持 legacy .xls，此前 .xls 走这里必然异常被吞、
    # 静默返回解析失败——如实降级为不支持，不再伪装支持。
    if suffix == ".xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"## 工作表：{ws.title}\n")
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c) for c in row]
                    if any(cells):
                        out.append("| " + " | ".join(cells) + " |")
            return "\n".join(out), True
        except Exception:
            return "", False
    if suffix == ".pptx":
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(raw))
            out = []
            for i, slide in enumerate(prs.slides):
                title = ""
                bullets = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        t = shape.text_frame.text.strip()
                        if not t:
                            continue
                        if shape == slide.shapes.title:
                            title = t
                        else:
                            bullets.extend(
                                ln.strip() for ln in t.split("\n") if ln.strip())
                out.append(f"## 幻灯片{i + 1}：{title or '(无标题)'}\n" +
                           "\n".join(f"- {b}" for b in bullets))
            return "\n\n".join(out), True
        except Exception:
            return "", False
    # 兜底当纯文本
    try:
        return raw.decode("utf-8", errors="replace"), True
    except Exception:
        return "", False


async def convert_via_llm(text: str, model_id: str, gw) -> str:
    """用专用转换大模型把文本整理为更适合机器学习/检索的 MD。
    降低成本：使用模型中心单独配置的转换模型。"""
    messages = [
        {"role": "system", "content":
            "你是文档转换器。把输入内容整理为结构清晰的 Markdown："
            "修正乱码、补齐标题层级、列表化要点、保留全部事实信息。"
            "只输出转换后的 Markdown，不要解释。"},
        {"role": "user", "content": text[:12000]},
    ]
    r = await gw.chat_with_agent_models([model_id], messages,
                                        config={"max_tokens": 4096,
                                                "temperature": 0.2})
    return (r.get("content") or "").strip() or text
