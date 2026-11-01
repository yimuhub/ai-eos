"""License 状态与账单导出（E1/E3/E4）。

三个端点刻意分开声明能力等级：

* `GET /license/status`：只读元信息，**任何登录用户**都能看。
  带 `company_id` 时额外要求 VIEW（只看本公司席位用量，跨公司 404）。
* `GET /billing/usage`：VIEW。跨公司 404（沿用全系统的"不泄露存在性"约定）。
* `GET /billing/invoices/export.csv`：MANAGE（账单是财务口径的对外单据）。

所有响应都**不含签名原文**，也不回显 License 文本。
"""
from __future__ import annotations

import csv
import html
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import Capability, ensure_capability
from app.db.session import get_db
from app.models.company import Company
from app.models.user import User
from app.services import license_service, usage_service

router = APIRouter(tags=["licensing"])

CSV_COLUMNS = (
    "company_id", "period_start", "period_end",
    "seats_limit", "seats_used", "agents_active",
    "tokens_input", "tokens_output", "tokens_total",
    "cost", "currency", "deliverables",
)


def _period_or_400(from_: str | None, to: str | None):
    try:
        return usage_service.parse_period(from_, to)
    except ValueError as exc:
        # 422 太像"字段校验"，这里给业务可读的 400
        raise HTTPException(400, str(exc)) from exc


