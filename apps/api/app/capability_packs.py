"""Capability Packs: 把 Hermes/Codex 级别的智能体能力赋予普通数字员工。

创建/编辑 Agent 时选择能力包，运行时 agent_runtime 根据能力包：
1. 注入对应工具的系统提示（模型知道自己会什么）
2. 执行链路挂载真实引擎（浏览器/电脑操作/记忆/自动化调度）

存储位置：agents.memory_config JSON 的 "capabilities" 键（无需迁移表结构）。
"""
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 能力定义（单一事实来源，前端从这里拉取）
# ---------------------------------------------------------------------------

CAPABILITY_PACKS: dict[str, dict] = {
    "computer_use": {
        "label": "🖥 电脑操作",
        "description": "可以操作计算机完成实际工作：打开应用、读写文件、执行命令",
        "risk": "high",
        "tools": ["computer_screenshot", "computer_click", "computer_type_text",
                  "computer_run_shell", "computer_run_write", "computer_run_app"],
        "system_prompt": (
            "你具备【电脑操作】能力：可以通过 computer-agent 工具集截图屏幕、点击、"
            "输入文字、执行终端命令来完成需要真实计算机操作的任务。"
            "遇到需要操作软件、文件系统的步骤时，主动使用这些工具而不是空想结果。"
        ),
        "engines": ["computer_agent"],
    },
    "web_browsing": {
        "label": "🌐 网页浏览",
        "description": "可以访问网页、搜索资料、抓取内容（含安全域名校验）",
        "risk": "medium",
        "tools": ["web_search", "web_fetch"],
        "system_prompt": (
            "你具备【网页浏览】能力：可以通过 browser-agent 工具访问 URL、"
            "获取网页文本内容、进行网络调研。写报告或查资料时先去真实网页取证，"
            "标注信息来源 URL，不要编造。"
        ),
        "engines": ["browser_agent"],
    },
    "persistent_memory": {
        "label": "🧠 持久记忆",
        "description": "跨任务记住经验和偏好，越用越懂你的业务",
        "risk": "low",
        "tools": ["memory_write", "memory_recall"],
        "system_prompt": (
            "你具备【持久记忆】能力：你的重要发现、用户偏好、踩过的坑都会写入长期记忆，"
            "下次任务自动带上。回复时如果记忆里有相关历史经验，明确引用它。"
        ),
        "engines": ["memory_engine"],
        "defaults": {"memory_types": ["EPISODIC", "SEMANTIC", "PREFERENCE"], "ttl_days": 90},
    },
    "self_learning": {
        "label": "📈 自我学习",
        "description": "每次任务后自动复盘，沉淀技能与改进建议",
        "risk": "low",
        "tools": [],
        "system_prompt": (
            "你具备【自我学习】能力：每完成一次任务，系统会自动复盘你的表现，"
            "产出经验教训和技能成长记录。主动在回复末尾总结'本次学到了什么'。"
        ),
        "engines": ["reflection_engine", "learning_engine"],
    },
    "scheduled_work": {
        "label": "⏰ 周期性工作",
        "description": "按计划自动执行任务（如每天晨报、每周总结），无需人工触发",
        "risk": "medium",
        "tools": [],
        "system_prompt": (
            "你具备【周期性工作】能力：你可以被自动化调度器定期唤醒执行任务，"
            "也可以创建子自动化流程。输出要适合无人值守场景：结论先行、可留档。"
        ),
        "engines": ["automation_engine", "scheduler_engine"],
    },
}

# Agent 底座（执行时真正驱动它的是什么）
AGENT_BACKENDS: dict[str, dict] = {
    "native_llm": {
        "label": "原生大模型",
        "description": "直接由配置的大模型 API 驱动（当前默认）",
    },
    "hermes": {
        "label": "Hermes Agent",
        "description": "由本机 Hermes Agent 驱动：完整工具链、技能系统、文件操作",
        "adapter": "hermes_cli",
    },
    "codex": {
        "label": "Codex Agent",
        "description": "由 OpenAI Codex CLI 驱动：强代码编写、GitHub 操作",
        "adapter": "codex_cli",
    },
}

