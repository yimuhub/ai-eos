"use client";
import { useEffect, useState, useCallback } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { formatDuration, formatRate, type MetricsOverview } from "@/lib/types";
import { notify } from "@/components/Toast";

/* ============================================================
   绩效中心
   ------------------------------------------------------------
   这一页回答两个不同的问题，改造前只回答了一半：

   1. 「谁干得多、谁在拖后腿」   → 员工绩效排行（原有的快照功能）
   2. 「这套系统到底靠不靠谱」   → 任务/执行/模型调用的成功率、
                                  平均耗时、失败原因归因（本次新增）

   第 2 类数字以前在系统里完全不存在：只有 token 费用（投入），
   没有结果。于是"能不能直接拿去用"只能靠感觉。
   ============================================================ */

type Ranked = {
  agent_name: string; score: number;
  cost_total?: number; cost_per_task?: number; roi?: number | null;
  tasks_total: number; tasks_completed: number; tasks_failed: number;
  meeting_spoke_count: number; messages_sent: number;
};

type Snapshot = { period_end: string; agent_id: string; score: number };

/** 一个数字 + 说明的紧凑卡片 */
function MetricCard({ label, value, sub, tone = "normal" }: {
  label: string; value: string; sub?: string;
  tone?: "normal" | "good" | "bad" | "muted";
}) {
  const color = tone === "good" ? "#34d399" : tone === "bad" ? "#f87171"
    : tone === "muted" ? "var(--muted)" : "var(--foreground)";
  return (
    <div className="card">
      <div className="text-xs" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="text-[26px] font-bold tabular-nums mt-0.5" style={{ color }}>{value}</div>
      {sub && <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{sub}</div>}
    </div>
  );
}

/** 成功率的分档着色：>90% 绿、>70% 琥珀、其余红。
 *  没有样本（null）走 muted，绝不显示成 0%。 */
function rateTone(rate: number | null): "good" | "bad" | "muted" | "normal" {
  if (rate === null) return "muted";
  if (rate >= 0.9) return "good";
  if (rate >= 0.7) return "normal";
  return "bad";
}

