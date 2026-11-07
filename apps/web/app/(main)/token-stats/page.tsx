"use client";
import { useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { formatLocalStamp } from "@/lib/remoteDevices";
import { confirmAction } from "@/components/ConfirmDialog";

type Pricing = {
  id: string; model_id: string; model_name: string;
  billing_mode: string;
  input_price_per_m: number; output_price_per_m: number;
  monthly_fee: number; overage_input_per_m: number; overage_output_per_m: number;
  included_tokens_per_month: number; currency: string; enabled: boolean; note?: string | null;
};
type ModelRow = { id: string; name: string; model_id: string; provider_type: string };
type StatRow = { key: string; name?: string; input_tokens: number; output_tokens: number; total_tokens: number; total_cost: number; calls: number; cost_pct?: number; rule_candidate?: boolean; suggestion?: string | null };
type Summary = {
  input_tokens: number; output_tokens: number; total_tokens: number;
  total_cost: number; calls: number; avg_latency_ms: number;
  trend: { date: string; total_tokens: number; total_cost: number; calls: number }[];
};
type UsageLog = {
  id: string; agent_name: string; scenario: string; scenario_label: string;
  model_name: string; input_tokens: number; output_tokens: number; total_tokens: number;
  total_cost: number; latency_ms: number; success: boolean; created_at?: string | null;
};

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}
function fmtCost(c: number): string {
  if (c === 0) return "¥0";
  if (c < 0.01) return "¥" + c.toFixed(4);
  return "¥" + c.toFixed(2);
}

type Tab = "dashboard" | "pricing" | "budget" | "logs";

type StatsBundle = {
  summary: Summary | null;
  byAgent: StatRow[]; byDept: StatRow[]; byCompany: StatRow[];
  byProject: StatRow[]; byTask: StatRow[]; byScenario: StatRow[];
};
type ScopeOpt = { id: string; name: string };

// 模块级空数组：`?? []` 每次渲染都是新引用，会让下游 useMemo 依赖每帧失效
const EMPTY_ROWS: StatRow[] = [];
const EMPTY_SCOPES: ScopeOpt[] = [];
const EMPTY_PRICINGS: Pricing[] = [];
const EMPTY_MODELS: ModelRow[] = [];
const EMPTY_LOGS: UsageLog[] = [];
const EMPTY_BUDGETS: any[] = [];

