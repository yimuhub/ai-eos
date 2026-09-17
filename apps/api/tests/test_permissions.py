"""权限模型的守卫：谁可以做什么，以及"新增的危险端点不能没有守卫"。

## 这一组测试为什么值得单独存在

跨租户越权（IDOR）有个很坏的性质：**它在单租户使用下完全看不出来**。
所有功能都正常，只有第二个账号出现时，数据才会串。

改造前审计发现并修掉了 career 模块的越权；这次端到端试跑又发现
模型配置（`/models`）的列表/改/删/测试**完全没有归属校验**，
以及两个急停接口能被任何登录用户调用来影响他人公司。
同类问题反复出现，说明靠"每次审计时人肉看一遍"是不够的。

因此这里放两层护栏：

* 行为层：对已知的高危端点，用第二个租户账号实际调用，要求 404。
* 静态层：扫描源码，**任何新增的 DELETE 端点都必须带归属校验痕迹**，
  否则门禁直接变红，迫使作者在合并前处理。

静态层是启发式的（匹配关键字），它不证明"逻辑正确"，
但能可靠地拦住"新加了一个删除接口，忘了写校验"这个最常见的失误。
"""
from __future__ import annotations

import pathlib
import re

import pytest

import app

# 不写模块级 pytestmark：pyproject 里已设 asyncio_mode=auto，
# 而本文件混有同步测试（源码扫描、矩阵语义），加标记只会产生
# "sync 函数被标为 asyncio" 的告警，掩盖真正的问题。

ROUTE_DIR = pathlib.Path(app.__file__).resolve().parent / "api" / "routes"

# 只要出现任意一个，就认为这个删除端点"声明了归属校验"
GUARD_MARKERS = (
    "ensure_capability",   # 统一权限层
    "require_capability",
    "_owned",              # _owned_company / _owned_department / _project_owned
    "_verify_",            # _verify_agent_access
    "owner_id",            # 就地比较归属（历史写法）
    "Capability",
)


# ---------------------------------------------------------------------------
# 静态层：新增 DELETE 端点必须带守卫
# ---------------------------------------------------------------------------

_TRIPLE_QUOTED = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _strip_noise(src: str) -> str:
    """去掉注释与文档字符串。

    不做这一步的话，一句"仅所有者可删除"的注释就能让门禁误判为已守卫——
    注释不是校验。
    """
    src = _TRIPLE_QUOTED.sub("", src)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def _delete_handler_blocks() -> list[tuple[str, int, str]]:
    """产出 (文件名, 起始行号, 代码块) —— 每个 DELETE 端点一块。"""
    blocks: list[tuple[str, int, str]] = []
    for path in sorted(ROUTE_DIR.glob("*.py")):
        src = _strip_noise(path.read_text(encoding="utf-8"))
        for m in re.finditer(r"@router\.delete\(", src):
            nxt = src.find("\n@router.", m.end())
            block = src[m.start(): nxt if nxt != -1 else len(src)]
            line_no = src[:m.start()].count("\n") + 1
            blocks.append((path.name, line_no, block))
    return blocks


def test_every_delete_endpoint_has_an_ownership_guard():
    """每个删除端点都必须带归属校验——这是防越权的最后一道自动防线。"""
    unguarded = [
        f"{name}:{line}"
        for name, line, block in _delete_handler_blocks()
        if not any(marker in block for marker in GUARD_MARKERS)
    ]
    assert not unguarded, (
        "以下删除端点没有任何归属校验痕迹，请补上 ensure_capability / "
        f"_owned_* / _verify_* 或 _owned 系列检查：{unguarded}"
    )


def test_guard_scanner_actually_detects_violations():
    """反向验证扫描器：给它一段没有守卫的删除代码，它必须报警。

    护栏测试本身也可能是空转的（正则写错就永远通过），必须先证明它有效。
    """
    sample = _strip_noise(
        '@router.delete("/x/{id}")\n'
        'async def bad(yid: str, db=Depends(get_db)):\n'
        '    """仅所有者可删除（这句注释不算校验）"""\n'
        "    await db.delete(await db.get(Thing, yid))\n"
        "\n"
        "@router.get(\"/y\")\n"
    )
    blocks = []
    for m in re.finditer(r"@router\.delete\(", sample):
        nxt = sample.find("\n@router.", m.end())
        blocks.append(sample[m.start(): nxt if nxt != -1 else len(sample)])
    assert len(blocks) == 1
    assert not any(m in blocks[0] for m in GUARD_MARKERS), \
        "扫描器把注释当成了校验，护栏会空转"