@router.get("/license/status")
async def license_status(
    company_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前部署的 License 模式与席位情况。

    模式：community（未配置）/ licensed（有效或已过期，看 read_only）/
    invalid（签名不可信，失败关闭为只读）。
    """
    state = license_service.get_state()

    seats_used: int | None = None
    if company_id:
        # 与其它只读端点同口径：无角色 → 404
        await ensure_capability(db, user.id, company_id, Capability.VIEW)
        seats_used = await usage_service.count_active_agents(db, company_id)

    return {
        "mode": state.mode,
        "status": state.status,
        "verification": state.verification,
        "seats": {"limit": state.seats, "used": seats_used},
        "features": list(state.features),
        "ignored_features": list(state.ignored_features),
        "read_only": state.read_only,
        "licensee": state.licensee,
        "instance": state.instance,
        "version": state.version,
        "issued_at": state.issued_at.isoformat() if state.issued_at else None,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        "reason": state.reason,
    }


@router.get("/billing/usage")
async def billing_usage(
    company_id: str = Query(...),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """期间用量：席位 / token / 成本 / 交付物 + 按模型、按天分组。"""
    await ensure_capability(db, user.id, company_id, Capability.VIEW)
    start, end = _period_or_400(from_, to)
    return await usage_service.company_usage(db, company_id, start, end)


@router.get("/billing/invoices/export.csv")
async def export_invoice_csv(
    company_id: str = Query(...),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """月度账单 CSV：一行汇总（金额为十进制定点字符串）。

    这里**不计算价格、不应用套餐/折扣**——只把期间内已经记在
    `token_usages.total_cost` 上的快照成本与用量导出，供财务对账/开票。
    """
    await ensure_capability(db, user.id, company_id, Capability.MANAGE)
    start, end = _period_or_400(from_, to)
    usage = await usage_service.company_usage(db, company_id, start, end)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    seats = usage["seats"]
    writer.writerow([
        usage["company_id"],
        usage["period"]["from"],
        usage["period"]["to"],
        "" if seats["limit"] is None else seats["limit"],
        seats["used"],
        usage["agents_active"],
        usage["tokens"]["input"],
        usage["tokens"]["output"],
        usage["tokens"]["total"],
        usage["cost"],
        usage["currency"],
        usage["deliverables"],
    ])

    filename = (f"aieos-invoice-{company_id}-"
                f"{usage['period']['from']}-{usage['period']['to']}.csv")
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


INVOICE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; color: #1f2329; background: #fff;
       font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC",
       "Microsoft YaHei", sans-serif; }
.invoice { max-width: 880px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 24px; }
.meta { color: #646a73; margin-bottom: 24px; }
.meta span { margin-right: 20px; }
h2 { margin: 28px 0 10px; font-size: 16px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; border: 1px solid #dee0e3; text-align: left; }
th { background: #f5f6f7; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: #8f959e; }
.total { font-size: 18px; font-weight: 600; }
.foot { margin-top: 32px; color: #8f959e; font-size: 12px; }
@media print {
  body { padding: 0; }
  .invoice { max-width: none; }
  th { background: #f5f6f7 !important; -webkit-print-color-adjust: exact;
       print-color-adjust: exact; }
}
"""


@router.get("/billing/invoices/view", response_class=HTMLResponse)
async def view_invoice_html(
    company_id: str = Query(...),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """月度账单 HTML 视图：浏览器直接打印为 PDF。

    与 CSV 同口径（usage_service.company_usage）；账单号确定性生成，
    同一公司同一周期永远得到同一个号。所有动态文本都经 html.escape，
    公司名/模型名不能注入标签。
    """
    await ensure_capability(db, user.id, company_id, Capability.MANAGE)
    start, end = _period_or_400(from_, to)
    usage = await usage_service.company_usage(db, company_id, start, end)
    company = await db.get(Company, company_id)
    if company is None:  # ensure_capability 已校验，保留防御式分支
        raise HTTPException(404, "Company not found")

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    invoice_no = (f"INV-{company_id[:8]}-{start.strftime('%Y%m%d')}-"
                  f"{end.strftime('%Y%m%d')}")
    seats = usage["seats"]
    seat_limit = "不限" if seats["limit"] is None else f"{seats['limit']:,}"
    model_rows = "".join(
        f"<tr><td>{esc(m['model'])}</td>"
        f"<td class=\"num\">{m['tokens']['input']:,}</td>"
        f"<td class=\"num\">{m['tokens']['output']:,}</td>"
        f"<td class=\"num\">{m['tokens']['total']:,}</td>"
        f"<td class=\"num\">{esc(m['cost'])}</td></tr>"
        for m in usage["by_model"]
    ) or '<tr><td colspan="5" class="muted">本期无用量流水</td></tr>'

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>账单 {esc(invoice_no)} - {esc(company.name)}</title>
<style>{INVOICE_CSS}</style>
</head>
<body>
<div class="invoice">
  <h1>AIEOS 月度账单</h1>
  <div class="meta">
    <span>客户：{esc(company.name)}</span>
    <span>账单号：{esc(invoice_no)}</span>
  </div>
  <div class="meta">
    <span>周期：{esc(usage['period']['from'])} 至 {esc(usage['period']['to'])}</span>
    <span>币种：{esc(usage['currency'])}</span>
  </div>

  <h2>汇总</h2>
  <table>
    <thead><tr><th>项目</th><th class="num">数值</th></tr></thead>
    <tbody>
      <tr><td>席位（已用 / 上限）</td>
          <td class="num">{seats['used']:,} / {esc(seat_limit)}</td></tr>
      <tr><td>Token（输入 / 输出 / 合计）</td>
          <td class="num">{usage['tokens']['input']:,} /
          {usage['tokens']['output']:,} / {usage['tokens']['total']:,}</td></tr>
      <tr><td>交付物</td>
          <td class="num">{usage['deliverables']:,}</td></tr>
      <tr><td class="total">应付金额</td>
          <td class="num total">{esc(usage['cost'])} {esc(usage['currency'])}</td></tr>
    </tbody>
  </table>

  <h2>按模型明细</h2>
  <table>
    <thead><tr><th>模型</th><th class="num">输入 Token</th>
      <th class="num">输出 Token</th><th class="num">合计 Token</th>
      <th class="num">成本</th></tr></thead>
    <tbody>{model_rows}</tbody>
  </table>

  <div class="foot">
    账单号 {esc(invoice_no)} · 金额为期间内已记账的成本快照，
    不含未出账用量；打印本页即可另存为 PDF。
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(
        content=page,
        headers={"Cache-Control": "no-store"},
    )

