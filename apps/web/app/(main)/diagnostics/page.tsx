"use client";
/**
 * 🩺 系统诊断（V5.4）：一键体检 + 修复建议。
 * 数据来自 GET /diagnostics（后端只读检查项）；失败项给出可执行的 fix 文案。
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/components/Toast";

interface Check { name: string; ok: boolean; detail: string; fix?: string; }
interface Report { ok: boolean; checked_at: string; checks: Check[]; }

export default function DiagnosticsPage() {
  const [report, setReport] = useState<Report | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true); setErr("");
    try {
      setReport(await api.get<Report>("/diagnostics"));
    } catch (e: any) {
      setErr(describe(e));
    } finally { setBusy(false); }
  };

  useEffect(() => { run(); }, []);

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

      {err && (
        <div className="card p-3 text-xs fade-up" style={{ borderColor: "rgba(255,80,80,.4)" }}>
          体检请求失败：{err}
          <button className="ml-2" onClick={() => notify("info", "请确认后端服务已启动")}>知道了</button>
        </div>
      )}
    </div>
  );
}

function describe(e: any): string {
  return e?.message || String(e);
}
