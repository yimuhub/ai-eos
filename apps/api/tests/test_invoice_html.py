"""E4 月度 HTML 账单：内容、确定性账单号、转义与鉴权/周期校验。"""
from __future__ import annotations

from datetime import datetime

from app.db.session import AsyncSessionLocal
from app.models.token_usage import TokenUsage


async def _add_usage(company_id: str, model: str, day: str,
                     input_tokens: int, output_tokens: int,
                     cost: float) -> None:
    async with AsyncSessionLocal() as s:
        s.add(TokenUsage(
            company_id=company_id, model_name=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cost=cost, output_cost=0.0, total_cost=cost,
            created_at=datetime.fromisoformat(f"{day}T12:00:00"),
        ))
        await s.commit()


async def test_invoice_html_renders_company_invoice_no_and_model_table(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner, name="星云 SaaS")
    await _add_usage(company["id"], "model-a", "2026-03-02", 100, 50, 0.1)
    await _add_usage(company["id"], "model-b", "2026-03-03", 200, 100, 0.2)

    r = await client.get("/billing/invoices/view", params={
        "company_id": company["id"], "from": "2026-03-01", "to": "2026-03-31",
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "星云 SaaS" in r.text

    invoice_no = (f"INV-{company['id'][:8]}-"
                  f"20260301-20260331")
    assert invoice_no in r.text
    # 周期、用量、成本与按模型明细都在页面上
    assert "2026-03-01" in r.text and "2026-03-31" in r.text
    assert "300" in r.text and "150" in r.text
    assert "0.30" in r.text
    assert "model-a" in r.text and "model-b" in r.text
    assert "@media print" in r.text


async def test_invoice_html_escapes_company_and_model_names(
        client, make_user, make_company):
    owner = await make_user()
    company = await make_company(
        owner, name='恶意 <script>alert("x")</script> 公司')
    await _add_usage(company["id"], "<img src=x onerror=alert(1)>",
                     "2026-03-02", 10, 5, 0.01)

    r = await client.get("/billing/invoices/view", params={
        "company_id": company["id"], "from": "2026-03-01", "to": "2026-03-31",
    }, headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "<img src=x" not in r.text
    assert "&lt;img" in r.text


async def test_invoice_html_cross_tenant_is_404(client, tenants):
    r = await client.get("/billing/invoices/view", params={
        "company_id": tenants.alice_co["id"],
        "from": "2026-03-01", "to": "2026-03-31",
    }, headers=tenants.bob["headers"])
    assert r.status_code == 404


async def test_invoice_html_rejects_bad_period(client, make_user, make_company):
    owner = await make_user()
    company = await make_company(owner)

    r = await client.get("/billing/invoices/view", params={
        "company_id": company["id"], "from": "2026-03-31", "to": "2026-03-01",
    }, headers=owner["headers"])
    assert r.status_code == 400

    r = await client.get("/billing/invoices/view", params={
        "company_id": company["id"], "from": "not-a-date", "to": "2026-03-31",
    }, headers=owner["headers"])
    assert r.status_code == 400