export default function PerformancePage() {
  const cid = useActiveCompanyId();
  const [days, setDays] = useState(30);
  const [ranked, setRanked] = useState<Ranked[]>([]);
  const [history, setHistory] = useState<Snapshot[]>([]);
  const [busy, setBusy] = useState(false);
  // V5.5 C1：HR 看板（编制 / 成本 / 产出 / 考核分）
  const [hr, setHr] = useState<{
    headcount: { total: number; by_status: Record<string, number> };
    cost_month: { tokens: number; cost: number; top: { agent: string; tokens: number; cost: number }[] };
    output_30d: { total_tasks: number; top: { agent: string; tasks: number }[] };
    assessment_top: { agent: string; score: number }[];
    health: number;
  } | null>(null);
  // 修复：此前 loadHr() 直接写在渲染体内——每次渲染发一次请求且 setHr 新对象
  // 回写触发再渲染 → 无限请求风暴。改为 useEffect + useCallback，仅随 cid 变化拉取。
  const loadHr = useCallback(() => {
    if (!cid) return;
    api.get(`/performance/company/${cid}/hr-overview`).then(setHr).catch(() => {});
  }, [cid]);
  useEffect(() => { loadHr(); }, [loadHr]);
  const hrRefresh = useCallback(() => { loadHr(); }, [loadHr]);

  const { data: m, error: mErr, loading: mLoading, refresh: refreshMetrics } =
    useResource<MetricsOverview>(
      cid ? `metrics:${cid}:${days}` : null,
      () => api.get<MetricsOverview>(`/metrics/overview?days=${days}`),
    );

  async function snapshot() {
    if (!cid) return;
    setBusy(true);
    try {
      const r = await api.post<{ ranked: Ranked[] }>(`/performance/company/${cid}/snapshot`);
      setRanked(r.ranked || []);
      notify("success", `已生成快照，共 ${r.ranked?.length ?? 0} 名员工`);
    } catch (e) {
      notify("error", describeError(e, "生成绩效快照失败"));
    }
    setBusy(false);
  }

  async function loadHistory() {
    if (!cid) return;
    try {
      setHistory(await api.get<Snapshot[]>(`/performance/company/${cid}/history?limit=20`));
    } catch (e) {
      notify("error", describeError(e, "读取历史快照失败"));
    }
  }

  const maxDaily = Math.max(1, ...(m?.throughput_by_day ?? []).map((d) => d.completed + d.failed));

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-6">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">📊 绩效中心</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            系统可靠性度量 · 员工产出排行 · 失败归因
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}
            className="input w-auto !py-2 text-xs">
            {[7, 14, 30, 90].map((d) => (<option key={d} value={d}>最近 {d} 天</option>))}
          </select>
          <button onClick={() => refreshMetrics()} className="btn-ghost text-xs">刷新</button>
        </div>
      </div>

      {!cid && <p className="text-sm" style={{ color: "var(--muted)" }}>加载中…</p>}

      {/* V5.5 C1：HR 看板（一人公司老板视角：编制/成本/产出/考核） */}
      {hr && (
        <div className="card card-static p-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-medium text-sm">🏛 HR 看板（公司四象限）</h3>
              <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                编制 / 本月成本 / 30 天产出 / 考核分 · 健康度 {Math.round(hr.health * 100)}%
              </p>
            </div>
            <button onClick={hrRefresh} className="text-xs text-[var(--muted)] hover:text-[var(--accent)]">⟳ 刷新</button>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <div className="rounded-lg p-3" style={{ background: "rgba(16,185,129,0.06)", border: "1px solid rgba(16,185,129,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#34d399" }}>👥 编制</div>
              <div className="text-xl font-bold">{hr.headcount.total} 人</div>
              <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                {Object.entries(hr.headcount.by_status).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}
              </div>
            </div>
            <div className="rounded-lg p-3" style={{ background: "rgba(245,158,11,0.06)", border: "1px solid rgba(245,158,11,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#fbbf24" }}>💰 本月成本</div>
              <div className="text-xl font-bold">¥{hr.cost_month.cost}</div>
              <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                {hr.cost_month.tokens} token · Top：{hr.cost_month.top[0]?.agent || "—"}
              </div>
            </div>
            <div className="rounded-lg p-3" style={{ background: "rgba(34,211,238,0.06)", border: "1px solid rgba(34,211,238,0.18)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#22d3ee" }}>📦 30 天产出</div>
              <div className="text-xl font-bold">{hr.output_30d.total_tasks} 任务</div>
              <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                Top：{hr.output_30d.top[0] ? `${hr.output_30d.top[0].agent}（${hr.output_30d.top[0].tasks}）` : "—"}
              </div>
            </div>
            <div className="rounded-lg p-3" style={{ background: "rgba(99,102,241,0.08)", border: "1px solid rgba(99,102,241,0.2)" }}>
              <div className="text-xs font-semibold mb-2" style={{ color: "#a5b4fc" }}>🏆 考核 Top</div>
              <div className="space-y-0.5">
                {(hr.assessment_top.length ? hr.assessment_top : [{ agent: "暂无快照", score: 0 }]).map((s, i) => (
                  <div key={i} className="text-[11px]" style={{ color: "var(--foreground)" }}>
                    {i + 1}. {s.agent} <span style={{ color: "var(--muted)" }}>{Math.round(s.score)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {mErr && (
        <div className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
          <span>🔌</span>
          <span className="flex-1">读取度量失败：{mErr}</span>
          <button onClick={() => refreshMetrics()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

      {/* ===== 可靠性概览 ===== */}
      <section className="space-y-3">
        <h2 className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--muted)" }}>
          可靠性 · 最近 {days} 天
        </h2>
        {m ? (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
              <MetricCard label="任务成功率" value={formatRate(m.tasks.success_rate)}
                tone={rateTone(m.tasks.success_rate)}
                sub={m.tasks.success_rate === null
                  ? "暂无已完成/失败的任务"
                  : `完成 ${m.tasks.completed} · 失败 ${m.tasks.failed}`} />
              <MetricCard label="执行成功率" value={formatRate(m.executions.success_rate)}
                tone={rateTone(m.executions.success_rate)}
                sub={`共 ${m.executions.total} 次员工执行`} />
              <MetricCard label="模型调用成功率" value={formatRate(m.model_calls.success_rate)}
                tone={rateTone(m.model_calls.success_rate)}
                sub={m.model_calls.total ? `共 ${m.model_calls.total} 次调用` : "还没有调用记录"} />
              <MetricCard label="平均任务耗时" value={formatDuration(m.executions.avg_duration_seconds)}
                tone="muted" sub="从开始执行到出结论" />
              <MetricCard label="平均模型延迟"
                value={m.model_calls.avg_latency_ms === null ? "—" : `${m.model_calls.avg_latency_ms} ms`}
                tone="muted" sub="偏高通常意味着供应商侧排队" />
            </div>

            {m.sampled?.truncated && (
              <p className="text-[11px]" style={{ color: "#fcd34d" }}>
                ⚠️ 样本量超过 {m.sampled.limit} 条上限，以上为抽样结论，不是全量精确值。
              </p>
            )}
            {m.tasks.total === 0 && (
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                最近 {days} 天还没有任务记录——先下达一个任务，这里才会有数字。
                （注意：显示「—」表示没有样本，不是成功率为 0。）
              </p>
            )}
          </>
        ) : (
          !mErr && <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="card" style={{ opacity: mLoading ? 0.5 : 1 }}>
                <div className="text-xs" style={{ color: "var(--muted)" }}>加载中…</div>
                <div className="text-[26px] font-bold mt-0.5">—</div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ===== 失败归因 + 按天吞吐 ===== */}
      {m && (m.failure_reasons.length > 0 || m.throughput_by_day.length > 0) && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {m.failure_reasons.length > 0 && (
            <div className="card card-static">
              <h2 className="text-sm font-semibold mb-3">失败原因 Top</h2>
              <div className="space-y-2">
                {m.failure_reasons.map((r) => {
                  const pct = Math.round((r.count / Math.max(1, m.tasks.failed + m.executions.failed)) * 100);
                  return (
                    <div key={r.reason}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="truncate pr-2" title={r.reason}>{r.reason}</span>
                        <span className="tabular-nums shrink-0" style={{ color: "var(--muted)" }}>{r.count} 次</span>
                      </div>
                      <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
                        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: "linear-gradient(90deg,#f87171,#fb923c)" }} />
                      </div>
                    </div>
                  );
                })}
              </div>
              {m.failure_by_agent.length > 0 && (
                <div className="mt-4 pt-3 border-t" style={{ borderColor: "var(--border)" }}>
                  <p className="text-xs mb-2" style={{ color: "var(--muted)" }}>失败集中在谁身上</p>
                  <div className="flex flex-wrap gap-1.5">
                    {m.failure_by_agent.map((a) => (
                      <span key={a.agent_id} className="text-[11px] px-2 py-1 rounded-lg"
                        style={{ background: "rgba(248,113,113,0.12)", color: "#fca5a5" }}>
                        {a.agent_name} · {a.failed} 次
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {m.throughput_by_day.length > 0 && (
            <div className="card card-static">
              <h2 className="text-sm font-semibold mb-3">每日产出（绿=完成 / 红=失败）</h2>
              <div className="flex items-end gap-1 h-40 overflow-x-auto pb-1">
                {m.throughput_by_day.map((d) => {
                  const total = d.completed + d.failed;
                  return (
                    <div key={d.date} className="flex flex-col items-center gap-1 shrink-0"
                      title={`${d.date}：完成 ${d.completed}，失败 ${d.failed}`}>
                      <div className="flex flex-col justify-end" style={{ height: 120 }}>
                        {d.failed > 0 && (
                          <div style={{ height: `${(d.failed / maxDaily) * 120}px`, background: "#f87171", width: 14, borderRadius: "3px 3px 0 0" }} />
                        )}
                        {d.completed > 0 && (
                          <div style={{ height: `${(d.completed / maxDaily) * 120}px`, background: "#34d399", width: 14, borderRadius: d.failed ? 0 : "3px 3px 0 0" }} />
                        )}
                        {total === 0 && <div style={{ height: 2, background: "var(--border)", width: 14 }} />}
                      </div>
                      <span className="text-[9px] tabular-nums" style={{ color: "var(--muted)" }}>
                        {d.date.slice(5)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ===== 员工绩效排行 ===== */}
      <section className="space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--muted)" }}>
            员工绩效排行
          </h2>
          <div className="flex gap-2">
            <button onClick={snapshot} disabled={!cid || busy} className="btn-primary !py-1.5 text-xs disabled:opacity-40">
              {busy ? "生成中…" : "生成绩效快照"}
            </button>
            <button onClick={loadHistory} disabled={!cid} className="btn-ghost !py-1.5 text-xs disabled:opacity-40">
              查看历史快照
            </button>
          </div>
        </div>

        <p className="text-[11px]" style={{ color: "var(--muted)" }}>
          排行需要先「生成绩效快照」——它会把当前一段时间内每名员工的任务完成情况固化成一条记录。
        </p>

        <div className="space-y-2">
          {ranked.map((r, i) => (
            <div key={`${r.agent_name}-${i}`} className="card flex items-center gap-4">
              <span className={`text-xl font-bold w-8 shrink-0 ${i === 0 ? "text-yellow-500" : i === 1 ? "text-[var(--muted)]" : "text-orange-300"}`}>
                {i + 1}
              </span>
              <div className="flex-1 min-w-0">
                <b className="text-sm">{r.agent_name}</b>
                <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  任务 {r.tasks_total} · 完成 {r.tasks_completed} · 失败 {r.tasks_failed}
                  {" · "}会议发言 {r.meeting_spoke_count} · 消息 {r.messages_sent}
                </div>
                {/* V5.6.1 ROI 考核维度（学 TheAgentCompany） */}
                {(r.cost_total != null || r.roi != null) && (
                  <div className="text-xs mt-0.5 flex flex-wrap gap-2" style={{ color: "var(--muted)" }}>
                    <span>💸 成本 ¥{(r.cost_total ?? 0).toFixed(2)}{r.cost_per_task ? `（¥${r.cost_per_task}/件）` : ""}</span>
                    {r.roi != null && (
                      <span style={{ color: (r.roi >= 0 ? "#34d399" : "#f87171") }}>
                        📈 ROI {(r.roi * 100).toFixed(0)}%{r.roi < 0 ? "（亏损）" : ""}
                      </span>
                    )}
                  </div>
                )}
              </div>
              <div className="text-2xl font-bold tabular-nums shrink-0">{r.score}</div>
            </div>
          ))}
          {!ranked.length && (
            <div className="card card-static text-center py-10 text-xs" style={{ color: "var(--muted)" }}>
              还没有绩效数据，点击「生成绩效快照」看看谁在干活
            </div>
          )}
        </div>

        {history.length > 0 && (
          <div className="card card-static">
            <h3 className="text-xs font-semibold mb-2" style={{ color: "var(--muted)" }}>历史快照（最近 20 条）</h3>
            <div className="space-y-1 text-xs">
              {history.map((h, i) => (
                <div key={i} className="flex justify-between tabular-nums" style={{ color: "var(--muted)" }}>
                  <span>{h.period_end?.slice(0, 16).replace("T", " ")} · 员工 {h.agent_id?.slice(0, 8)}</span>
                  <span className="text-[var(--foreground)]">{h.score}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
