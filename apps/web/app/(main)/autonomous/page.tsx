"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";

type Agent = { id: string; name: string; role?: string; department_id?: string | null };
type Dept = { id: string; name: string };
type Conn = { id: string; name: string; provider: string };
type ScopeRow = {
  id: string; scope_type: string; scope_id: string | null;
  name: string; level: number; emergency_stopped: boolean;
  limits: { cost: number | null; time_min: number | null; actions: number | null };
};
type LogItem = {
  id: string; scope_type: string; level: number;
  why?: string | null; summary?: string | null; actions: number;
  cost_estimate?: number | null; human_override?: boolean; created_at?: string | null;
};

const LEVELS = [
  { v: 0, name: "L0 · 人工确认", desc: "所有动作需老板确认后执行", icon: "🛡️" },
  { v: 1, name: "L1 · 低频抽查", desc: "大部分自己做，每5次抽查1次", icon: "👀" },
  { v: 2, name: "L2 · 均衡模式", desc: "常规事务自治，关键节点上报", icon: "⚖️" },
  { v: 3, name: "L3 · 高度自治", desc: "每10次动作才抽查1次", icon: "🚀" },
  { v: 4, name: "L4 · 全自动", desc: "完全放手（需设限额兜底）", icon: "♾️" },
];
const LEVEL_BADGE: Record<number, string> = {
  0: "bg-gray-500/15 text-gray-300",
  1: "bg-blue-500/15 text-blue-300",
  2: "bg-indigo-500/15 text-indigo-300",
  3: "bg-violet-500/15 text-violet-300",
  4: "bg-fuchsia-500/15 text-fuchsia-300",
};

