"""技能库导入器（V6.2 专家技能引入方案的核心）。

三个通道，统一落库为 skill_library 条目：
* SKILL.md    → kind=skill     （提示型技能：frontmatter + 方法论正文）
* mcp.json    → kind=connector （连接器蓝图：MCP server 配置）
* experts/ 包 → kind=expert    （agents/*.md 人设 + settings.json 编排）

配套能力：
* frontmatter 极简解析（项目无 PyYAML，不为此加依赖）；
* 触发词提取（引号触发语 / TRIGGER: 行 / 「当用户…」句式）；
* 行业分类（10 行业包关键词 + 跨行业通用层，三级匹配的第一级）；
* 品牌/许可扫描（brand_refs 命中即 review，不放 auto_pass）。

导入是纯函数（不碰 DB），路由层负责落库与审核。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

# ─────────────────────────── 行业分类关键词 ───────────────────────────
# 行业层：与 10 个内置行业包 key 一一对应；通用层：跨行业随装随用。
INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    # 行业层
    "catering": ["餐饮", "门店", "外卖", "排班", "食安", "食品", "菜单", "储值",
                 "会员复购", "catering", "restaurant"],
    "equipment": ["特种设备", "定检", "制造台账", "OEE", "精加工", "焊接",
                  "装备制造", "equipment", "manufacturing"],
    "ecom": ["跨境", "电商", "listing", "亚马逊", "亚马逊", "shopify", "独立站",
             "汇率", "退货率", "选品", "ecom", "cross-border"],
    "logistics": ["物流", "仓储", "运力", "干线", "配送", "周转", "安责险",
                  "logistics", "warehouse"],
    "medical": ["医美", "执业资质", "耗材", "病历", "客诉处置", "皮肤",
                "medical", "clinic"],
    "auto": ["汽修", "工单", "配件库存", "钣金", "喷漆", "危废", "auto repair"],
    "edu": ["教培", "消课", "课酬", "师资", "退费", "招生", "edu", "training"],
    "stay": ["民宿", "入住率", "OTA", "房态", "民宿预订", "homestay"],
    "law": ["律师", "案卷", "利益冲突", "计时计费", "法务", "律所", "legal"],
    "build": ["建筑", "工程", "进度产值", "农民工工资", "安全巡检", "施工",
              "construction"],
    # 通用层
    "general:office": ["办公", "文档", "表格", "PPT", "演示", "会议纪要",
                       "简历", "周报", "office", "document"],
    "general:data": ["数据分析", "报表", "指标", "统计", "可视化", "excel",
                     "data"],
    "general:marketing": ["营销", "投放", "私域", "文案", "内容创作", "种草",
                          "营销活动", "marketing", "content"],
    "general:finance": ["发票", "对账", "应收", "报销", "预算", "财税",
                        "finance", "invoice"],
    "general:hr": ["招聘", "绩效", "入离职", "社保", "人力", "hr"],
    "general:sales": ["销售", "线索", "跟单", " CRM", "商机", "客户建档",
                      "sales", "crm"],
    "general:dev": ["代码", "开发", "部署", "API", "工程", "编码", "dev"],
}

# 品牌词：命中即不放 auto_pass（需人工核 license 与品牌引用）。
KNOWN_BRANDS: list[str] = [
    "workbuddy", "codebuddy", "qiqi", "ircloud", "58pic", "3chat", "77hub",
    "agentearth", "maton", "virxact", "neodata", "westock", "ai-shifu",
    "chanmama", "蝉妈妈", "千瓜", "腾讯", "企业微信", "钉钉", "抖音",
]

# 通用提示注入特征（方法论正文里教 agent 越权/无视规则的行）。
_INJECTION_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"reveal\s+your\s+(system\s+)?(prompt|instructions)", re.I),
    re.compile(r"忽略(之前|以上|上述|前面)的?(指令|设定|提示)"),
    re.compile(r"无视(之前|以上|上述|前面)的?(指令|设定|提示)"),
    re.compile(r"(system|assistant)\s*[:：]\s*$", re.I | re.M),
]

_MAX_BODY = 6000          # 方法论正文入库截断
_MAX_TRIGGER = 10         # 触发词上限
_MAX_INDUSTY_TAGS = 5     # 行业标签上限


def _uuid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────── frontmatter 解析 ───────────────────────────
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """极简 YAML frontmatter 解析：只支持 `key: value` 与 `key: |` 多行块。
    项目无 PyYAML，不为此加依赖；解析失败的行忽略（技能正文照常入库）。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    meta: dict[str, Any] = {}
    body_start = len(lines)
    key: str | None = None
    block: list[str] = []
    for i, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if stripped == "---":
            body_start = i + 1
            break
        if line.startswith(("  ", "\t")) and key and stripped:
            block.append(stripped)
            continue
        if key and block:
            meta[key] = "\n".join(block).strip()
            key, block = None, []
        m = re.match(r"^([A-Za-z_][\w-]*)\s*[:：]\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if v in ("|", ">"):
            key, block = k, []
        elif v:
            meta[k] = v.strip("'\"")
        else:
            key, block = k, []
    if key and block:
        meta[key] = "\n".join(block).strip()
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


# ─────────────────────────── 触发词提取 ───────────────────────────
def extract_triggers(text: str, limit: int = _MAX_TRIGGER) -> list[str]:
    """从 description/正文提取触发词：引号短语 + TRIGGER: 行 + 高频领域词。"""
    triggers: list[str] = []
    quoted = re.findall(r"[「\"“]([^」\"”]{2,24})[」\"”]", text)
    triggers.extend(q.strip() for q in quoted[:limit])
    for line in text.split("\n"):
        m = re.match(r"\s*(?:TRIGGER|触发)[:：]\s*(.+)$", line, re.I)
        if m:
            for seg in re.split(r"[、,，/;；]", m.group(1)):
                seg = seg.strip().strip("\"'「」")
                if 2 <= len(seg) <= 24:
                    triggers.append(seg)
    seen: set[str] = set()
    out: list[str] = []
    for t in triggers:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
        if len(out) >= limit:
            break
    return out


# ─────────────────────────── 行业分类 ───────────────────────────
def classify_industry(text: str, limit: int = _MAX_INDUSTY_TAGS) -> list[str]:
    """关键词命中 → industry_tags（行业层 + general:* 通用层）。"""
    low = text.lower()
    hits: list[str] = []
    for tag, words in INDUSTRY_KEYWORDS.items():
        for w in words:
            if w.lower() in low:
                hits.append(tag)
                break
        if len(hits) >= limit:
            break
    return hits


# ─────────────────────────── 品牌 / 注入扫描 ───────────────────────────
def scan_brands(text: str) -> list[str]:
    low = text.lower()
    return [b for b in KNOWN_BRANDS if b.lower() in low]


def count_injection_markers(text: str) -> int:
    return sum(len(p.findall(text)) for p in _INJECTION_MARKERS)


def strip_injection(text: str, marker: str = "〔已过滤疑似指令〕") -> str:
    """把方法论正文里的越权指令行打码（不整段丢弃，保内容可用）。"""
    out = text
    for p in _INJECTION_MARKERS:
        out = p.sub(marker, out)
    return out


# ─────────────────────────── 三通道导入 ───────────────────────────
def import_skill_md(source_id: str, content: str,
                    source: str = "workbuddy") -> dict[str, Any]:
    """SKILL.md → kind=skill 条目（未落库，返回 dict 给路由层）。"""
    meta, body = parse_frontmatter(content)
    body = body[:_MAX_BODY]
    name = str(meta.get("name") or source_id)[:200]
    desc = str(meta.get("description") or "").strip() or body[:200]
    version = str(meta.get("version") or "")[:20]
    license = str(meta.get("license") or "")[:60] or None
    triggers = extract_triggers(f"{desc}\n{body}")
    tags = classify_industry(f"{name} {desc} {body}")
    brands = scan_brands(f"{name} {desc} {content[:4000]}")
    injections = count_injection_markers(body)
    status = "auto_pass" if not brands and not injections else "review"
    return {
        "id": _uuid(), "source": source, "source_id": source_id[:200],
        "kind": "skill", "name": name, "version": version or None,
        "description": desc[:2000], "payload": {
            "frontmatter": {k: v for k, v in meta.items()
                            if isinstance(v, (str, int, float))},
            "body": body,
        },
        "trigger_keywords": triggers, "industry_tags": tags,
        "license": license, "brand_refs": brands,
        "review_status": status,
        "flags": {"injection_markers": injections},
    }


def import_mcp_json(source_id: str, content: str,
                    source: str = "workbuddy") -> dict[str, Any]:
    """mcp.json → kind=connector 蓝图。解析失败会带 error 标记。"""
    try:
        data = json.loads(content)
    except ValueError as exc:
        return {"id": _uuid(), "source": source, "source_id": source_id[:200],
                "kind": "connector", "name": source_id[:200],
                "review_status": "rejected", "payload": {"raw": content[:4000]},
                "flags": {"parse_error": str(exc)}, "industry_tags": [],
                "trigger_keywords": [], "brand_refs": scan_brands(content)}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        servers = data if isinstance(data, dict) else {}
    first_name = next(iter(servers), source_id)
    first = servers.get(first_name) or {}
    name = str(first.get("name") or first_name)[:200]
    desc = str(first.get("description") or
               f"MCP 连接器蓝图：{', '.join(list(servers)[:5])}")[:2000]
    raw = json.dumps(data, ensure_ascii=False)
    brands = scan_brands(f"{name} {desc} {raw[:4000]}")
    tags = classify_industry(f"{name} {desc}")
    return {
        "id": _uuid(), "source": source, "source_id": source_id[:200],
        "kind": "connector", "name": name,
        "description": desc, "payload": {"mcp": data},
        "trigger_keywords": [], "industry_tags": tags,
        "license": None, "brand_refs": brands,
        "review_status": "auto_pass" if not brands else "review",
        "flags": {"servers": list(servers)[:10]},
    }


def import_expert_pack(source_id: str, files: dict[str, str],
                       source: str = "workbuddy") -> dict[str, Any]:
    """experts/<pack>/ 目录 → kind=expert 团队包。

    files: 相对路径 → 内容。约定：
      README.md（团队说明，含成员表/预设 Workflow）、agents/*.md（人设）、
      settings.json（编排配置）、skills/（团队专属技能，原样保留）。
    """
    agents: list[dict[str, str]] = []
    for path, content in sorted(files.items()):
        norm = "/" + path.replace("\\", "/").lstrip("/")
        if "/agents/" in norm and norm.endswith(".md"):
            fname = norm.rsplit("/", 1)[-1][:-3]
            meta, body = parse_frontmatter(content)
            # 职务式命名：name 优先取 frontmatter 的 role/title，其次文件名
            title = str(meta.get("role") or meta.get("title") or fname)[:60]
            agents.append({"slug": fname, "title": title,
                           "name": str(meta.get("name") or title)[:80],
                           "md": content[:6000]})
    readme = files.get("README.md", "")
    settings_raw = files.get("settings.json", "")
    try:
        settings = json.loads(settings_raw) if settings_raw else {}
    except ValueError:
        settings = {}
    name = source_id[:200]
    desc = ""
    for line in readme.split("\n"):
        line = line.strip().lstrip("#* ").strip()
        if len(line) >= 8:
            desc = line[:2000]
            break
    brands = scan_brands(f"{name} {desc} {readme[:3000]}")
    tags = classify_industry(f"{name} {desc} {readme[:2000]}")
    skills_list: list[str] = []
    for path, content in files.items():
        norm = "/" + path.replace("\\", "/").lstrip("/")
        if "/skills/" in norm and norm.endswith("SKILL.md"):
            meta, _ = parse_frontmatter(content)
            skills_list.append(str(meta.get("name") or norm)[:120])
    return {
        "id": _uuid(), "source": source, "source_id": source_id[:200],
        "kind": "expert", "name": name, "description": desc,
        "payload": {"agents": agents[:40], "readme": readme[:8000],
                    "settings": settings, "skills": skills_list[:30]},
        "trigger_keywords": extract_triggers(readme[:3000]),
        "industry_tags": tags, "license": None,
        "brand_refs": brands,
        "review_status": "auto_pass" if not brands else "review",
        "flags": {"agents": len(agents)},
    }


def bulk_import(items: list[dict[str, Any]],
                source: str = "workbuddy") -> list[dict[str, Any]]:
    """批量导入入口：items 元素形如
    {kind: skill|connector|expert, source_id, content?(单文件), files?(多文件)}。"""
    out: list[dict[str, Any]] = []
    for it in items:
        kind = str(it.get("kind") or "skill")
        sid = str(it.get("source_id") or it.get("name") or "").strip()
        if not sid:
            continue
        if kind == "skill":
            out.append(import_skill_md(sid, str(it.get("content") or ""), source))
        elif kind == "connector":
            out.append(import_mcp_json(sid, str(it.get("content") or ""), source))
        elif kind == "expert":
            out.append(import_expert_pack(sid,
                                          dict(it.get("files") or {}), source))
    return out
