"""自定义断言规则的白名单注册表。

`assert.custom: "rule_id"` 只查这张表。**不允许**工作流写脚本路径、
表达式或任意代码——多租户下那等于把服务器执行权限交给工作流作者。

新增一条规则的方式：在模块里定义 `fn(text, files, ctx) -> bool|Check`，
然后在下面注册。注册即可用，不需要改求值器。
"""
from __future__ import annotations

from typing import Any, Callable

CUSTOM_RULES: dict[str, Callable[[str, list, dict], Any]] = {}
