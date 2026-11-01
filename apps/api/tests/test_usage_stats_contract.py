"""用量统计与能力包安装的形状契约。

这四条都来自改造时的**实测**（对着真实返回值逐字段比对），不是设想出来的
边界。它们的共同点是：接口 200、页面不报错、数据却是错的或干脆是空的——
"能打开"这一关拦不住任何一个。

1. `GET /tokens/pricing/models` 的 `provider_type` 必须是枚举值
   `openai_compat`，不能是 `"ModelProviderType.OPENAI_COMPAT"`（全站另有 5 处
   都用 `.value`，这里曾是唯一的例外，前端下拉框直接把枚举名显示给用户）；
2. `GET /tokens/stats/by-task` 的 `name` 必须是任务标题——`Task` 的显示列叫
   `title` 不叫 `name`，反查时只看 `name` 会让这一整列都是 UUID；
3. `GET /tokens/stats/by-scenario` 与其余 5 个维度一样带 `name`（中文标签），
   未登记的场景码原样回填 key；
4. `GET /governance/installations` 是治理中心「已安装」面板的唯一数据源：
   只列本公司、且回滚过的不再计。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.db.session import AsyncSessionLocal
from app.models.agent import Model, ModelProviderType
from app.models.p3_governance import MarketplacePackage, PackageInstallation
from app.models.task import Task
from app.models.token_usage import TokenUsage


def _id() -> str:
    return str(uuid.uuid4())


def _recent(days: int = 0) -> datetime:
    """与端点同口径的 naive UTC（`_now_utc()` 会抹掉 tzinfo）。"""
    return datetime.utcnow() - timedelta(days=days)


async def _usage(company_id: str, **fields) -> None:
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(
            id=_id(), company_id=company_id,
            input_tokens=100, output_tokens=40, total_tokens=140,
            input_cost=0.14, output_cost=0.0, total_cost=0.14,
            created_at=_recent(), **fields))
        await s.commit()


async def test_pricing_model_options_use_provider_enum_value(client, tenants):
    co = tenants.alice_co["id"]
    name = f"计费下拉-{co[:8]}"
    async with AsyncSessionLocal() as s:
        s.add(Model(id=_id(), company_id=co, name=name,
                    provider_type=ModelProviderType.OPENAI_COMPAT,
                    model_id="agnes-2.5-flash", config={}))
        await s.commit()

    r = await client.get("/tokens/pricing/models", headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    row = next(m for m in r.json() if m["name"] == name)
    assert row["provider_type"] == "openai_compat", row


async def test_by_task_name_resolves_to_task_title(client, tenants):
    co = tenants.alice_co["id"]
    task_id = _id()
    title = "把季度复盘改成图表"
    async with AsyncSessionLocal() as s:
        s.add(Task(id=task_id, company_id=co, title=title,
                   created_by=tenants.alice["id"]))
        await s.commit()
    await _usage(co, task_id=task_id, scenario="TASK")

    r = await client.get(f"/tokens/stats/by-task?company_id={co}&days=30",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["key"] == task_id)
    assert row["name"] == title, f"任务维度又退回 UUID：{row}"


async def test_by_scenario_rows_carry_readable_name(client, tenants):
    co = tenants.alice_co["id"]
    await _usage(co, scenario="EXPERT_SEARCH")
    await _usage(co, scenario="NOT_A_REGISTERED_SCENARIO")

    r = await client.get(f"/tokens/stats/by-scenario?company_id={co}&days=30",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = {x["key"]: x for x in r.json()}
    # experts.py 真实写入的场景码曾不在标签表里，只能显示英文原码
    assert rows["EXPERT_SEARCH"]["name"] == "找专家"
    # 没登记的场景不许凭空编一个名字，原样回填 key
    assert rows["NOT_A_REGISTERED_SCENARIO"]["name"] == "NOT_A_REGISTERED_SCENARIO"
    # 六个维度口径统一：都带 name，前端不必再各抄一份标签表
    assert set(rows["EXPERT_SEARCH"]) >= {"key", "name", "total_tokens",
                                          "total_cost", "calls", "cost_pct"}


async def _seed_package(company_id: str, installed_by: str, *,
                        rolled_back: bool) -> tuple[str, str]:
    pkg_id, inst_id = _id(), _id()
    async with AsyncSessionLocal() as s:
        s.add(MarketplacePackage(id=pkg_id, package_type="SKILL",
                                 key="night-tour-pack", name="夜游能力包",
                                 latest_version="1.2.0", published=True))
        s.add(PackageInstallation(
            id=inst_id, company_id=company_id, package_id=pkg_id,
            version="1.2.0", installed_by=installed_by,
            rolled_back_at=_recent() if rolled_back else None))
        await s.commit()
    return pkg_id, inst_id


async def test_installations_lists_active_installs(client, tenants):
    co = tenants.alice_co["id"]
    _, live = await _seed_package(co, tenants.alice["id"], rolled_back=False)
    _, undone = await _seed_package(co, tenants.alice["id"], rolled_back=True)

    r = await client.get(f"/governance/installations?company_id={co}",
                         headers=tenants.alice["headers"])
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = [x["id"] for x in rows]
    assert live in ids, "已安装面板的唯一数据源没列出生效的安装记录"
    assert undone not in ids, "回滚过的安装记录不该再算「已安装」"

    live_row = next(x for x in rows if x["id"] == live)
    # 前端渲染的就是 package_key（安装记录本身只有 package_id）
    assert live_row["package_key"] == "night-tour-pack"
    assert live_row["package_name"] == "夜游能力包"
    assert live_row["version"] == "1.2.0"


async def test_installations_are_tenant_scoped(client, tenants):
    alice_co = tenants.alice_co["id"]
    await _seed_package(alice_co, tenants.alice["id"], rolled_back=False)

    # B 公司查自己的：空——A 的安装记录不会串到这里
    r = await client.get(f"/governance/installations?company_id={tenants.bob_co['id']}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == []

    # B 拿 A 的 company_id 来查：router 级 require_company_scope 直接拦掉，
    # 且按"不泄露该公司是否存在"的口径回 404
    r = await client.get(f"/governance/installations?company_id={alice_co}",
                         headers=tenants.bob["headers"])
    assert r.status_code == 404, r.text

    # 不带 company_id：端点自身要求必带（否则等于跨公司全量导出）
    r = await client.get("/governance/installations",
                         headers=tenants.bob["headers"])
    assert r.status_code == 422, r.text


def test_provider_type_is_never_serialised_via_str():
    """守住第 1 条的根因：provider_type 一律取 `.value`，不要 `str(枚举)`。"""
    import pathlib
    import re

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    hits = []
    for py in app_dir.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"str\(\s*[\w.\[\]\"']+\.provider_type\s*\)", line):
                hits.append(f"{py.relative_to(app_dir)}:{lineno}: {line.strip()}")
    assert hits == [], "这些地方的 provider_type 会序列化成枚举名：\n" + "\n".join(hits)
