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

    broken, dynamic, _n_web, _n_mobile = check_api_contract.check()

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
    移动端单独一条：它只有 10 处调用，一次正则失效就静默归零。
    """
    check_api_contract = pytest.importorskip("check_api_contract")

    routes = check_api_contract.backend_routes()
    calls = check_api_contract.frontend_calls()
    mobile = check_api_contract.mobile_calls()

    assert len(routes) > 100, f"后端只解析出 {len(routes)} 个路径，OpenAPI 可能未完整生成"
    assert len(calls) > 100, f"只扫到 {len(calls)} 处前端调用，扫描逻辑可能失效"
    assert len(mobile) >= 10, (
        f"只扫到 {len(mobile)} 处移动端 call() 调用（实测 10 处），"
        "mobile/lib/api.ts 的解析逻辑可能失效"
    )
    # 移动端全部集中在一个文件里；扫到别的目录说明过滤条件漂了
    assert {f for f, _l, _m, _p in mobile} == {"mobile/lib/api.ts"}, mobile


def test_frontend_has_no_raw_fetch_outside_api_layer():
    """前端不得再出现裸 fetch / 硬编码后端地址 / 手工拼 token。

    ## 为什么值得一条测试

    此前 30+ 个页面各自 `fetch(process.env.NEXT_PUBLIC_API_URL + ...)`，
    并且自己 `localStorage.getItem("aieos_token")` 拼 Authorization：
    - 没有超时：后端卡住页面就永久转圈；
    - 没有错误规范化：422 时把 `[object Object]` 显示给用户；
    - 绕过统一层就意味着**同时绕过契约校验**——`PUT /external-agents/{id}/ENABLED`
      这种后端根本不存在的调用，靠裸 fetch 谁也发现不了。

    统一到 `lib/api.ts` 后这些能力才生效，这条测试防止倒退。
    """
    web = pathlib.Path(__file__).resolve().parents[3] / "apps" / "web"
    offenders: list[str] = []

    for f in list(web.rglob("*.tsx")) + list(web.rglob("*.ts")):
        s = str(f)
        if "node_modules" in s or "/.next/" in s or s.endswith("lib/api.ts"):
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(src.splitlines(), 1):
            # 注释与文档里出现这些字符串是正常的（本测试自己的说明就有）
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            if "fetch(" in line:
                offenders.append(f"{f.relative_to(web)}:{i} 裸 fetch：{line.strip()[:90]}")
            elif "NEXT_PUBLIC_API_URL" in line:
                offenders.append(f"{f.relative_to(web)}:{i} 硬编码后端地址：{line.strip()[:90]}")
            elif "aieos_token" in line:
                offenders.append(f"{f.relative_to(web)}:{i} 手工取 token：{line.strip()[:90]}")

    assert not offenders, (
        "前端必须统一走 lib/api.ts（超时 / 错误规范化 / 契约校验都挂在那里）：\n  "
        + "\n  ".join(offenders)
    )