export default function AutonomousPage() {
  // 激活公司可订阅：切公司后作用域规则/日志/部门自动跟随，不再用 `/companies → cs[0]` 快照。
  const cid = useActiveCompanyId() ?? "";
  const [companyLv, setCompanyLv] = useState<number>(2);
  const [stopped, setStopped] = useState(false);
  const [scopes, setScopes] = useState<ScopeRow[]>([]);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [depts, setDepts] = useState<Dept[]>([]);
  const [connections, setConnections] = useState<Conn[]>([]);

  // 配置表单：作用域类型 → 对象 → 参数
  const [scopeType, setScopeType] = useState<"COMPANY" | "DEPARTMENT" | "AGENT">("AGENT");
  const [targetId, setTargetId] = useState("");
  const [confLevel, setConfLevel] = useState(2);
  const [maxParallel, setMaxParallel] = useState(1);
  const [rateLimit, setRateLimit] = useState(10);

  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"ok" | "err">("ok");
  const [busy, setBusy] = useState("");

  const flash = (t: string, type: "ok" | "err" = "ok") => {
    setMsg(t); setMsgType(type);
    setTimeout(() => setMsg(""), 4000);
  };

  /**
   * 六块数据一次并发取回，交给统一数据层缓存/去重/聚焦重取。
   * 任一项失败退回空值（与原来的 Promise.allSettled 逐项兜底一致），
   * 不会因为一个口径挂了整页空白；settings 失败时用 null 表示"本次没取到"，
   * 避免用默认值覆盖用户刚设置的本地状态。
   */
  const { data: bundle, refresh: load } = useResource(
    cid ? `autonomous:${cid}` : null,
    async () => {
      const [s, ov, l, a, d, c] = await Promise.allSettled([
        api.get("/governance/autonomous-settings"),
        api.get("/governance/autonomous-overview"),
        api.get("/governance/autonomous-logs?limit=30"),
        api.get("/agents"),
        api.get(`/companies/${cid}/departments`),
        api.get("/external-agents"),
      ]);
      return {
        cid,
        level: s.status === "fulfilled" ? (s.value?.level ?? 2) : null,
        stopped: s.status === "fulfilled" ? !!s.value?.emergency_stopped : null,
        scopes: ov.status === "fulfilled" && Array.isArray(ov.value) ? ov.value : [],
        logs: l.status === "fulfilled" && Array.isArray(l.value) ? l.value : [],
        agents: a.status === "fulfilled" && Array.isArray(a.value) ? a.value : [],
        depts: d.status === "fulfilled" && Array.isArray(d.value)
          ? (d.value as any[]).map((x) => ({ id: x.id, name: x.name }))
          : [],
        connections: c.status === "fulfilled" ? c.value?.connections || [] : [],
      };
    },
  );
  // 切公司时旧 bundle 先不渲染，避免规则/日志短暂显示上一家公司的数据。
  const active = bundle && bundle.cid === cid ? bundle : null;

  useEffect(() => {
    if (!active) return;
    if (active.level !== null) setCompanyLv(active.level);
    if (active.stopped !== null) setStopped(active.stopped);
    setScopes(active.scopes);
    setLogs(active.logs);
    setAgents(active.agents);
    setDepts(active.depts);
    setConnections(active.connections);
  }, [active]);

  /* ---------- 公司级 ---------- */
  async function setCompany(level: number) {
    setBusy(`co-${level}`);
    try {
      await api.put("/governance/autonomous-settings", {
        level, scope_type: "COMPANY",
        cost_limit: level >= 4 ? 0.5 : null,
      });
      setCompanyLv(level);
      flash(`✅ 全公司自治等级已设为 ${LEVELS[level].name}`);
      load();
    } catch (e: any) { flash(e.message || String(e), "err"); }
    finally { setBusy(""); }
  }

  /* ---------- 部门 / Agent 级 ---------- */
  async function saveScoped() {
    if (scopeType !== "COMPANY" && !targetId) return flash("请先选择具体对象", "err");
    setBusy("scoped");
    try {
      await api.put("/governance/autonomous-settings", {
        level: confLevel,
        scope_type: scopeType,
        scope_id: scopeType === "COMPANY" ? null : targetId,
        cost_limit: confLevel >= 4 ? Math.max(rateLimit * 0.05, 0.5) : null,
        action_limit: maxParallel,
        time_limit_minutes: null,
      });
      flash(`✅ 已保存：${scopeType === "DEPARTMENT" ? "部门" : scopeType === "AGENT" ? "Agent" : "公司"} · ${LEVELS[confLevel].name}`);
      load();
    } catch (e: any) { flash(e.message || String(e), "err"); }
    finally { setBusy(""); }
  }

  async function removeScoped(row: ScopeRow) {
    if (!(await confirmAction({
      title: `确定清除「${row.name}」的专属设置？`,
      description: "清除后该对象将回落到公司默认的自治等级，不会停止它当前正在执行的任务。",
      danger: false,
      confirmText: "清除",
    }))) return;
    if (!cid) { flash("未识别到当前公司，请先到「公司管理」选择公司", "err"); return; }
    setBusy(`rm-${row.id}`);
    try {
      const r = await api.del(`/governance/autonomous-overview/${row.id}?company_id=${cid}`);
      flash(`✅ ${r?.message || "已清除，该对象回落到公司默认"}`);
      load();
    } catch (e: any) {
      flash(e.message || "清除失败", "err");
    } finally { setBusy(""); }
  }

  async function emergencyStop() {
    if (!(await confirmAction({
      title: "确定全局紧急停止？",
      description: "这是整个系统的总闸。所有 Agent 的自治行为会被立刻冻结。",
      bullets: [
        "正在执行的自治任务将被中止",
        "在你手动恢复之前，没有任何 Agent 会自己开工",
        "已产出的成果与记忆不受影响",
      ],
      confirmText: "紧急停止",
    }))) return;
    setBusy("stop");
    try {
      await api.post("/governance/emergency-stop", {});
      setStopped(true);
      flash("🛑 已全局紧急停止"); load();
    } catch (e: any) { flash(e.message || String(e), "err"); }
    finally { setBusy(""); }
  }

  async function resume() {
    setBusy("resume");
    try {
      await api.post("/governance/emergency-resume", {});
      setStopped(false);
      flash("✅ 已恢复正常运行"); load();
    } catch (e: any) { flash(e.message || String(e), "err"); }
    finally { setBusy(""); }
  }

  const agentOptions = agents;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-4">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-[var(--foreground)] flex items-center gap-2">
            <span className="pulse-dot" /> 🤖 自治模式
          </h1>
          <p className="text-sm text-[var(--muted-foreground)] mt-1">
            三级作用域控制：<b className="text-[var(--foreground)]">整家公司 → 某个部门 → 某个 Agent</b>，越具体的设置优先级越高
          </p>
        </div>
        {stopped && (
          <span className="bg-red-500/15 text-red-400 border border-red-500/30 px-3 py-1 rounded-full text-xs font-medium animate-pulse">
            ⛔ 紧急停止中
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ===== ① 公司级 ===== */}
        <div className="card p-5 space-y-3 transition-all hover-glow">
          <div className="flex items-center gap-2">
            <span className="w-6 h-6 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 text-white text-xs font-bold flex items-center justify-center">1</span>
            <h3 className="font-semibold text-[var(--foreground)]">🏢 整个公司</h3>
            <span className="ml-auto text-xs text-[var(--muted-foreground)]">所有部门的默认值</span>
          </div>
          <div className="space-y-1.5">
            {LEVELS.map((l) => (
              <button key={l.v} onClick={() => setCompany(l.v)} disabled={!!busy}
                className={`w-full text-left px-3 py-2.5 rounded-lg transition-all duration-200 ${
                  companyLv === l.v
                    ? "bg-gradient-to-r from-indigo-600 to-violet-600 text-white shadow-lg shadow-indigo-500/25"
                    : "bg-white/[0.04] hover:bg-white/[0.08] text-[var(--foreground)] hover:translate-x-1"
                } ${busy === `co-${l.v}` ? "opacity-50 pointer-events-none" : ""}`}>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <span>{l.icon}</span>{l.name}
                  {companyLv === l.v && <span className="ml-auto text-xs">✓ 当前</span>}
                </div>
                <p className={`text-xs mt-0.5 ml-6 ${companyLv === l.v ? "text-white/70" : "text-[var(--muted-foreground)]"}`}>{l.desc}</p>
              </button>
            ))}
          </div>
          <div className="pt-2 border-t border-[var(--border)]">
            {stopped ? (
              <button onClick={resume} disabled={!!busy}
                className="btn-primary w-full !bg-none bg-gradient-to-r from-emerald-600 to-green-500 py-2.5 rounded-lg text-sm font-medium text-white">
                ▶ 恢复运行
              </button>
            ) : (
              <button onClick={emergencyStop} disabled={!!busy}
                className="w-full py-2.5 rounded-lg text-sm font-medium text-white bg-gradient-to-r from-red-700 to-red-500 hover:brightness-110 shadow-lg shadow-red-500/20 disabled:opacity-50">
                {busy === "stop" ? "处理中..." : "🛑 全局紧急停止"}
              </button>
            )}
          </div>
        </div>

        {/* ===== ②③ 部门 / Agent 级 ===== */}
        <div className="card p-5 space-y-3 transition-all hover-glow">
          <div className="flex items-center gap-2">
            <span className="w-6 h-6 rounded-full bg-gradient-to-br from-violet-500 to-fuchsia-600 text-white text-xs font-bold flex items-center justify-center">2·3</span>
            <h3 className="font-semibold text-[var(--foreground)]">🎯 部门 / 单个 Agent 定制</h3>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] -mt-1">
            给某个部门或某个 Agent 单独设等级；未单独设置的自动沿用上面的公司默认。
          </p>

          <label className="block">
            <span className="text-xs text-[var(--muted-foreground)] mb-1 block">① 选择作用范围</span>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => { setScopeType("DEPARTMENT"); setTargetId(""); }}
                className={`px-3 py-2 rounded-lg text-sm transition-all ${scopeType === "DEPARTMENT"
                  ? "bg-indigo-600 text-white shadow-md shadow-indigo-500/25" : "bg-white/[0.05] hover:bg-white/[0.09] text-[var(--foreground)]"}`}>
                🏬 按部门
              </button>
              <button onClick={() => { setScopeType("AGENT"); setTargetId(""); }}
                className={`px-3 py-2 rounded-lg text-sm transition-all ${scopeType === "AGENT"
                  ? "bg-indigo-600 text-white shadow-md shadow-indigo-500/25" : "bg-white/[0.05] hover:bg-white/[0.09] text-[var(--foreground)]"}`}>
                🤖 按单个 Agent
              </button>
            </div>
          </label>

          <label className="block">
            <span className="text-xs text-[var(--muted-foreground)] mb-1 block">
              ② 选择{scopeType === "DEPARTMENT" ? "部门" : "Agent"}
            </span>
            <select value={targetId} onChange={(e) => setTargetId(e.target.value)} className="input w-full">
              <option value="">— 请选择 —</option>
              {scopeType === "DEPARTMENT"
                ? depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))
                : agentOptions.map((a) => (<option key={a.id} value={a.id}>{a.name}{a.role ? ` (${a.role})` : ""}</option>))}
            </select>
            {(scopeType === "DEPARTMENT" ? !depts.length : !agentOptions.length) && (
              <p className="text-xs text-amber-400/80 mt-1">列表为空，请先在对应页面创建。</p>
            )}
          </label>

          <label className="block">
            <span className="text-xs text-[var(--muted-foreground)] mb-1 block">③ 设置自治等级</span>
            <select value={confLevel} onChange={(e) => setConfLevel(Number(e.target.value))} className="input w-full">
              {LEVELS.map((l) => (<option key={l.v} value={l.v}>{l.icon} {l.name}</option>))}
            </select>
          </label>

          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-[var(--muted-foreground)] mb-1 block">最大并行任务数</span>
              <input type="number" min={1} max={10} value={maxParallel}
                onChange={(e) => setMaxParallel(Number(e.target.value) || 1)} className="input w-full" />
            </label>
            <label className="block">
              <span className="text-xs text-[var(--muted-foreground)] mb-1 block">限速 / 分钟</span>
              <input type="number" min={1} max={100} value={rateLimit}
                onChange={(e) => setRateLimit(Number(e.target.value) || 10)} className="input w-full" />
            </label>
          </div>

          <button onClick={saveScoped} disabled={!!busy}
            className="btn-primary w-full py-2.5 text-sm font-medium disabled:opacity-50">
            {busy === "scoped" ? "保存中..." : "💾 保存定制规则"}
          </button>
        </div>
      </div>

      {/* ===== 已生效的定制规则一览 ===== */}
      <div className="card p-5 transition-all hover-glow">
        <h3 className="font-semibold text-[var(--foreground)] mb-3">📐 当前生效的作用域规则</h3>
        <div className="space-y-2">
          {scopes.filter((r) => r.scope_type !== "COMPANY").map((r) => (
            <div key={r.id} className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-white/[0.03] border border-[var(--border)]">
              <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${LEVEL_BADGE[r.level] || ""}`}>
                L{r.level}
              </span>
              <span className="text-xs text-[var(--muted-foreground)]">
                {r.scope_type === "AGENT" ? "🤖 Agent" : "🏬 部门"}
              </span>
              <span className="text-sm text-[var(--foreground)] font-medium">{r.name}</span>
              <span className="text-xs text-[var(--muted-foreground)] ml-auto hidden sm:inline">
                并行 {r.limits.actions ?? "-"} · 限速 {r.limits.cost != null ? `${r.limits.cost}/min` : "-"}
              </span>
              <button onClick={() => removeScoped(r)} disabled={!!busy} title="清除该定制"
                className="text-xs text-[var(--muted-foreground)] hover:text-red-400 ml-2">✕</button>
            </div>
          ))}
          <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-indigo-500/[0.06] border border-indigo-500/20">
            <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${LEVEL_BADGE[companyLv] || ""}`}>L{companyLv}</span>
            <span className="text-xs text-[var(--muted-foreground)]">🏢 公司默认</span>
            <span className="text-sm text-[var(--foreground)] font-medium">其余全部对象沿用此等级</span>
          </div>
          {!scopes.filter((r) => r.scope_type !== "COMPANY").length && (
            <p className="text-xs text-[var(--muted-foreground)] px-1">
              还没有部门/Agent 级定制 —— 上方「②③」面板创建第一条规则即可。
            </p>
          )}
        </div>
      </div>

      {/* ===== 审计日志 + 外部协作 ===== */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-5 lg:col-span-2 transition-all hover-glow flex flex-col">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-[var(--foreground)]">📋 执行审计日志</h3>
            <button onClick={load} className="text-xs text-[var(--muted-foreground)] hover:text-[var(--accent)]">⟳ 刷新</button>
          </div>
          <div className="text-xs space-y-2 overflow-auto max-h-[320px] pr-1 flex-1">
            {logs.map((l) => (
              <div key={l.id} className="border-b border-[var(--border)] pb-2 last:border-0">
                <div className="flex items-center gap-1.5 flex-wrap">
                  {l.human_override && <span className="px-1.5 py-0.5 rounded bg-orange-500/15 text-orange-400 text-[10px]">人工干预</span>}
                  {l.why === "EMERGENCY_STOP" && <span className="px-1.5 py-0.5 rounded bg-red-500/15 text-red-400 text-[10px]">紧急停止</span>}
                  {l.why === "CONFIG_UPDATE" && <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400 text-[10px]">配置变更</span>}
                  <span className="text-[var(--muted-foreground)]">{l.created_at?.slice(0, 16).replace("T", " ")}</span>
                </div>
                <div className="mt-1 text-[var(--foreground)]">
                  {l.summary || l.why || `${l.scope_type} · L${l.level}`}
                  {l.actions > 0 && <span className="text-[var(--muted-foreground)]"> · {l.actions} 个动作</span>}
                </div>
              </div>
            ))}
            {!logs.length && (
              <div className="text-center py-8 text-[var(--muted-foreground)]">
                <p className="text-2xl mb-2 opacity-40">📭</p>
                <p>暂无审计日志</p>
                <p className="text-[10px] mt-1 opacity-60">调整等级或保存配置后会生成记录</p>
              </div>
            )}
          </div>
        </div>

        {/* 操作指南 */}
        <div className="card p-5 transition-all hover-glow">
          <h3 className="font-semibold text-[var(--foreground)] mb-3">📖 快速上手</h3>
          <ol className="text-xs text-[var(--muted-foreground)] space-y-2.5 list-decimal list-inside leading-relaxed">
            <li><b className="text-[var(--foreground)]">先设公司默认</b>（左上）：一般 L1~L2，稳妥起步。</li>
            <li><b className="text-[var(--foreground)]">给能干的部门加速</b>：选「按部门」→ 如「新媒体部」→ 设 L3，整个部门一起提速。</li>
            <li><b className="text-[var(--foreground)]">给核心 Agent 放权</b>：选「按单个 Agent」→ 如「CEO Agent」→ 设 L4 + 限额，只放权这一个。</li>
            <li><b className="text-[var(--foreground)]">出事一键刹车</b>：点「🛑 全局紧急停止」，所有自治立即冻结；处理完点「恢复」。</li>
            <li><b className="text-[var(--foreground)]">事后查日志</b>：每次等级调整、配置变更、人工干预都会记录在下方。</li>
          </ol>
          <p className="text-[10px] text-[var(--muted-foreground)] mt-3 pt-3 border-t border-[var(--border)] opacity-70">
            优先级：Agent &gt; 部门 &gt; 公司。越具体的规则越先生效。
          </p>
        </div>
      </div>

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
