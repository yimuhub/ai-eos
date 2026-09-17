"""接口契约门禁：前端每个 `api.*` 调用都必须能在后端找到对应路由。

## 为什么它值得一个测试

改造前有 11 处前端调用对不上后端（404/405），最典型的是人工介入页：
`POST /intervention/requests/{id}/APPROVE` 后端根本没有这个路径，
但因为前端**无视响应无条件提示"已批准"**，用户点完以为成功了。

这类缺陷有个共同特征：**后端能启动、前端能编译、页面能打开**，
所以没有任何一道自动化关卡会拦它。契约校验补上了这一关。

## 实现说明

复用 `tools/check_api_contract.py`（CLI 与测试共用同一份逻辑，
避免"测试里过、命令行里不过"的分裂）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def test_frontend_api_calls_match_backend_routes():
    check_api_contract = pytest.importorskip(
        "check_api_contract", reason="契约校验工具不可用")

    broken, dynamic = check_api_contract.check()

    if broken:
        lines = [f"发现 {len(broken)} 处接口契约不匹配："]
        for file, line, method, raw, methods in broken:
            if methods:
                lines.append(f"  [405 方法不匹配] {file}:{line}  "
                             f"前端 {method} {raw}；后端仅有 {methods}")
            else:
                lines.append(f"  [404 路径不存在] {file}:{line}  "
                             f"前端 {method} {raw}")
        pytest.fail("\n".join(lines))

    # 动态拼接的调用列出来供人工复核，但不作为失败条件
    if dynamic:
        print(f"\n[提示] {len(dynamic)} 处动态拼接调用经宽松匹配通过，建议复核：")
        for file, line, method, raw in dynamic:
            print(f"  {file}:{line}  {method} {raw}")


def test_contract_tool_scans_a_meaningful_number_of_calls():
    """防止"工具坏了导致 0 处不匹配"这种假绿。

    如果扫描到的调用数骤降（比如前端目录改名、正则失效），
    上面那条用例会因为"没发现任何问题"而通过——那是假的安全感。
    """
    check_api_contract = pytest.importorskip("check_api_contract")

    routes = check_api_contract.backend_routes()
    calls = check_api_contract.frontend_calls()

    assert len(routes) > 100, f"后端只解析出 {len(routes)} 个路径，OpenAPI 可能未完整生成"
    assert len(calls) > 100, f"只扫到 {len(calls)} 处前端调用，扫描逻辑可能失效"
