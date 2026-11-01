"""B6 质量评测：评分器自测 + 种子任务集结构校验。

这些测试不依赖真实模型、不访问网络、不碰数据库：
* 评分器用内置好/中/差样本自证规则生效；
* 种子集校验保证「60 条任务、5 类各 12 条、每条都有可机检规则」不被改坏。

加固点（防止为了凑数而放松质量）：
* id 必须是「类别前缀 + 短横线小写」且与 category 对应；
* 每条 expected 至少两条可执行规则，避免只有 min_chars 的注水任务；
* 规则取值仍只允许评测器支持的 5 种。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = REPO_ROOT / "tools" / "eval_harness.py"
SEED = REPO_ROOT / "tools" / "eval_tasks.seed.json"

SUPPORTED_RULES = {
    "must_include",
    "forbidden",
    "min_chars",
    "min_sections",
    "citation_required",
}

EXPECTED_CATEGORIES = {"调研汇报", "方案计划", "数据分析", "文案材料", "流程清单"}

# 类别 → id 前缀，保证 id 可读、可分组、可全局唯一
CATEGORY_PREFIX = {
    "调研汇报": "research",
    "方案计划": "plan",
    "数据分析": "data",
    "文案材料": "copy",
    "流程清单": "checklist",
}

TASKS_PER_CATEGORY = 12
TOTAL_TASKS = TASKS_PER_CATEGORY * len(EXPECTED_CATEGORIES)

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_scorer_self_test_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "--self-test"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "评分器自测失败：\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "自测通过" in proc.stdout, proc.stdout


def test_seed_tasks_are_complete_and_machine_checkable():
    tasks = json.loads(SEED.read_text(encoding="utf-8"))
    assert isinstance(tasks, list), "种子集必须是 JSON 数组"
    assert len(tasks) == TOTAL_TASKS, (
        f"种子集应有 {TOTAL_TASKS} 条任务，实际 {len(tasks)}"
    )

    ids = [t.get("id") for t in tasks]
    assert all(isinstance(tid, str) and tid.strip() for tid in ids), "每条任务都要有非空 id"
    assert len(set(ids)) == len(ids), f"任务 id 重复：{ids}"

    categories = Counter(t.get("category") for t in tasks)
    assert set(categories) == EXPECTED_CATEGORIES, (
        f"任务类别应为 {sorted(EXPECTED_CATEGORIES)}，实际 {sorted(categories)}"
    )
    assert len(categories) == 5, f"应覆盖 5 类任务，实际 {len(categories)}：{categories}"
    assert all(count == TASKS_PER_CATEGORY for count in categories.values()), (
        f"每类任务应各 {TASKS_PER_CATEGORY} 条，实际：{categories}"
    )

    for task in tasks:
        tid = task.get("id")
        category = task.get("category")
        for field in ("title", "prompt", "category"):
            value = task.get(field)
            assert isinstance(value, str) and value.strip(), f"{tid} 缺少非空 {field}"

        assert isinstance(tid, str) and ID_PATTERN.match(tid), (
            f"id 必须是短横线小写命名（如 research-ai-office-market）：{tid!r}"
        )
        prefix = CATEGORY_PREFIX.get(category)
        assert prefix, f"{tid} 的类别不在约定范围内：{category!r}"
        assert tid.startswith(f"{prefix}-"), (
            f"{tid} 的 id 前缀应为「{prefix}-」，与类别「{category}」一致"
        )

        expected = task.get("expected")
        assert isinstance(expected, dict) and expected, f"{tid} 缺少 expected 规则"
        unknown = set(expected) - SUPPORTED_RULES
        assert not unknown, f"{tid} 含不支持的规则：{sorted(unknown)}"

        checks = 0
        for key in ("must_include", "forbidden"):
            if key in expected:
                items = expected[key]
                assert isinstance(items, list) and items, f"{tid}.{key} 必须是非空数组"
                assert all(isinstance(x, str) and x.strip() for x in items), (
                    f"{tid}.{key} 必须全部是非空字符串"
                )
                assert len(set(items)) == len(items), f"{tid}.{key} 不应有重复项"
                checks += 1
        for key in ("min_chars", "min_sections"):
            if key in expected:
                value = expected[key]
                assert isinstance(value, int) and not isinstance(value, bool) and value > 0, (
                    f"{tid}.{key} 必须是正整数"
                )
                checks += 1
        if "citation_required" in expected:
            assert isinstance(expected["citation_required"], bool), (
                f"{tid}.citation_required 必须是布尔值"
            )
            checks += 1
        assert checks >= 2, f"{tid} 至少要有两条可机检规则，实际 {checks} 条"