# 专家↔技能包推荐映射（V5.0.2）：按专家部门预配"上岗即开箱"的技能包。
# 从专家库招聘时若未手选技能包，后端自动按此推荐填入 —— 专家与技能包从此有对应关系。
DEPT_CAPABILITY_PACKS: dict[str, list[str]] = {
    "company": ["persistent_memory", "self_learning"],
    "engineering": ["computer_use", "persistent_memory", "self_learning"],
    "design": ["web_browsing", "persistent_memory"],
    "marketing": ["web_browsing", "persistent_memory"],
    "paid-media": ["web_browsing", "scheduled_work"],
    "sales": ["web_browsing", "persistent_memory", "self_learning"],
    "finance": ["persistent_memory", "scheduled_work"],
    "hr": ["persistent_memory", "scheduled_work"],
    "legal": ["persistent_memory", "web_browsing"],
    "supply-chain": ["persistent_memory", "scheduled_work"],
    "product": ["persistent_memory", "self_learning"],
    "project-management": ["persistent_memory", "scheduled_work"],
    "testing": ["computer_use", "self_learning"],
    "support": ["persistent_memory", "scheduled_work"],
    "specialized": ["persistent_memory"],
    "spatial-computing": ["computer_use", "self_learning"],
    "game-development": ["computer_use", "self_learning"],
    "academic": ["web_browsing", "persistent_memory"],
    "gis": ["web_browsing", "persistent_memory"],
    "security": ["computer_use", "persistent_memory"],
    "strategy": ["persistent_memory", "self_learning"],
    "integrations": ["computer_use", "persistent_memory"],
}

DEFAULT_CAPABILITY_PACKS: list[str] = ["persistent_memory"]


def recommended_packs_for_dept(department: str | None) -> list[str]:
    """部门 → 推荐技能包（未知部门给通用默认，保证推荐永远非空）。"""
    if department in DEPT_CAPABILITY_PACKS:
        return list(DEPT_CAPABILITY_PACKS[department])
    return list(DEFAULT_CAPABILITY_PACKS)



@dataclass
class ResolvedCapabilities:
    """运行时解析后的能力集合。"""
    packs: list[str]
    backend: str
    tools: list[str]
    prompt_segments: list[str]
    engines: list[str]
    config: dict


def resolve_capabilities(memory_config: dict | None) -> ResolvedCapabilities:
    """从 agents.memory_config 解析能力配置（容错：脏数据返回默认值）。"""
    cfg = (memory_config or {}) if isinstance(memory_config, dict) else {}
    packs = [p for p in cfg.get("capabilities", []) if p in CAPABILITY_PACKS]
    backend = cfg.get("backend") if cfg.get("backend") in AGENT_BACKENDS else "native_llm"

    tools: list[str] = []
    prompts: list[str] = []
    engines: list[str] = []
    merged_cfg: dict = {}
    for p in packs:
        spec = CAPABILITY_PACKS[p]
        tools.extend(spec["tools"])
        prompts.append(spec["system_prompt"])
        engines.extend(spec["engines"])
        if spec.get("defaults"):
            merged_cfg.setdefault(p, {}).update(spec["defaults"])

    return ResolvedCapabilities(
        packs=packs,
        backend=backend,
        tools=sorted(set(tools)),
        prompt_segments=prompts,
        engines=sorted(set(engines)),
        config=merged_cfg,
    )


def build_capability_system_prompt(memory_config: dict | None) -> str:
    """把能力说明拼成 system prompt 片段（无能力时返回空串）。"""
    rc = resolve_capabilities(memory_config)
    if not rc.prompt_segments:
        return ""
    parts = [
        "## 你的智能体能力",
        *rc.prompt_segments,
        f"\n可用工具清单: {', '.join(rc.tools) if rc.tools else '（基础对话）'}",
    ]
    return "\n".join(parts)


def validate_capabilities(capabilities: list, backend: str | None) -> tuple[list[str], str]:
    """校验前端传来的能力列表，返回 (合法packs, 合法backend)。"""
    valid_packs = [p for p in (capabilities or []) if p in CAPABILITY_PACKS]
    valid_backend = backend if backend in AGENT_BACKENDS else "native_llm"
    return valid_packs, valid_backend