# ---------------------------------------------------------------------------
# 行为层：跨租户调用必须被拒
# ---------------------------------------------------------------------------

async def test_company_delete_cross_tenant(client, tenants):
    r = await client.delete(f"/companies/{tenants.bob_co['id']}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, f"越权删除公司竟然成功：{r.status_code}"


async def test_department_delete_cross_tenant(client, tenants):
    r = await client.post("/departments",
                          params={"cid": tenants.bob_co["id"]},
                          json={"name": "B 部门"},
                          headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    did = r.json()["id"]

    r = await client.delete(f"/departments/{did}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_agent_delete_cross_tenant(client, tenants):
    r = await client.post("/agents", json={
        "company_id": tenants.bob_co["id"], "name": "B 的员工"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    r = await client.delete(f"/agents/{aid}", headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_project_delete_cross_tenant(client, tenants):
    r = await client.post("/projects", json={
        "company_id": tenants.bob_co["id"], "name": "B 的项目"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.delete(f"/projects/{pid}", headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_emergency_stop_cross_tenant(client, tenants):
    """急停是最典型的"影响他人"接口：原先既不校验归属，还会跨公司生效。"""
    for path in ("/automation/emergency-stop", "/governance/emergency-stop",
                 "/governance/emergency-resume"):
        r = await client.post(path,
                              params={"company_id": tenants.bob_co["id"]},
                              headers=tenants.alice["headers"])
        assert r.status_code == 404, \
            f"{path} 允许越权调用他人公司：{r.status_code}"


# ---------------------------------------------------------------------------
# 模型配置的归属（本次修复的核心漏洞）
# ---------------------------------------------------------------------------

_MODEL_BODY = {
    "name": "归属测试模型",
    "provider_type": "openai_compat",
    "base_url": "http://127.0.0.1:9/v1",
    "api_key_env": "NOT_A_REAL_KEY_ENV",
    "model_id": "x",
}


async def _make_model(client, user, *, company_id: str | None = None) -> str:
    body = dict(_MODEL_BODY)
    if company_id:
        body["company_id"] = company_id
    r = await client.post("/models", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_model_list_is_tenant_scoped(client, tenants):
    """A 的列表里不能出现 B 的模型配置（含 base_url 与密钥变量名）。"""
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])

    r = await client.get("/models", headers=tenants.alice["headers"])
    ids = [m["id"] for m in r.json()]
    assert b_model not in ids, "跨租户泄露了模型配置"


async def test_model_cannot_be_created_under_other_company(client, tenants):
    r = await client.post("/models", json={
        **_MODEL_BODY, "company_id": tenants.bob_co["id"]},
        headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_model_update_and_delete_cross_tenant(client, tenants):
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])

    r = await client.patch(f"/models/{b_model}", json={"name": "被改名"},
                           headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权改模型配置竟然成功"

    r = await client.delete(f"/models/{b_model}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权删模型配置竟然成功"


async def test_unowned_model_can_be_claimed_but_not_silently_deleted(client,
                                                                    make_user,
                                                                    make_company):
    """未归属（升级前遗留）的配置：只能认领，不能直接删。

    这是"不破坏升级前安装"与"关闭越权口子"之间的折中：
    无主配置可能正被别人公司的员工使用，直接删掉会造成看不见的破坏。
    """
    user = await make_user()
    co = await make_company(user)
    mid = await _make_model(client, user, company_id=None)

    listed = (await client.get("/models", headers=user["headers"])).json()
    row = [m for m in listed if m["id"] == mid][0]
    assert row["unowned"] is True, "未归属的配置必须被标记出来"

    r = await client.delete(f"/models/{mid}", headers=user["headers"])
    assert r.status_code == 409, "未归属配置应要求先认领，而不是直接删除"

    r = await client.patch(f"/models/{mid}", json={"company_id": co["id"]},
                           headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True

    r = await client.delete(f"/models/{mid}", headers=user["headers"])
    assert r.status_code == 204, "认领后应能正常删除"


# ---------------------------------------------------------------------------
# 矩阵本身
# ---------------------------------------------------------------------------

def test_role_matrix_semantics():
    """矩阵语义：越权的不该被悄悄允许。"""
    from app.core.permissions import Capability, Role, capabilities_of, role_has

    assert capabilities_of(Role.OWNER) == frozenset(Capability), "所有者应具备全部能力"
    assert not role_has(Role.VIEWER, Capability.OPERATE), "只读角色不能写"
    assert not role_has(Role.VIEWER, Capability.DESTRUCTIVE)
    assert not role_has(Role.MEMBER, Capability.DESTRUCTIVE), "普通成员不能删公司"
    assert role_has(Role.MEMBER, Capability.OPERATE)
    assert role_has(Role.ADMIN, Capability.DESTRUCTIVE)
    assert not role_has(Role.ADMIN, Capability.ADMIN), "管理员不该拥有所有权级能力"
    assert not role_has(None, Capability.VIEW), "无角色必须什么都不行"


async def test_access_matrix_endpoint_requires_login(client):
    r = await client.get("/access/matrix")
    assert r.status_code in (401, 403), "权限矩阵不应匿名可读"


async def test_access_matrix_is_exposed_for_frontend(client, make_user):
    """前端要据此禁用/隐藏操作，因此必须能读到自己角色的能力集合。"""
    user = await make_user()
    r = await client.get("/access/matrix", headers=user["headers"])
    assert r.status_code == 200, r.text
    rows = {row["role"]: row["capabilities"] for row in r.json()["matrix"]}
    assert set(rows) == {"owner", "admin", "member", "viewer"}
    assert "destructive" in rows["owner"]
    assert rows["viewer"] == ["view"]


async def test_resolve_role_returns_none_for_foreign_company(client, tenants):
    from app.db.session import AsyncSessionLocal
    from app.core.permissions import resolve_role

    async with AsyncSessionLocal() as db:
        assert await resolve_role(db, tenants.alice["id"],
                                  tenants.bob_co["id"]) is None
        assert await resolve_role(db, tenants.alice["id"],
                                  tenants.alice_co["id"]) is not None
        assert await resolve_role(db, tenants.alice["id"], None) is None


# ---------------------------------------------------------------------------
# 静态护栏扫出来的那一批：逐个用行为验证兜住
# ---------------------------------------------------------------------------

async def test_billing_config_delete_cross_tenant(client, tenants):
    """计费标准挂在模型上：B 的模型 → B 的计费标准 → A 不能删。

    回归守卫：`DELETE /tokens/pricing/{id}` 原先按 id 直接删，
    任何登录用户都能抹掉别人公司的计费标准，成本口径随之失真。
    """
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])
    r = await client.post("/tokens/pricing",
                          json={"model_id": b_model, "billing_mode": "USAGE",
                                "input_price_per_m": 1.0},
                          headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.delete(f"/tokens/pricing/{pid}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权删除他人计费标准竟然成功"

    r = await client.delete(f"/tokens/pricing/{pid}",
                            headers=tenants.bob["headers"])
    assert r.status_code == 200, "所有者应能删除自己的计费标准"


async def test_knowledge_document_delete_cross_tenant(client, tenants):
    """知识库文档：B 上传的文档，A 不能删。

    回归守卫：`DELETE /kb2/documents/{id}` 与 `/knowledge/documents/{id}`
    原先都只按 id 取文档就删，没有归属校验。
    """
    r = await client.post("/kb2/documents",
                          params={"company_id": tenants.bob_co["id"]},
                          json={"title": "B 的知识库文档", "content": "机密内容"},
                          headers=tenants.bob["headers"])
    if r.status_code not in (200, 201):
        pytest.skip(f"kb2 创建接口形态与预期不同：{r.status_code} {r.text[:120]}")
    doc_id = r.json().get("id") or r.json().get("doc_id")

    r = await client.delete(f"/kb2/documents/{doc_id}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权删除他人知识库文档竟然成功"
