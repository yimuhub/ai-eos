"""SLO 告警规则的守卫。

## 为什么用文本解析而不是 PyYAML

运行环境不保证装了 PyYAML，而"告警引用了不存在的指标"这类错误必须在
CI 就拦住。规则文件的写法受本仓库控制（固定缩进、每条规则一个
``alert:`` 块），用少量标准库文本解析即可钉住关键不变量：

* 规则文件存在且是 ``groups:`` / ``rules:`` 结构；
* 表达式里出现的每个指标都能在 ``_ALL_METRICS`` 找到同名注册项
  （直方图的 ``_bucket`` / ``_sum`` / ``_count`` 视为同一指标的派生序列）；
* 至少 5 条告警，每条都有 expr / for / labels.severity；
* 严重级别只允许 P1/P2（或 critical/warning），且 P1、P2 都被用到。

宁可测试啰嗦，也不要把"告警写错了指标名、上线后静默不触发"放进生产。
"""
from __future__ import annotations

import pathlib
import re

from app.core import metrics

# apps/api/tests/test_slo_alerts.py -> 仓库根目录
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
ALERTS_PATH = REPO_ROOT / "deploy" / "prometheus" / "alerts.yml"

SEVERITY_VALUES = {"P1", "P2", "critical", "warning"}
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _text() -> str:
    assert ALERTS_PATH.is_file(), f"缺少告警规则文件：{ALERTS_PATH}"
    return ALERTS_PATH.read_text(encoding="utf-8")


def _rule_blocks(text: str) -> list[str]:
    """按 ``alert:`` 行切块；每块从本行到下一个 alert 行之前。"""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if re.match(r"\s*-\s+alert:\s*\S", line):
            if current is not None:
                blocks.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def _labels_section(block: str) -> list[str]:
    """取出 ``labels:`` 下缩进更深的行（遇到同级或更浅即结束）。"""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "labels:":
            continue
        base_indent = len(line) - len(line.lstrip())
        section: list[str] = []
        for following in lines[index + 1:]:
            if not following.strip():
                continue
            indent = len(following) - len(following.lstrip())
            if indent <= base_indent:
                break
            section.append(following.strip())
        return section
    return []


def _known_metrics() -> set[str]:
    return {metric.name for metric in metrics._ALL_METRICS}


def _normalize(name: str, known: set[str]) -> str:
    """直方图派生序列归一：仅当去掉后缀后确有注册项才归一。"""
    for suffix in HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if base in known:
                return base
    return name


def test_alert_file_exists_and_has_groups_and_rules():
    text = _text()
    assert "groups:" in text
    assert "rules:" in text


def test_at_least_five_alerts():
    blocks = _rule_blocks(_text())
    assert len(blocks) >= 5, f"告警条数不足：{len(blocks)}"


def test_referenced_metrics_are_all_registered():
    """表达式/注释里的每个指标名都必须真实存在，不能编造。"""
    text = _text()
    known = _known_metrics()
    referenced = set(re.findall(r"\baieos_[a-z_]+", text))
    assert referenced, "规则文件里没有引用任何 aieos_ 指标"

    unknown = sorted(
        name for name in referenced if _normalize(name, known) not in known)
    assert not unknown, f"告警引用了未注册的指标：{unknown}"


def test_every_rule_has_expr_for_and_severity_label():
    blocks = _rule_blocks(_text())
    assert blocks, "没有解析到任何 alert 块"

    for block in blocks:
        name = re.search(r"alert:\s*(\S+)", block).group(1)
        expr = re.search(r"(?m)^\s+expr:\s*(\S.*)$", block)
        assert expr is not None and expr.group(1).strip(), \
            f"{name} 缺少 expr"

        window = re.search(r"(?m)^\s+for:\s*(\d+[smhd])\s*$", block)
        assert window is not None, f"{name} 缺少合法的 for（如 10m）"

        labels = _labels_section(block)
        severity = [item for item in labels
                    if item.startswith("severity:")]
        assert severity, f"{name} 的 labels 里缺少 severity：{labels}"
        value = severity[0].split(":", 1)[1].strip()
        assert value in SEVERITY_VALUES, \
            f"{name} 的 severity 非法：{value!r}"

        assert "annotations:" in block, f"{name} 缺少 annotations"
        assert re.search(r"(?m)^\s+summary:\s*\S", block), \
            f"{name} 缺少 annotations.summary"
        assert re.search(r"(?m)^\s+description:\s*\S", block), \
            f"{name} 缺少 annotations.description"


def test_severity_grades_are_used_and_alert_names_unique():
    blocks = _rule_blocks(_text())
    names = [re.search(r"alert:\s*(\S+)", block).group(1)
             for block in blocks]
    assert len(names) == len(set(names)), f"告警名重复：{names}"

    severities = set()
    for block in blocks:
        labels = _labels_section(block)
        for item in labels:
            if item.startswith("severity:"):
                severities.add(item.split(":", 1)[1].strip())
    # 立即处理与工作时间处理两档都要有，否则分级形同虚设。
    assert "P1" in severities, f"缺少 P1 告警：{severities}"
    assert "P2" in severities, f"缺少 P2 告警：{severities}"