export default function TokenAdminPage() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [days, setDays] = useState(30);

  // 激活公司可订阅：切公司 → 统计/流水/预算/范围选项全部自动换到新公司
  const cid = useActiveCompanyId();
  // undefined = 还没确认好激活公司。此时先别请求，否则会先按"无公司"打一轮、
  // cid 一到又重打一轮（首屏闪一次 0 数据）。
  const companyReady = cid !== undefined;

  /* ---------- 数据（统一交给 useResource：去重、缓存、聚焦重取） ---------- */
  // 统计看板：7 个口径一次并发拉回，失败的那一项退回空值（与原来的
  // allSettled 逐项兜底一致，不会因为一个口径挂了整页空白）
  const { data: stats, refresh: refreshStats } = useResource<StatsBundle>(
    companyReady ? `tokens:stats:${cid ?? "none"}:${days}` : null,
    async () => {
      const [s, a, d, co, p, t, sc] = await Promise.allSettled([
        api.get<Summary>(`/tokens/stats/summary?days=${days}`),
        cid ? api.get<StatRow[]>(`/tokens/stats/by-agent?company_id=${cid}&days=${days}`) : Promise.resolve([]),
        cid ? api.get<StatRow[]>(`/tokens/stats/by-department?company_id=${cid}&days=${days}`) : Promise.resolve([]),
        api.get<StatRow[]>(`/tokens/stats/by-company?days=${days}`),
        cid ? api.get<StatRow[]>(`/tokens/stats/by-project?company_id=${cid}&days=${days}`) : Promise.resolve([]),
        cid ? api.get<StatRow[]>(`/tokens/stats/by-task?company_id=${cid}&days=${days}`) : Promise.resolve([]),
        cid ? api.get<StatRow[]>(`/tokens/stats/by-scenario?company_id=${cid}&days=${days}`) : Promise.resolve([]),
      ]);
      const pick = (r: PromiseSettledResult<StatRow[]>) => (r.status === "fulfilled" ? r.value : []);
      return {
        summary: s.status === "fulfilled" ? s.value : null,
        byAgent: pick(a), byDept: pick(d), byCompany: pick(co),
        byProject: pick(p), byTask: pick(t), byScenario: pick(sc),
      };
    },
  );
  const summary = stats?.summary ?? null;
  const byAgent = stats?.byAgent ?? EMPTY_ROWS;
  const byDept = stats?.byDept ?? EMPTY_ROWS;
  const byCompany = stats?.byCompany ?? EMPTY_ROWS;
  const byProject = stats?.byProject ?? EMPTY_ROWS;
  const byTask = stats?.byTask ?? EMPTY_ROWS;
  const byScenario = stats?.byScenario ?? EMPTY_ROWS;

  // 范围选项（预算表单的部门/员工下拉）
  const { data: scopeOpts } = useResource<{ departments: ScopeOpt[]; agents: ScopeOpt[] }>(
    cid ? `tokens:scope:${cid}` : null,
    async () => {
      const [r, ra] = await Promise.all([
        api.get<ScopeOpt[]>(`/companies/${cid}/departments`).catch(() => [] as ScopeOpt[]),
        api.get<ScopeOpt[]>(`/agents?company_id=${cid}`).catch(() => [] as ScopeOpt[]),
      ]);
      return {
        departments: Array.isArray(r) ? r : ((r as any)?.departments || []),
        agents: Array.isArray(ra) ? ra : [],
      };
    },
  );
  const departments = scopeOpts?.departments ?? EMPTY_SCOPES;
  const agents = scopeOpts?.agents ?? EMPTY_SCOPES;

  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"ok" | "err">("ok");
  const flash = (t: string, type: "ok" | "err" = "ok") => {
    setMsg(t); setMsgType(type); setTimeout(() => setMsg(""), 3500);
  };

  // 计费表单
  const [pf, setPf] = useState({
    model_id: "", billing_mode: "USAGE",
    input_price_per_m: 0, output_price_per_m: 0,
    monthly_fee: 0, overage_input_per_m: 0, overage_output_per_m: 0,
    included_tokens_per_month: 0,
  });
  const [editingId, setEditingId] = useState<string | null>(null);

  // 计费标准：账号级配置，与公司无关，只有切到该 Tab 才拉
  const { data: pricingData, refresh: refreshPricing } = useResource<{ pricings: Pricing[]; models: ModelRow[] }>(
    tab === "pricing" ? "tokens:pricing" : null,
    async () => {
      const [p, m] = await Promise.all([
        api.get<Pricing[]>("/tokens/pricing"),
        api.get<ModelRow[]>("/tokens/pricing/models"),
      ]);
      return { pricings: Array.isArray(p) ? p : [], models: Array.isArray(m) ? m : [] };
    },
  );
  const pricings = pricingData?.pricings ?? EMPTY_PRICINGS;
  const models = pricingData?.models ?? EMPTY_MODELS;

  // 用量流水：只在 Tab 激活时拉，与原来的"切 Tab 才请求"一致
  const { data: logData, refresh: refreshLogs } = useResource<UsageLog[]>(
    tab === "logs" && cid ? `tokens:logs:${cid}` : null,
    async () => {
      const r = await api.get<UsageLog[]>(`/tokens/usage-logs?company_id=${cid}&limit=100`);
      return Array.isArray(r) ? r : [];
    },
  );
  const logs = logData ?? EMPTY_LOGS;

  // V5.5 P0：预算上限
  const [bform, setBform] = useState({
    scope_type: "company", scope_id: "", limit_tokens: 0, limit_cost: 0, period: "day",
  });
  const { data: budgetData, refresh: refreshBudgets } = useResource<any[]>(
    tab === "budget" && cid ? `tokens:budgets:${cid}` : null,
    async () => {
      const r = await api.get<{ budgets?: any[] }>(`/token-budgets?company_id=${cid}`);
      return Array.isArray(r?.budgets) ? r.budgets : [];
    },
  );
  const budgets = budgetData ?? EMPTY_BUDGETS;

  async function saveBudget() {
    try {
      if (!cid) return flash("还没有公司，无法设预算", "err");
      if (!bform.limit_tokens && !bform.limit_cost) return flash("请至少填一个上限", "err");
      const payload: Record<string, unknown> = { ...bform };
      if (bform.scope_type === "company") delete payload.scope_id;
      else if (!bform.scope_id) {
        delete payload.scope_id;
        return flash(bform.scope_type === "department" ? "请选择要限制的部门" : "请选择要限制的员工", "err");
      }
      await api.post(`/token-budgets?company_id=${cid}`, payload);
      flash("✅ 预算上限已生效：到顶自动暂停（不加价惩罚）");
      setBform({ scope_type: "company", scope_id: "", limit_tokens: 0, limit_cost: 0, period: "day" });
      refreshBudgets();
    } catch (e: any) { flash(e.message || String(e), "err"); }
  }

  async function toggleBudget(b: any) {
    try {
      if (!cid) return;
      const res = await api.put(`/token-budgets/${b.id}?company_id=${cid}`,
        { enabled: !b.paused, paused: false });
      // V5.6.1 tripped→resume：恢复预算时后端自动续跑挂起执行
      const resumed: string[] = res?.resumed_executions || [];
      flash(b.paused
        ? `✅ 已恢复执行${resumed.length ? `，${resumed.length} 个挂起任务自动续跑` : ""}`
        : "⏸ 已暂停该预算（LLM 调用将被拦截）");
      refreshBudgets();
    } catch (e: any) { flash(e.message || String(e), "err"); }
  }

  async function deleteBudget(b: any) {
    try {
      if (!cid) return;
      await api.del(`/token-budgets/${b.id}?company_id=${cid}`);
      flash("已删除预算上限");
      refreshBudgets();
    } catch (e: any) { flash(e.message || String(e), "err"); }
  }

  async function savePricing() {
    if (!pf.model_id) return flash("请选择模型", "err");
    try {
      await api.post("/tokens/pricing", { ...pf, enabled: true });
      flash("✅ 计费标准已保存，即刻生效（新调用按新单价计算）");
      setEditingId(null);
      setPf({ model_id: "", billing_mode: "USAGE", input_price_per_m: 0, output_price_per_m: 0, monthly_fee: 0, overage_input_per_m: 0, overage_output_per_m: 0, included_tokens_per_month: 0 });
      refreshPricing(); refreshStats();
    } catch (e: any) { flash(e.message || String(e), "err"); }
  }

  function editPricing(p: Pricing) {
    setEditingId(p.id);
    setPf({
      model_id: p.model_id, billing_mode: p.billing_mode,
      input_price_per_m: p.input_price_per_m, output_price_per_m: p.output_price_per_m,
      monthly_fee: p.monthly_fee, overage_input_per_m: p.overage_input_per_m,
      overage_output_per_m: p.overage_output_per_m,
      included_tokens_per_month: p.included_tokens_per_month,
    });
  }

  async function delPricing(p: Pricing) {
    if (!(await confirmAction({
      title: `删除「${p.model_name}」的计费标准？`,
      description: "删除后该模型将回落到默认计费标准，已产生的历史用量记录不受影响。",
      confirmText: "删除",
    }))) return;
    try { await api.del(`/tokens/pricing/${p.id}`); flash("已删除"); refreshPricing(); }
    catch (e: any) { flash(e.message || String(e), "err"); }
  }

  const maxTrend = Math.max(1, ...(summary?.trend || []).map((t) => t.total_tokens));

  /* ---------- 小组件 ---------- */
  function KpiCard({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent: string }) {
    return (
      <div className="card p-4 relative overflow-hidden">
        <div className={`absolute left-0 top-0 bottom-0 w-1 ${accent}`} />
        <p className="text-xs text-[var(--muted-foreground)]">{label}</p>
        <p className="text-2xl font-bold text-[var(--foreground)] mt-1">{value}</p>
        {sub && <p className="text-xs text-[var(--muted-foreground)] mt-0.5">{sub}</p>}
      </div>
    );
  }

  function StatTable({ title, icon, rows, emptyHint }: { title: string; icon: string; rows: StatRow[]; emptyHint: string }) {
    const max = Math.max(1, ...rows.map((r) => r.total_tokens));
    return (
      <div className="card p-5">
        <h3 className="font-semibold text-[var(--foreground)] mb-3">{icon} {title}</h3>
        {rows.length ? (
          <div className="space-y-2">
            {rows.slice(0, 12).map((r) => (
              <div key={r.key}>
                <div className="flex items-center justify-between text-xs mb-1">
                  <span className="text-[var(--foreground)] font-medium truncate max-w-[60%]">{r.name || r.key}</span>
                  <span className="text-[var(--muted-foreground)] shrink-0 ml-2">
                    {fmtTokens(r.total_tokens)} tokens · {fmtCost(r.total_cost)} · {r.calls}次
                    {r.cost_pct ? ` · 成本占比 ${r.cost_pct}%` : ""}
                  </span>
                </div>
                {/* V5.8 ⑩ 成本分维：可规则化场景的高占比降本建议 */}
                {r.suggestion && (
                  <div className="text-[11px] mt-0.5 px-2 py-1 rounded bg-amber-500/10 text-amber-300">
                    💡 {r.suggestion}
                  </div>
                )}
                <div className="h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                  <div className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500"
                    style={{ width: `${Math.max(3, (r.total_tokens / max) * 100)}%` }} />
                </div>
              </div>
            ))}
          </div>
        ) : <p className="text-xs text-[var(--muted-foreground)] py-4 text-center">{emptyHint}</p>}
      </div>
    );
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-4">
      {/* 页头 + Tab */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="mr-auto">
          <h1 className="text-2xl font-bold text-[var(--foreground)] flex items-center gap-2">
            <span className="pulse-dot" /> 📊 Token 统计与计费
          </h1>
          <p className="text-sm text-[var(--muted-foreground)] mt-1">
            所有接入模型的用量自动计量 · 六维统计 · 后台计费标准
          </p>
        </div>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="input w-auto text-sm">
          <option value={1}>最近24小时</option>
          <option value={7}>最近7天</option>
          <option value={30}>最近30天</option>
          <option value={90}>最近90天</option>
        </select>
        <div className="flex rounded-lg bg-white/[0.05] p-1">
          {([["dashboard", "📈 统计看板"], ["pricing", "💰 计费设置"], ["budget", "🛡 预算上限"], ["logs", "📜 用量流水"]] as [Tab, string][]).map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-4 py-1.5 rounded-md text-sm transition-all ${tab === k
                ? "bg-gradient-to-r from-indigo-600 to-violet-600 text-white shadow-md"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* ===== Tab 1: 统计看板 ===== */}
      {tab === "dashboard" && (
        <div className="space-y-4">
          {/* KPI 卡片 */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <KpiCard label="总 Token 用量" value={fmtTokens(summary?.total_tokens ?? 0)}
              sub={`输入 ${fmtTokens(summary?.input_tokens ?? 0)} / 输出 ${fmtTokens(summary?.output_tokens ?? 0)}`}
              accent="bg-gradient-to-b from-indigo-500 to-violet-500" />
            <KpiCard label="总费用" value={fmtCost(summary?.total_cost ?? 0)}
              sub="按后台计费标准估算" accent="bg-gradient-to-b from-emerald-500 to-green-500" />
            <KpiCard label="调用次数" value={String(summary?.calls ?? 0)}
              sub={`平均延迟 ${(summary?.avg_latency_ms ?? 0) / 1000}s`} accent="bg-gradient-to-b from-blue-500 to-cyan-500" />
            <KpiCard label="日均消耗" value={fmtTokens(Math.round((summary?.total_tokens ?? 0) / Math.max(days, 1)))}
              sub={`按最近${days}天平均`} accent="bg-gradient-to-b from-orange-500 to-amber-500" />
          </div>

          {/* 趋势图 */}
          <div className="card p-5">
            <h3 className="font-semibold text-[var(--foreground)] mb-4">📅 按天消耗趋势</h3>
            {(summary?.trend?.length) ? (
              <div className="flex items-end gap-1 h-32">
                {summary.trend.map((t) => (
                  <div key={t.date} className="flex-1 group relative flex flex-col justify-end min-w-[8px]"
                    title={`${t.date}: ${fmtTokens(t.total_tokens)} tokens / ${fmtCost(t.total_cost)}`}>
                    <div className="rounded-t bg-gradient-to-t from-indigo-600 to-violet-400 transition-all group-hover:brightness-125"
                      style={{ height: `${Math.max(4, (t.total_tokens / maxTrend) * 100)}%` }} />
                  </div>
                ))}
              </div>
            ) : <p className="text-xs text-[var(--muted-foreground)] text-center py-6">暂无数据 —— Agent 开始调用模型后这里会出现趋势柱状图</p>}
          </div>

          {/* 六维统计 */}
          <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
            <StatTable title="(a) 个人维度 — Agent 用量排行" icon="🤖" rows={byAgent} emptyHint="暂无 Agent 用量记录" />
            <StatTable title="(b) 部门维度" icon="🏬" rows={byDept} emptyHint="暂无部门归属数据" />
            <StatTable title="(c) 公司维度" icon="🏢" rows={byCompany} emptyHint="暂无公司用量记录" />
            <StatTable title="(d) 项目维度" icon="📂" rows={byProject} emptyHint="任务执行关联项目后可统计" />
            <StatTable title="(e) 任务维度 — Top 消耗" icon="📌" rows={byTask} emptyHint="暂无任务级用量记录" />
            <StatTable title="(f) 场景维度 — 学习/会议/任务…" icon="🎯" rows={byScenario} emptyHint="暂无场景分类数据" />
          </div>
        </div>
      )}

      {/* ===== Tab 2: 计费设置 ===== */}
      {tab === "pricing" && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* 设置表单 */}
          <div className="card p-5 space-y-3">
            <h3 className="font-semibold text-[var(--foreground)]">
              💰 {editingId ? "编辑计费标准" : "新增计费标准"}
            </h3>
            <label className="block">
              <span className="text-xs text-[var(--muted-foreground)] mb-1 block">选择模型</span>
              <select value={pf.model_id} disabled={!!editingId}
                onChange={(e) => setPf({ ...pf, model_id: e.target.value })} className="input w-full">
                <option value="">— 请选择 —</option>
                {models.map((m) => (<option key={m.id} value={m.id}>{m.name} ({m.provider_type})</option>))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-[var(--muted-foreground)] mb-1 block">计费方式</span>
              <div className="grid grid-cols-2 gap-2">
                <button onClick={() => setPf({ ...pf, billing_mode: "USAGE" })}
                  className={`px-3 py-2 rounded-lg text-sm transition-all ${pf.billing_mode === "USAGE"
                    ? "bg-indigo-600 text-white shadow-md" : "bg-white/[0.05] hover:bg-white/[0.09]"}`}>
                  按量计费
                </button>
                <button onClick={() => setPf({ ...pf, billing_mode: "SUBSCRIPTION" })}
                  className={`px-3 py-2 rounded-lg text-sm transition-all ${pf.billing_mode === "SUBSCRIPTION"
                    ? "bg-indigo-600 text-white shadow-md" : "bg-white/[0.05] hover:bg-white/[0.09]"}`}>
                  订阅制
                </button>
              </div>
            </label>

            {pf.billing_mode === "USAGE" ? (
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-[var(--muted-foreground)] mb-1 block">输入单价<br /><span className="opacity-60">元/百万token</span></span>
                  <input type="number" step="0.01" value={pf.input_price_per_m}
                    onChange={(e) => setPf({ ...pf, input_price_per_m: Number(e.target.value) || 0 })}
                    className="input w-full" />
                </label>
                <label className="block">
                  <span className="text-xs text-[var(--muted-foreground)] mb-1 block">输出单价<br /><span className="opacity-60">元/百万token</span></span>
                  <input type="number" step="0.01" value={pf.output_price_per_m}
                    onChange={(e) => setPf({ ...pf, output_price_per_m: Number(e.target.value) || 0 })}
                    className="input w-full" />
                </label>
              </div>
            ) : (
              <div className="space-y-2">
                <label className="block">
                  <span className="text-xs text-[var(--muted-foreground)] mb-1 block">包月费用（元）</span>
                  <input type="number" step="0.01" value={pf.monthly_fee}
                    onChange={(e) => setPf({ ...pf, monthly_fee: Number(e.target.value) || 0 })}
                    className="input w-full" />
                </label>
                <label className="block">
                  <span className="text-xs text-[var(--muted-foreground)] mb-1 block">每月包含 token 额度</span>
                  <input type="number" value={pf.included_tokens_per_month}
                    onChange={(e) => setPf({ ...pf, included_tokens_per_month: Number(e.target.value) || 0 })}
                    className="input w-full" placeholder="如 10000000" />
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <label className="block">
                    <span className="text-xs text-[var(--muted-foreground)] mb-1 block">超额输入单价</span>
                    <input type="number" step="0.01" value={pf.overage_input_per_m}
                      onChange={(e) => setPf({ ...pf, overage_input_per_m: Number(e.target.value) || 0 })}
                      className="input w-full" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-[var(--muted-foreground)] mb-1 block">超额输出单价</span>
                    <input type="number" step="0.01" value={pf.overage_output_per_m}
                      onChange={(e) => setPf({ ...pf, overage_output_per_m: Number(e.target.value) || 0 })}
                      className="input w-full" />
                  </label>
                </div>
              </div>
            )}

            <button onClick={savePricing}
              className="btn-primary w-full py-2.5 text-sm font-medium">
              {editingId ? "💾 更新标准" : "➕ 创建标准"}
            </button>
            {editingId && (
              <button onClick={() => { setEditingId(null); setPf({ model_id: "", billing_mode: "USAGE", input_price_per_m: 0, output_price_per_m: 0, monthly_fee: 0, overage_input_per_m: 0, overage_output_per_m: 0, included_tokens_per_month: 0 }); }}
                className="w-full text-xs text-[var(--muted-foreground)] hover:text-[var(--accent)]">
                取消编辑
              </button>
            )}
            <p className="text-[10px] text-[var(--muted-foreground)] pt-2 border-t border-[var(--border)] opacity-70">
              单价参考主流厂商报价口径（元/百万token）。历史账单按调用当时的单价快照结算，改价不影响已发生的记录。
            </p>
          </div>

          {/* 已配置列表 */}
          <div className="card p-5 lg:col-span-2">
            <h3 className="font-semibold text-[var(--foreground)] mb-3">📋 已配置的计费标准</h3>
            {pricings.length ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-[var(--muted-foreground)] border-b border-[var(--border)]">
                      <th className="pb-2 pr-3">模型</th>
                      <th className="pb-2 pr-3">方式</th>
                      <th className="pb-2 pr-3">输入价</th>
                      <th className="pb-2 pr-3">输出价</th>
                      <th className="pb-2 pr-3">月费</th>
                      <th className="pb-2 pr-3">状态</th>
                      <th className="pb-2">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pricings.map((p) => (
                      <tr key={p.id} className="border-b border-[var(--border)] last:border-0 hover:bg-white/[0.03]">
                        <td className="py-2.5 pr-3 font-medium text-[var(--foreground)]">{p.model_name}</td>
                        <td className="py-2.5 pr-3">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] ${p.billing_mode === "USAGE"
                            ? "bg-blue-500/15 text-blue-300" : "bg-violet-500/15 text-violet-300"}`}>
                            {p.billing_mode === "USAGE" ? "按量" : "订阅"}
                          </span>
                        </td>
                        <td className="py-2.5 pr-3">¥{p.input_price_per_m}/M</td>
                        <td className="py-2.5 pr-3">¥{p.output_price_per_m}/M</td>
                        <td className="py-2.5 pr-3">{p.billing_mode === "SUBSCRIPTION" ? `¥${p.monthly_fee}` : "-"}</td>
                        <td className="py-2.5 pr-3">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] ${p.enabled
                            ? "bg-emerald-500/15 text-emerald-300" : "bg-gray-500/15 text-gray-400"}`}>
                            {p.enabled ? "生效中" : "停用"}
                          </span>
                        </td>
                        <td className="py-2.5 space-x-2 whitespace-nowrap">
                          <button onClick={() => editPricing(p)} className="text-[var(--accent)] hover:underline">编辑</button>
                          <button onClick={() => delPricing(p)} className="text-red-400 hover:underline">删除</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="text-center py-10 text-[var(--muted-foreground)]">
                <p className="text-3xl mb-2 opacity-40">💰</p>
                <p className="text-sm">还没有配置任何计费标准</p>
                <p className="text-xs mt-1 opacity-70">左侧选择模型并填写单价——保存后系统开始按此标准估算每次调用的费用</p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ===== V5.5 P0：预算上限（到顶就停，不加价惩罚）===== */}
      {tab === "budget" && (
        <div className="space-y-4">
          <div className="card p-5">
            <h3 className="font-medium text-[var(--foreground)] mb-1">
              🛡 新建预算上限
            </h3>
            <p className="text-xs mb-3 text-[var(--muted)]">
              一人公司成本护栏：用量到顶 → 自动暂停（任务停下等老板调额），
              <b>不是 10 倍加价惩罚</b>。
            </p>
            <div className="flex flex-wrap gap-2 items-end">
              {/* V5.5 A4：范围（公司/部门/员工） */}
              <label className="text-xs text-[var(--muted)]">
                范围
                <select value={bform.scope_type}
                  onChange={(e) => setBform({ ...bform, scope_type: e.target.value, scope_id: "" })}
                  className="input !py-1.5 mt-1 block w-24">
                  <option value="company">🏢 公司</option>
                  <option value="department">📁 部门（虚拟账户）</option>
                  <option value="agent">👤 员工</option>
                </select>
              </label>
              {bform.scope_type === "department" && (
                <label className="text-xs text-[var(--muted)]">
                  部门
                  <select value={bform.scope_id}
                    onChange={(e) => setBform({ ...bform, scope_id: e.target.value })}
                    className="input !py-1.5 mt-1 block w-36">
                    <option value="">选择部门…</option>
                    {departments.map((d) => (
                      <option key={d.id} value={d.id}>{d.name}</option>
                    ))}
                  </select>
                </label>
              )}
              {bform.scope_type === "agent" && (
                <label className="text-xs text-[var(--muted)]">
                  员工
                  <select value={bform.scope_id}
                    onChange={(e) => setBform({ ...bform, scope_id: e.target.value })}
                    className="input !py-1.5 mt-1 block w-36">
                    <option value="">选择员工…</option>
                    {agents.map((a) => (
                      <option key={a.id} value={a.id}>{a.name}</option>
                    ))}
                  </select>
                </label>
              )}
              <label className="text-xs text-[var(--muted)]">
                Token 上限
                <input type="number" min={0} value={bform.limit_tokens}
                  onChange={(e) => setBform({ ...bform, limit_tokens: +e.target.value })}
                  placeholder="如 500000"
                  className="input w-32 !py-1.5 mt-1 block" />
              </label>
              <label className="text-xs text-[var(--muted)]">
                费用上限（¥）
                <input type="number" min={0} step="0.01" value={bform.limit_cost || ""}
                  onChange={(e) => setBform({ ...bform, limit_cost: +e.target.value })}
                  placeholder="如 100"
                  className="input w-28 !py-1.5 mt-1 block" />
              </label>
              <label className="text-xs text-[var(--muted)]">
                周期
                <select value={bform.period}
                  onChange={(e) => setBform({ ...bform, period: e.target.value })}
                  className="input !py-1.5 mt-1 block">
                  <option value="day">每天重置</option>
                  <option value="total">累计不重置</option>
                </select>
              </label>
              <button onClick={saveBudget} className="btn-primary !py-1.5 text-xs">
                ＋ 保存预算
              </button>
            </div>
          </div>

          <div className="card p-5">
            <h3 className="font-medium text-[var(--foreground)] mb-3">当前预算列表</h3>
            {budgets.length === 0 ? (
              <div className="text-center py-8 text-[var(--muted)]">
                <p className="text-3xl mb-2 opacity-40">🛡</p>
                <p className="text-sm">还没有预算上限</p>
              </div>
            ) : (
              <div className="space-y-2">
                {budgets.map((b: any) => (
                  <div key={b.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-xl px-3 py-2.5 border"
                    style={{
                      borderColor: b.exceeded ? "rgba(239,68,68,0.4)" : "var(--border)",
                      background: b.exceeded ? "rgba(239,68,68,0.06)" : "rgba(255,255,255,0.02)",
                    }}>
                    <div className="text-sm text-[var(--foreground)]">
                      <span className="font-medium">
                        {b.scope_type === "agent" ? "👤 员工级"
                          : b.scope_type === "department" ? "📁 部门级" : "🏢 公司级"}
                      </span>
                      <span className="ml-2 text-xs text-[var(--muted)]">
                        上限 {b.limit_tokens || "∞"} token{b.limit_cost ? ` / ¥${b.limit_cost}` : ""}
                        {" "}· {b.period === "day" ? "每日" : "累计"}
                      </span>
                      <div className="text-xs mt-0.5 text-[var(--muted)]">
                        已用 {b.used_tokens ?? "—"} token{b.used_cost != null ? ` / ¥${b.used_cost}` : ""}
                      </div>
                      {/* V5.5 A2：用量进度条（80% 起黄色预警，超了红色） */}
                      {b.usage_pct != null && !b.exceeded && (
                        <div className="mt-1.5 h-1.5 rounded-full overflow-hidden max-w-[220px]"
                          style={{ background: "rgba(255,255,255,0.06)" }}>
                          <div className="h-full rounded-full transition-all"
                            style={{
                              width: `${Math.min(b.usage_pct, 100)}%`,
                              background: b.usage_pct >= 80 ? "linear-gradient(90deg,#f59e0b,#ef4444)"
                                : "linear-gradient(90deg,#22d3ee,#34d399)",
                            }} />
                        </div>
                      )}
                      {b.warn && (
                        <div className="text-[11px] mt-1" style={{ color: "#fbbf24" }}>
                          ⚠ 已用 {b.usage_pct}%（≥80% 预警，到顶将自动暂停）
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {b.paused && (
                        <span className="text-xs px-2 py-0.5 rounded-full"
                          style={{ background: "rgba(239,68,68,0.15)", color: "#f87171" }}>
                          ⏸ 已自动暂停
                        </span>
                      )}
                      <button onClick={() => toggleBudget(b)}
                        className="btn-ghost text-xs !py-1">
                        {b.paused ? "▶ 恢复" : "⏸ 停用"}
                      </button>
                      <button onClick={() => deleteBudget(b)}
                        className="btn-ghost text-xs !py-1" style={{ color: "#f87171" }}>
                        🗑
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ===== Tab 3: 用量流水 ===== */}
      {tab === "logs" && (
        <div className="card p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-[var(--foreground)]">📜 最近 100 条调用流水</h3>
            <div className="flex items-center gap-3">
              <button
                onClick={async () => {
                  if (!cid) return flash("还没有公司，无法导出", "err");
                  try {
                    downloadURL(
                      await fetchBlobURL(`/tokens/usage-logs/export.csv?company_id=${cid}`),
                      "usage-logs.csv",
                    );
                  } catch (e: any) {
                    flash(`导出失败：${describeError(e)}`, "err");
                  }
                }}
                className="text-xs text-[var(--muted-foreground)] hover:text-[var(--accent)]"
              >⬇ 导出 CSV</button>
              <button onClick={() => refreshLogs()} className="text-xs text-[var(--muted-foreground)] hover:text-[var(--accent)]">⟳ 刷新</button>
            </div>
          </div>
          {logs.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-[var(--muted-foreground)] border-b border-[var(--border)]">
                    <th className="pb-2 pr-3">时间</th>
                    <th className="pb-2 pr-3">Agent</th>
                    <th className="pb-2 pr-3">场景</th>
                    <th className="pb-2 pr-3">模型</th>
                    <th className="pb-2 pr-3 text-right">输入</th>
                    <th className="pb-2 pr-3 text-right">输出</th>
                    <th className="pb-2 pr-3 text-right">合计</th>
                    <th className="pb-2 pr-3 text-right">费用</th>
                    <th className="pb-2 text-right">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.map((l) => (
                    <tr key={l.id} className="border-b border-[var(--border)] last:border-0 hover:bg-white/[0.03]">
                      <td className="py-2 pr-3 text-[var(--muted-foreground)] whitespace-nowrap">{formatLocalStamp(l.created_at)}</td>
                      <td className="py-2 pr-3 font-medium text-[var(--foreground)]">{l.agent_name}</td>
                      <td className="py-2 pr-3">
                        <span className="px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-300 text-[10px]">{l.scenario_label}</span>
                      </td>
                      <td className="py-2 pr-3 text-[var(--muted-foreground)]">{l.model_name}</td>
                      <td className="py-2 pr-3 text-right">{l.input_tokens.toLocaleString()}</td>
                      <td className="py-2 pr-3 text-right">{l.output_tokens.toLocaleString()}</td>
                      <td className="py-2 pr-3 text-right font-medium">{l.total_tokens.toLocaleString()}</td>
                      <td className="py-2 pr-3 text-right">{fmtCost(l.total_cost)}</td>
                      <td className="py-2 text-right text-[var(--muted-foreground)]">{(l.latency_ms / 1000).toFixed(1)}s</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-10 text-[var(--muted-foreground)]">
              <p className="text-3xl mb-2 opacity-40">📜</p>
              <p className="text-sm">还没有调用流水</p>
              <p className="text-xs mt-1 opacity-70">Agent 每次调用大模型都会在这里留下一条记录</p>
            </div>
          )}
        </div>
      )}

      {msg && (
        <div className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-xl shadow-2xl backdrop-blur-lg border text-sm animate-fade-up ${
          msgType === "err"
            ? "bg-red-950/80 border-red-500/30 text-red-300"
            : "bg-emerald-950/80 border-emerald-500/30 text-emerald-300"}`}>
          {msg}
        </div>
      )}
    </div>
  );
}
