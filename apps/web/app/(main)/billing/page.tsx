"use client";

/**
 * 套餐与账单：把「升级」从一句话变成一条能走完的路。
 *
 * 背景：后端此前任何公司老板一次 POST /subscription/plan 就能免费升到企业版，
 * 现在升级/续费必须先有订单、由平台确认收款，再落到订阅上。后端修好了，
 * 但界面上仍然没有入口——老板只会在「席位已满」的报错里读到"去提交订单"，
 * 却找不到能提交订单的页面。这个页面就是那条路的界面。
 *
 * 两条角色视角放在同一页：
 *   - 公司视角（谁都能看）：当前套餐、配额进度、我的订单、申请升级、降级、取消；
 *   - 平台视角（仅 is_superadmin）：全部公司待确认收款订单，一键盖章。
 * 自托管部署里首位安装者通常两种身份都有，本地可以自助走完整个闭环。
 */
import { useCallback, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import { formatLocalStamp } from "@/lib/remoteDevices";

type Plan = {
  plan_code: string;
  label: string;
  seats_limit: number | null;
  monthly_token_quota: number | null;
};

type Subscription = {
  company_id: string;
  plan_code: string;
  plan_label: string;
  status: string;
  effective_status: string;
  trial_ends_at: string | null;
  period: { start: string | null; end: string | null };
  limits: { seats: number | null; monthly_tokens: number | null };
  seats_limit: number | null;
  monthly_token_quota: number | null;
  usage: { seats_used: number; tokens_used_this_month: number } | null;
};

type Order = {
  id: string;
  company_id: string;
  plan_code: string;
  plan_label: string;
  status: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  applied_at: string | null;
  note: string | null;
  created_at: string | null;
  company_name?: string;
};

type Me = { id: string; is_superadmin?: boolean };

const PLAN_RANK: Record<string, number> = { trial: 0, pro: 1, enterprise: 2 };

const STATUS_LABEL: Record<string, { text: string; tone: string }> = {
  TRIALING: { text: "试用中", tone: "text-emerald-300" },
  TRIAL_EXPIRED: { text: "试用已到期", tone: "text-amber-300" },
  ACTIVE: { text: "生效中", tone: "text-emerald-300" },
  PAST_DUE: { text: "已逾期", tone: "text-amber-300" },
  CANCELED: { text: "已取消", tone: "text-zinc-400" },
};

const ORDER_STATUS: Record<string, { text: string; tone: string }> = {
  PENDING: { text: "待平台确认收款", tone: "text-amber-300" },
  PAID: { text: "已收款，待启用", tone: "text-indigo-300" },
  APPLIED: { text: "已生效", tone: "text-emerald-300" },
  CANCELED: { text: "已取消", tone: "text-zinc-400" },
};

function fmtCount(n: number | null): string {
  if (n === null) return "不限";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n % 1_000 === 0 ? 0 : 1)}K`;
  return String(n);
}

function UsageBar({ used, limit, unit }: { used: number; limit: number | null; unit: string }) {
  if (limit === null) {
    return (
      <div className="text-xs" style={{ color: "var(--muted)" }}>
        已用 {fmtCount(used)} {unit} · 不限量
      </div>
    );
  }
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 100;
  const full = limit > 0 && used >= limit;
  return (
    <div>
      <div className="flex items-center justify-between text-xs mb-1">
        <span style={{ color: "var(--muted)" }}>已用 {fmtCount(used)} / {fmtCount(limit)} {unit}</span>
        <span className={full ? "text-amber-300" : "text-[var(--muted)]"}>
          {limit === 0 ? "已冻结" : `${pct}%`}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-white/[0.08] overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${full ? "bg-gradient-to-r from-amber-500 to-red-500" : "bg-gradient-to-r from-indigo-500 to-cyan-400"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function BillingPage() {
  const cid = useActiveCompanyId();
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"ok" | "err">("ok");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const flash = (text: string, type: "ok" | "err" = "ok") => {
    setMsg(text);
    setMsgType(type);
    setTimeout(() => setMsg(""), 4000);
  };

  const {
    data: sub, loading: subLoading, error: subError, refresh: refreshSub,
  } = useResource<Subscription>(
    cid ? `billing:sub:${cid}` : null,
    () => api.get<Subscription>(`/subscription?company_id=${cid}`),
  );

  // 套餐目录是全局常量，不分公司；缓存一次即可。
  const { data: plans } = useResource<Plan[]>(
    "billing:plans",
    async () => (await api.get<{ plans: Plan[] }>("/subscription/plans")).plans || [],
  );

  const {
    data: orders, refresh: refreshOrders,
  } = useResource<Order[]>(
    cid ? `billing:orders:${cid}` : null,
    async () => (await api.get<{ orders: Order[] }>(`/subscription/orders?company_id=${cid}`)).orders || [],
  );

  const { data: me } = useResource<Me>(
    "billing:me",
    () => api.get<Me>("/auth/me"),
  );
  const isSuperadmin = !!me?.is_superadmin;

  // 平台视角：只有超管会真的发请求（非超管后端直接 404）。
  const {
    data: pending, refresh: refreshPending,
  } = useResource<Order[]>(
    isSuperadmin ? "billing:pending" : null,
    async () => (await api.get<{ orders: Order[] }>("/subscription/orders/pending")).orders || [],
  );

  const refreshAll = useCallback(() => {
    refreshSub();
    refreshOrders();
    if (isSuperadmin) refreshPending();
  }, [refreshSub, refreshOrders, refreshPending, isSuperadmin]);

  const run = async (key: string, task: () => Promise<void>) => {
    setBusy(key);
    try {
      await task();
    } catch (e) {
      flash(describeError(e, "操作失败"), "err");
    } finally {
      setBusy(null);
    }
  };

  const requestOrder = (plan: Plan) => run(`order:${plan.plan_code}`, async () => {
    await api.post("/subscription/orders", {
      company_id: cid,
      plan_code: plan.plan_code,
      note: note.trim() || null,
    });
    setNote("");
    refreshAll();
    flash(`已提交「${plan.label}」申请：平台确认收款后，回到本页点「立即启用」即可生效`);
  });

  const downgrade = async (plan: Plan) => {
    const ok = await confirmAction({
      title: `降级到「${plan.label}」？`,
      description: "降级立即生效，本计费周期不会重算，超出新套餐上限的部分不会回删。",
      bullets: [
        `席位上限：${fmtCount(sub?.seats_limit ?? null)} → ${fmtCount(plan.seats_limit)}`,
        `月度 token：${fmtCount(sub?.monthly_token_quota ?? null)} → ${fmtCount(plan.monthly_token_quota)}`,
      ],
      confirmText: "确认降级",
    });
    if (!ok) return;
    await run(`down:${plan.plan_code}`, async () => {
      await api.post("/subscription/plan", { company_id: cid, plan_code: plan.plan_code });
      refreshAll();
      flash(`已降级到「${plan.label}」`);
    });
  };

  const cancelOrder = async (order: Order) => {
    const ok = await confirmAction({
      title: "取消这张订单？",
      description: "取消后需要重新提交申请。",
      confirmText: "取消订单",
      danger: true,
    });
    if (!ok) return;
    await run(`cancel:${order.id}`, async () => {
      await api.post(`/subscription/orders/${order.id}/cancel`);
      refreshAll();
      flash("订单已取消");
    });
  };

  const confirmPaid = (order: Order) => run(`confirm:${order.id}`, async () => {
    await api.post(`/subscription/orders/${order.id}/confirm`);
    refreshAll();
    flash(`已确认「${order.company_name || order.company_id}」的「${order.plan_label}」收款，通知公司启用套餐`);
  });

  const applyPaid = (order: Order) => run(`apply:${order.id}`, async () => {
    await api.post("/subscription/plan", {
      company_id: order.company_id,
      plan_code: order.plan_code,
      order_id: order.id,
    });
    refreshAll();
    flash(`「${order.plan_label}」已生效`);
  });

  const cancelSubscription = async () => {
    const ok = await confirmAction({
      title: "取消订阅？",
      description: "当前计费周期结束后进入冻结状态：已有员工可以继续看数据，但不能扩编、不能再调用模型。",
      confirmText: "取消订阅",
      danger: true,
    });
    if (!ok) return;
    await run("cancel-sub", async () => {
      await api.post("/subscription/cancel", { company_id: cid });
      refreshAll();
      flash("订阅已取消");
    });
  };

  const currentRank = sub ? (PLAN_RANK[sub.plan_code] ?? 0) : 0;
  const status = sub ? (STATUS_LABEL[sub.effective_status] || { text: sub.effective_status, tone: "text-zinc-300" }) : null;
  const pendingPlans = new Set((orders || []).filter((o) => o.status === "PENDING").map((o) => o.plan_code));
  const paidOrders = (orders || []).filter((o) => o.status === "PAID");

  return (
    <div className="p-6 max-w-[1200px] mx-auto animate-fade-up space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-[var(--foreground)] flex items-center gap-2">
          <span className="pulse-dot" /> 💳 套餐与账单
        </h1>
        <p className="text-sm text-[var(--muted-foreground)] mt-1">
          当前套餐、配额进度、升级申请与订单 · 升级需平台确认收款后生效
        </p>
      </div>

      {msg && (
        <div className={`rounded-lg px-4 py-2.5 text-sm ${msgType === "ok"
          ? "bg-emerald-500/10 text-emerald-200 ring-1 ring-emerald-500/25"
          : "bg-red-500/10 text-red-200 ring-1 ring-red-500/25"}`}>
          {msg}
        </div>
      )}

      {!cid && <p className="text-sm" style={{ color: "var(--muted)" }}>还没有公司，先去「公司管理」建一家。</p>}
      {cid && subLoading && !sub && <p className="text-sm" style={{ color: "var(--muted)" }}>正在读取订阅…</p>}
      {subError && <p className="text-sm text-red-300">加载订阅失败：{subError}</p>}

      {sub && status && (
        <div className="card p-5 space-y-4">
          <div className="flex flex-wrap items-start gap-3">
            <div className="mr-auto">
              <div className="flex items-center gap-2">
                <span className="text-lg font-semibold">{sub.plan_label}</span>
                <span className={`text-xs px-2 py-0.5 rounded-full bg-white/[0.06] ${status.tone}`}>{status.text}</span>
              </div>
              <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                {sub.effective_status === "TRIALING" && sub.trial_ends_at
                  ? `试用截止 ${formatLocalStamp(sub.trial_ends_at)}`
                  : sub.period.end
                    ? `本周期至 ${formatLocalStamp(sub.period.end)}`
                    : "尚未开通计费周期"}
              </p>
              {sub.effective_status === "TRIAL_EXPIRED" && (
                <p className="text-xs mt-1 text-amber-300">
                  试用已到期，系统已冻结扩编与模型调用。提交升级申请并确认收款后立即恢复。
                </p>
              )}
            </div>
            {sub.status !== "CANCELED" && (
              <button onClick={cancelSubscription} disabled={!!busy}
                className="btn-ghost !py-2 !px-3 text-xs" title="取消后本周期结束进入冻结">
                取消订阅
              </button>
            )}
          </div>

          <div className="grid md:grid-cols-2 gap-4">
            <UsageBar used={sub.usage?.seats_used ?? 0} limit={sub.seats_limit} unit="席位" />
            <UsageBar used={sub.usage?.tokens_used_this_month ?? 0} limit={sub.monthly_token_quota} unit="token / 本月" />
          </div>
        </div>
      )}

      {/* 套餐对比 */}
      <div className="grid md:grid-cols-2 gap-3">
        {(plans || []).map((plan) => {
          const rank = PLAN_RANK[plan.plan_code] ?? 0;
          const isCurrent = sub?.plan_code === plan.plan_code;
          const isPending = pendingPlans.has(plan.plan_code);
          const higher = rank > currentRank;
          const lower = rank < currentRank;
          return (
            <div key={plan.plan_code}
              className={`card p-5 space-y-3 ${isCurrent ? "ring-1 ring-indigo-500/40" : ""}`}>
              <div className="flex items-center gap-2">
                <span className="font-semibold">{plan.label}</span>
                {isCurrent && <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300">当前套餐</span>}
              </div>
              <ul className="text-sm space-y-1" style={{ color: "var(--muted)" }}>
                <li>席位：{fmtCount(plan.seats_limit)}</li>
                <li>月度 token：{fmtCount(plan.monthly_token_quota)}</li>
              </ul>
              {isCurrent && (
                <button onClick={() => requestOrder(plan)} disabled={!!busy || isPending}
                  className="btn-ghost !py-2 text-sm w-full">
                  {isPending ? "已有待确认订单" : busy === `order:${plan.plan_code}` ? "提交中…" : "续费本套餐"}
                </button>
              )}
              {higher && (
                <button onClick={() => requestOrder(plan)} disabled={!!busy || isPending}
                  className="btn-primary !py-2 text-sm w-full disabled:opacity-40">
                  {isPending ? "等待平台确认收款" : busy === `order:${plan.plan_code}` ? "提交中…" : `申请升级到${plan.label}`}
                </button>
              )}
              {lower && (
                <button onClick={() => downgrade(plan)} disabled={!!busy}
                  className="btn-ghost !py-2 text-sm w-full">
                  {busy === `down:${plan.plan_code}` ? "处理中…" : `降级到${plan.label}`}
                </button>
              )}
            </div>
          );
        })}
      </div>

      <div className="card p-4">
        <label className="text-xs" style={{ color: "var(--muted)" }}>
          申请备注（可选）：打款账户、发票抬头、对接人
        </label>
        <input value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="例如：已对公转账，回单号 20261109-001"
          className="input w-full mt-1.5 text-sm" maxLength={500} />
      </div>

      {/* 我的订单 */}
      <div className="space-y-2">
        <h2 className="text-sm font-semibold" style={{ color: "var(--muted)" }}>我的订单</h2>
        {paidOrders.length > 0 && (
          <div className="rounded-lg px-4 py-2.5 text-xs bg-indigo-500/10 text-indigo-200 ring-1 ring-indigo-500/25">
            有 {paidOrders.length} 张订单已确认收款，点「立即启用」即可落到当前套餐。
          </div>
        )}
        {(!orders || orders.length === 0) && <p className="text-xs" style={{ color: "var(--muted)" }}>还没有订单。</p>}
        {(orders || []).map((order) => {
          const st = ORDER_STATUS[order.status] || { text: order.status, tone: "text-zinc-300" };
          return (
            <div key={order.id} className="card px-4 py-3 flex flex-wrap items-center gap-3">
              <div className="mr-auto min-w-0">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium">{order.plan_label}</span>
                  <span className={`text-[11px] ${st.tone}`}>{st.text}</span>
                </div>
                <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                  提交 {formatLocalStamp(order.created_at)}
                  {order.confirmed_at ? ` · 收款确认 ${formatLocalStamp(order.confirmed_at)}` : ""}
                  {order.applied_at ? ` · 生效 ${formatLocalStamp(order.applied_at)}` : ""}
                  {order.note ? ` · ${order.note}` : ""}
                </p>
              </div>
              {order.status === "PENDING" && (
                <button onClick={() => cancelOrder(order)} disabled={!!busy}
                  className="btn-ghost !py-1.5 !px-3 text-xs">取消</button>
              )}
              {order.status === "PAID" && (
                <button onClick={() => applyPaid(order)} disabled={!!busy}
                  className="btn-primary !py-1.5 !px-3 text-xs">
                  {busy === `apply:${order.id}` ? "启用中…" : "立即启用"}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* 平台视角 */}
      {isSuperadmin && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold" style={{ color: "var(--muted)" }}>
            平台待确认收款（超管可见）
          </h2>
          {pending && pending.length === 0 && <p className="text-xs" style={{ color: "var(--muted)" }}>没有待处理的订单。</p>}
          {(pending || []).map((order) => (
            <div key={order.id} className="card px-4 py-3 flex flex-wrap items-center gap-3 ring-1 ring-amber-500/20">
              <div className="mr-auto min-w-0">
                <div className="text-sm">
                  <span className="font-medium">{order.company_name || order.company_id}</span>
                  <span className="mx-1.5" style={{ color: "var(--muted)" }}>→</span>
                  <span>{order.plan_label}</span>
                </div>
                <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                  提交 {formatLocalStamp(order.created_at)}{order.note ? ` · ${order.note}` : ""}
                </p>
              </div>
              <button onClick={() => confirmPaid(order)} disabled={!!busy}
                className="btn-primary !py-1.5 !px-3 text-xs">
                {busy === `confirm:${order.id}` ? "确认中…" : "确认收款"}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
