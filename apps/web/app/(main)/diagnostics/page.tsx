"use client";
/**
 * 🩺 系统诊断（V5.4）：一键体检 + 修复建议。
 * 数据来自 GET /diagnostics（后端只读检查项）；失败项给出可执行的 fix 文案。
 */
import { Fragment, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useResource } from "@/lib/useResource";
import { getInteractionStats, type InteractionStat } from "@/lib/perf";
import { notify } from "@/components/Toast";

interface Check { name: string; ok: boolean; detail: string; fix?: string; }
interface Report { ok: boolean; checked_at: string; checks: Check[]; }

export default function DiagnosticsPage() {
  // 体检报告是只读快照：交给统一数据层（缓存 + 回窗重取），
  // 「重新体检」= 强制刷新同一 key。
  const { data: report, error, loading: busy, refresh } = useResource<Report>(
    "diagnostics:report",
    () => api.get<Report>("/diagnostics"),
  );
  const [perf, setPerf] = useState<InteractionStat[]>([]);

  // 交互延迟样本只存在 sessionStorage，不是网络数据，仍由页面自己采
  useEffect(() => { setPerf(getInteractionStats()); }, []);

  const run = () => {
    setPerf(getInteractionStats());
    refresh();
  };

  const bad = report?.checks.filter((c) => !c.ok) || [];

  return (
    <div className="p-4 flex flex-col h-full gap-3 overflow-auto">
      <div className="flex items-center justify-between fade-up">
        <div>
          <h1 className="text-lg font-semibold">🩺 系统诊断</h1>
          <p className="text-xs mt-0.5" style={{ color: "var(--muted-foreground)" }}>
            {report ? `体检完成 ${report.checked_at} · ${report.ok ? "全部通过" : bad.length + " 项异常"}` : "体检中…"}
          </p>
        </div>
        <button onClick={run} disabled={busy}
          className="text-xs px-3 py-2 rounded-md card"
          style={{ borderColor: "var(--border)" }}>
          {busy ? "体检中…" : "🔄 重新体检"}
        </button>
      </div>

      {report && (
        <div className="card p-4 fade-up flex items-center gap-3"
             style={{ borderColor: report.ok ? "rgba(80,220,140,.35)" : "rgba(255,140,80,.4)" }}>
          <div className="text-2xl">{report.ok ? "✅" : "🛠"}</div>
          <div className="text-sm">
            {report.ok ? "全部检查项通过，系统健康。" :
              `${bad.length} 项异常：${bad.map((c) => c.name).join("、")}` }
          </div>
        </div>
      )}

      <div className="card p-4 fade-up flex flex-col gap-2.5 card-static">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium">⏱ 交互延迟</div>
          <span className="text-[10px]" style={{ color: "var(--muted-foreground)" }}>
            本会话 · 最近 200 条
          </span>
        </div>
        {perf.length === 0 ? (
          <div className="text-xs" style={{ color: "var(--muted-foreground)" }}>
            暂无样本，操作几页后再看
          </div>
        ) : (
          <div className="text-xs">
            <div className="grid gap-x-3 gap-y-1.5 tabular-nums"
                 style={{ gridTemplateColumns: "minmax(90px, 1fr) 52px 64px 64px 64px" }}>
              <span style={{ color: "var(--muted-foreground)" }}>指标</span>
              <span className="text-right" style={{ color: "var(--muted-foreground)" }}>样本</span>
              <span className="text-right" style={{ color: "var(--muted-foreground)" }}>P50</span>
              <span className="text-right" style={{ color: "var(--muted-foreground)" }}>P95</span>
              <span className="text-right" style={{ color: "var(--muted-foreground)" }}>最大</span>
              {perf.map((s) => (
                <Fragment key={s.name}>
                  <span className="truncate">{s.name}</span>
                  <span className="text-right" style={{ color: "var(--muted-foreground)" }}>{s.count}</span>
                  <span className="text-right">{fmtMs(s.p50)}</span>
                  <span className="text-right">{fmtMs(s.p95)}</span>
                  <span className="text-right">{fmtMs(s.max)}</span>
                </Fragment>
              ))}
            </div>
            <div className="mt-2 text-[10px]" style={{ color: "var(--muted-foreground)" }}>
              仅存在本标签页（sessionStorage），关闭即清空，不上报。
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-2 fade-up" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}>
        {report?.checks.map((c) => (
          <div key={c.name} className="card p-3 flex flex-col gap-1.5"
               style={{ borderColor: c.ok ? "var(--border)" : "rgba(255,140,80,.4)" }}>
            <div className="flex items-center gap-2 text-sm font-medium">
              <span>{c.ok ? "✓" : "✗"}</span>
              <span>{c.name}</span>
              {c.fix && !c.ok && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full"
                      style={{ background: "rgba(255,140,80,.15)", color: "#ff9d66" }}>需处理</span>
              )}
            </div>
            <div className="text-xs" style={{ color: "var(--muted-foreground)" }}>{c.detail}</div>
            {c.fix && !c.ok && (
              <div className="text-xs flex gap-1.5" style={{ color: "#ffb088" }}>
                <span>💡</span><span>{c.fix}</span>
              </div>
            )}
          </div>
        ))}
      </div>

      {error && !busy && (
        <div className="card p-3 text-xs fade-up" style={{ borderColor: "rgba(255,80,80,.4)" }}>
          体检请求失败：{error}
          <button className="ml-2" onClick={() => notify("info", "请确认后端服务已启动")}>知道了</button>
        </div>
      )}
    </div>
  );
}

/** 交互耗时展示：1s 以上换算成秒，10ms 以下保留一位小数 */
function fmtMs(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms < 10 ? ms.toFixed(1) : Math.round(ms)}ms`;
}
