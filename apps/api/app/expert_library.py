"""Expert Template Importer — 解析 agency-agents-zh 仓库的专家 Markdown。

无 PyYAML 依赖：frontmatter 极简（name/description/emoji/color），手工解析。
"""
import re

SOURCE_URL = "https://github.com/jnMetaCode/agency-agents-zh"

# 部门目录 → 中文工种名
DEPT_NAMES = {
    "company": "公司高管", "engineering": "工程", "design": "设计",
    "marketing": "营销", "paid-media": "付费媒体", "sales": "销售",
    "finance": "金融", "hr": "人力资源", "legal": "法务",
    "supply-chain": "供应链", "product": "产品", "project-management": "项目管理",
    "testing": "测试", "support": "支持", "specialized": "专项",
    "spatial-computing": "空间计算", "game-development": "游戏开发",
    "academic": "学术", "gis": "GIS", "security": "安全", "strategy": "战略",
    "integrations": "集成",
}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析极简 YAML frontmatter（key: value 单行）。"""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            fm = text[3:end]
            for line in fm.splitlines():
                m = re.match(r"^\s*([a-z_]+)\s*:\s*(.*)$", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip('"')
            body = text[end + 4:].lstrip("\n")
    return meta, body


def _extract_deliverables(body: str) -> list[str]:
    """提取「交付物」章节的一级条目（供验收出题）。
    章节范围到下一个同级的 ## 标题为止（### 子标题内的条目也收集）。"""
    m = re.search(r"(?ms)^##\s*.*交付物.*?$\n(.*?)(?=^##\s|\Z)", body)
    if not m:
        return []
    items = [re.sub(r"\*\*(.+?)\*\*", r"\1", ln.strip()[1:])
             for ln in m.group(1).splitlines()
             if ln.strip().startswith(("-", "*"))]
    return [i.strip()[:120] for i in items if i][:10]


def _truncate_prompt(body: str, limit: int = 6000) -> str:
    """system_prompt 截断策略：保留 身份/使命/规则 章节全文，交付物与流程按余量。"""
    if len(body) <= limit:
        return body
    # 章节优先级：身份 > 使命 > 规则 > 交付物 > 流程 > 其余
    secs = re.split(r"(?m)^##\s+", body)
    ordered: list[str] = []
    rest: list[str] = []
    for s in secs:
        head = s.split("\n", 1)[0].lower()
        if any(k in head for k in ("身份", "记忆")):
            ordered.insert(0, "## " + s.rstrip())
        elif any(k in head for k in ("使命", "核心")):
            ordered.insert(1, "## " + s.rstrip()) if len(ordered) >= 1 else ordered.append("## " + s.rstrip())
        elif any(k in head for k in ("规则", "必须")):
            ordered.append("## " + s.rstrip())
        else:
            rest.append(s.rstrip())
    out = "# 专家人设\n"
    for s in ordered:
        out += s + "\n\n"
    for s in rest:  # 余量内能放多少放多少
        if len(out) + len(s) + 2 <= limit:
            out += s + "\n\n"
    out = out[:limit]
    out += "\n\n（人设较长，此处为核心摘录；完整版本见模板详情）"
    return out


def parse_expert_md(path_name: str, text: str, source_dir: str) -> dict | None:
    """解析单个专家 md → 模板记录 dict。"""
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or re.sub(r"[-_]", " ", path_name).strip()
    if not name:
        return None
    return {
        "source_slug": f"{source_dir}-{path_name}",
        "name": name,
        "emoji": meta.get("emoji"),
        "color": meta.get("color"),
        "department": source_dir,
        "dept_label": DEPT_NAMES.get(source_dir, source_dir),
        "description": meta.get("description") or "",
        "system_prompt": _truncate_prompt(body),
        "raw_md": text,
        "deliverables": _extract_deliverables(body),
    }
