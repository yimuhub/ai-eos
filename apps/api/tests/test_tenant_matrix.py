"""A1「自动权限矩阵」：对带资源 id 的端点批量验证租户隔离。

每个参数化用例只做一件事：

1. **属主正向对照**——alice 调同一端点必须得到 ``ok_status``。
   没有这条，「bob 全是 404」可能只是因为端点被改坏了（假绿）。
2. **外部租户拒绝**——bob 拿着 alice 的资源 id 调同一端点，必须是
   404/403，且响应体里不能出现 alice 的公司 id 或资源内容标记。
3. **写端点未被改动**——bob 尝试 PATCH/DELETE 后，alice 再读同一资源，
   内容必须与攻击前完全一致。

所有调用走真实 ASGI + 真实鉴权（conftest 的 ``client`` / ``tenants``），
不 mock 依赖、不碰真实库。资源族与端点清单见
``tests/tenant_matrix_registry.py``。

失败信息统一打印「族 / 方法 / 路径 / 攻击变体 / 预期 / 实际 / 响应片段」，
参数化天然把每个 (族, 端点) 隔离开——一个族红不会掩盖其他族。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.tenant_matrix_registry import (
    RESOURCE_SPECS,
    SKIPPED_SURFACES,
    EndpointSpec,
    MatrixCtx,
    ResourceSpec,
    cases,
)

# 资源内容无关的易变字段：读取同一资源两次时不应参与比较
_VOLATILE_KEYS = frozenset({"access_count", "created_at", "updated_at", "last_at"})

_CASES = cases()
_CASE_IDS = [f"{spec.name}::{ep.method} {ep.path_template}" for spec, ep in _CASES]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _fmt_str(template: str, values: dict[str, Any]) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:  # 占位符没有对应值 = 注册表 bug，直接报清楚
        raise AssertionError(
            f"[matrix] 路径模板 {template!r} 缺少占位符值 {exc}；"
            f"当前可用键={sorted(values)}") from exc


def _fmt_obj(obj: Any, values: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return _fmt_str(obj, values)
    if isinstance(obj, dict):
        return {k: _fmt_obj(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fmt_obj(v, values) for v in obj]
    return obj


def _normalize(obj: Any) -> Any:
    """递归去掉易变字段；列表保持顺序（同一资源两次读取顺序稳定）。"""
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def _pick_resource(payload: Any, resource_id: str) -> Any:
    """列表型读视图只比较「本资源那一行」，避免同族其他行干扰。"""
    if isinstance(payload, list):
        return [
            row for row in payload
            if isinstance(row, dict) and str(row.get("id")) == str(resource_id)
        ]
    return payload


def _failure(spec: ResourceSpec, ep: EndpointSpec, variant: str,
             expected: str, resp) -> str:
    return (
        "\n权限矩阵失败\n"
        f"  族={spec.name}\n"
        f"  方法={ep.method}\n"
        f"  路径={resp.request.url}\n"
        f"  攻击变体={variant}\n"
        f"  预期={expected}\n"
        f"  实际={resp.status_code}\n"
        f"  响应片段={resp.text[:240]!r}"
    )


def _owner_failure(spec: ResourceSpec, ep: EndpointSpec, resp) -> str:
    return (
        "\n属主正向对照失败（防止「全都 404」的假绿）\n"
        f"  族={spec.name}\n"
        f"  方法={ep.method}\n"
        f"  路径={resp.request.url}\n"
        f"  预期={ep.ok_status}\n"
        f"  实际={resp.status_code}\n"
        f"  响应片段={resp.text[:240]!r}"
    )


async def _call(ctx: MatrixCtx, ep: EndpointSpec, path: str, *,
                headers: dict, params: dict | None = None,
                body: Any = None, actor: str = "owner", variant: str = "default"):
    kwargs: dict[str, Any] = {"headers": headers}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    try:
        return await ctx.client.request(ep.method, path, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 把请求异常也包装成可读失败
        raise AssertionError(
            f"[matrix] 请求抛异常：{ep.method} {path} "
            f"actor={actor} variant={variant}: {type(exc).__name__}: {exc}")


async def _observe(ctx: MatrixCtx, spec: ResourceSpec, ep: EndpointSpec,
                   values: dict[str, Any]) -> tuple[int, Any, str]:
    assert ep.observe_path, f"[matrix] {spec.name} 的写端点缺少 observe_path"
    path = _fmt_str(ep.observe_path, values)
    resp = await ctx.client.get(path, headers=ctx.owner_headers)
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text
    return resp.status_code, payload, resp.text


# ---------------------------------------------------------------------------
# 注册表自检
# ---------------------------------------------------------------------------
def test_registry_has_enough_families_and_endpoints():
    usable = [s for s in RESOURCE_SPECS if s.endpoints and s.skip_reason is None]
    assert len(usable) >= 12, f"可用资源族不足 12：{len(usable)}"
    for spec in RESOURCE_SPECS:
        assert 1 <= len(spec.endpoints) <= 3, \
            f"{spec.name} 应只收录 1~3 个按 id 端点，实际 {len(spec.endpoints)}"
        for ep in spec.endpoints:
            assert ep.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, ep
            assert "{id}" in ep.path_template or spec.name in {
                "companies", "agent_memories"}, \
                f"{spec.name} 的 {ep.path_template} 不像按资源 id 的端点"
            if ep.method != "GET":
                assert ep.observe_path, f"{spec.name} {ep.method} 缺少改动对照"


def test_registry_covers_attacker_company_and_victim_company_variants():
    query_endpoints = [(s, e) for s, e in _CASES if e.company_query]
    assert len(query_endpoints) >= 3, (
        "需要至少 3 个 company_id 查询参数端点，才能同时覆盖"
        "「bob 用自己的公司」「bob 用 alice 的公司」两种攻击变体")
    for _, ep in query_endpoints:
        haystack = (
            ep.path_template
            + json.dumps(ep.query or {}, ensure_ascii=False)
            + json.dumps(ep.json or {}, ensure_ascii=False)
        )
        assert "{id}" in haystack or "{agent_id}" in haystack, (
            f"{ep.method} {ep.path_template} 的 company_id 变体没有指向具体资源")


def test_skipped_surfaces_have_reasons():
    for name, reason in SKIPPED_SURFACES:
        assert name and reason, "跳过项必须写清原因"


# ---------------------------------------------------------------------------
# 矩阵主用例
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spec,endpoint", _CASES, ids=_CASE_IDS)
async def test_foreign_tenant_denied_and_owner_allowed(client, tenants, spec, endpoint):
    ctx = MatrixCtx(
        client=client,
        owner=tenants.alice,
        owner_company=tenants.alice_co,
        attacker=tenants.bob,
        attacker_company=tenants.bob_co,
    )
    alice_co = ctx.owner_company_id

    # 1) 每个用例独立创建资源（真实接口，owner 身份）
    resource_id, created_body = await spec.create(ctx)
    assert resource_id, f"[registry] {spec.name}.create 未返回资源 id"

    marker = str(created_body.get("_marker") or "")
    values: dict[str, Any] = {
        **{k: v for k, v in created_body.items()
           if isinstance(v, str) and not k.startswith("_")},
        "id": str(resource_id),
        "company_id": alice_co,
    }

    path = _fmt_str(endpoint.path_template, values)
    base_query = _fmt_obj(endpoint.query or {}, values)
    body = _fmt_obj(endpoint.json, values) if endpoint.json is not None else None

    # 2) 写端点：先记录属主视角的基线（bob 未动手前）
    before: tuple[int, Any, str] | None = None
    compare_id = str(created_body.get("memory_id") or resource_id)
    if endpoint.method != "GET":
        before = await _observe(ctx, spec, endpoint, values)
        assert before[0] == 200, (
            f"[matrix] 属主在攻击前读不到自己的 {spec.name} 资源："
            f"{before[0]} {before[2][:200]!r}")

    # 3) 外部租户攻击：变体 = 不带 query / bob 自己的公司 / alice 的公司
    if endpoint.company_query:
        variants = [
            ("bob_own_company_id", ctx.attacker_company_id),
            ("alice_company_id", alice_co),
        ]
    else:
        variants = [("default", None)]

    for variant_name, company_id in variants:
        attack_query = dict(base_query)
        if company_id is not None:
            attack_query["company_id"] = company_id
        resp = await _call(
            ctx, endpoint, path,
            headers=ctx.attacker_headers,
            params=attack_query or None,
            body=body,
            actor="bob",
            variant=variant_name,
        )
        assert resp.status_code in (403, 404), _failure(
            spec, endpoint, variant_name,
            "404 或 403（外部租户不得 2xx，否则就是越权）", resp)
        assert alice_co not in resp.text, _failure(
            spec, endpoint, variant_name, "响应体不得包含 alice 的公司 id", resp)
        if marker:
            assert marker not in resp.text, _failure(
                spec, endpoint, variant_name, "响应体不得包含 alice 的资源内容标记", resp)

    # 4) 写端点：bob 被拒之后，alice 的资源必须原封不动
    if before is not None:
        after = await _observe(ctx, spec, endpoint, values)
        assert after[0] == 200, (
            f"\n越权写导致属主资源不可读\n  族={spec.name}\n"
            f"  方法={endpoint.method}\n  路径={path}\n"
            f"  攻击后读取状态={after[0]}\n  响应={after[2][:240]!r}")
        before_payload = _pick_resource(_normalize(before[1]), compare_id)
        after_payload = _pick_resource(_normalize(after[1]), compare_id)
        assert before_payload == after_payload, (
            "\n越权写改动了属主资源\n"
            f"  族={spec.name}\n"
            f"  方法={endpoint.method}\n"
            f"  路径={path}\n"
            f"  攻击前={json.dumps(before_payload, ensure_ascii=False)[:300]}\n"
            f"  攻击后={json.dumps(after_payload, ensure_ascii=False)[:300]}")

    # 5) 属主正向对照：放在攻击之后，DELETE 才不会把攻击目标提前删掉
    owner_query = dict(base_query)
    if endpoint.company_query:
        owner_query["company_id"] = alice_co
    owner_resp = await _call(
        ctx, endpoint, path,
        headers=ctx.owner_headers,
        params=owner_query or None,
        body=body,
        actor="alice",
        variant="owner",
    )
    assert owner_resp.status_code == endpoint.ok_status, \
        _owner_failure(spec, endpoint, owner_resp)
    if endpoint.expect_marker and marker:
        assert marker in owner_resp.text, (
            "\n属主读到了响应但没有资源内容（端点可能返回空壳）\n"
            f"  族={spec.name}\n  方法={endpoint.method}\n"
            f"  路径={owner_resp.request.url}\n  响应片段={owner_resp.text[:240]!r}")
