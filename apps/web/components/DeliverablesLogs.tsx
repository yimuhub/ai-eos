"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/* ============================================================
   交付成果与日志面板（项目详情 / 任务详情共用）
   - 成果列表：点击弹出预览窗口
   - 项目日志：Agent 沟通/想法，支持导出 MD
   ============================================================ */

type Asset = {
  id: string; name: string; type: string; size: number;
  version: number; creator_type?: string; created_at: string;
};
type LogEntry = {
  ts: string; kind: string; who: string;
  type: string; title: string; content: string;
};

export function DeliverablesPanel({ projectId }: { projectId: string }) {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [preview, setPreview] = useState<{ name: string; content: string } | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.get(`/projects/${projectId}/deliverables`);
      setAssets(Array.isArray(data) ? data : []);
    } catch { setAssets([]); }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  async function openPreview(a: Asset) {
    setLoadingPreview(true);
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/assets/${a.id}`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
      const text = await r.text();
      setPreview({ name: a.name, content: text });
    } catch (e: any) {
      setPreview({ name: a.name, content: `预览失败：${e.message}` });
    }
    setLoadingPreview(false);
  }

  return (
    <div className="card card-static">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-sm">📦 交付成果（{assets.length}）</h2>
        <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/12 text-emerald-300">
          点击预览
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {assets.map((a) => (
          <button key={a.id} onClick={() => openPreview(a)}
            className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border hover:border-indigo-500/40"
            style={{ background: "var(--surface-2)", borderColor: "var(--border)" }}>
            <div className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: "rgba(99,102,241,0.15)", color: "#a5b4fc" }}>📄</div>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium truncate">{a.name}</div>
              <div className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                v{a.version} · {(a.size / 1024).toFixed(1)}KB
              </div>
            </div>
          </button>
        ))}
        {!assets.length && (
          <p className="text-xs text-center py-6 col-span-full" style={{ color: "var(--muted)" }}>
            暂无交付成果 · 任务完成后自动归档到这里
          </p>
        )}
      </div>

      {/* 预览弹窗 */}
      {preview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.7)", backdropFilter: "blur(6px)" }}
          onClick={() => setPreview(null)}>
          <div className="w-full max-w-3xl max-h-[85vh] rounded-2xl overflow-hidden flex flex-col animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid var(--border)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="px-5 py-3.5 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <h3 className="font-bold text-sm truncate">📄 {preview.name}</h3>
              <div className="flex gap-2 items-center shrink-0">
                <a href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/assets/${assets.find((x) => x.name === preview.name)?.id}/download`}
                  download
                  className="btn-primary !py-1.5 !px-3 text-xs">下载</a>
                <button onClick={() => setPreview(null)} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
              </div>
            </div>
            <pre className="flex-1 overflow-auto p-5 text-xs leading-relaxed whitespace-pre-wrap"
              style={{ color: "var(--foreground)", fontFamily: "'SF Mono', Menlo, monospace" }}>
              {loadingPreview ? "加载中…" : preview.content}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

export function ProjectLogsPanel({ projectId }: { projectId: string }) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/projects/${projectId}/logs`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
      const data = await r.json();
      setLogs(data.logs || []);
    } catch { setLogs([]); }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  const KIND_META: Record<string, { icon: string; label: string }> = {
    collab: { icon: "💬", label: "协作" },
    meeting: { icon: "🏛", label: "会议" },
    event: { icon: "⚙", label: "事件" },
  };

  function exportMd() {
    window.open(
      `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/projects/${projectId}/logs/export?format=md&_t=${Date.now()}`,
      "_blank");
  }

  return (
    <div className="card card-static">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-sm">
          📜 项目日志
          <span className="ml-2 text-[10px] font-normal" style={{ color: "var(--muted)" }}>
            Agent 沟通与工作记录 · {logs.length} 条
          </span>
        </h2>
        <button onClick={exportMd} className="btn-ghost !py-1 !px-2.5 text-[11px]">
          ⬇ 导出 MD
        </button>
      </div>

      <div className={`space-y-1 overflow-y-auto ${expanded ? "max-h-[500px]" : "max-h-52"}`}>
        {(expanded ? logs : logs.slice(0, 12)).map((log, i) => {
          const km = KIND_META[log.kind] || { icon: "•", label: log.kind };
          return (
            <details key={i} className="rounded-lg px-3 py-2 transition-colors group"
              style={{ background: "var(--surface-2)" }}>
              <summary className="text-xs cursor-pointer flex items-center gap-2 list-none">
                <span>{km.icon}</span>
                <span className="font-medium">{log.who}</span>
                <span className="text-[9px] px-1.5 py-0.5 rounded"
                  style={{ background: "rgba(99,102,241,0.12)", color: "#a5b4fc" }}>
                  {log.type}
                </span>
                <span className="ml-auto text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>
                  {log.ts.slice(5, 16).replace("T", " ")}
                </span>
              </summary>
              {log.title && (
                <p className="text-xs mt-1.5 pl-6 font-medium" style={{ color: "#a5b4fc" }}>
                  📌 {log.title}
                </p>
              )}
              {log.content && (
                <p className="text-[11px] mt-1 pl-6 leading-relaxed whitespace-pre-wrap"
                  style={{ color: "var(--muted)" }}>
                  {log.content}
                </p>
              )}
            </details>
          );
        })}
        {!logs.length && (
          <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
            暂无日志 · Agent 协作沟通和任务事件会自动记录到这里
          </p>
        )}
      </div>

      {logs.length > 12 && (
        <button onClick={() => setExpanded(!expanded)}
          className="mt-2 w-full text-xs py-1.5 rounded-lg transition-colors hover:bg-white/[0.04]"
          style={{ color: "var(--muted)" }}>
          {expanded ? "收起 ▲" : `展开全部 ${logs.length} 条 ▼`}
        </button>
      )}
    </div>
  );
}


export function TaskLogsPanel({ taskId }: { taskId: string }) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/tasks/${taskId}/logs`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
      const data = await r.json();
      setLogs(data.logs || []);
    } catch { setLogs([]); }
  }, [taskId]);

  useEffect(() => { load(); }, [load]);

  const KIND_META: Record<string, { icon: string }> = {
    event: { icon: "⚙" },
    intervention: { icon: "🎛" },
    collab: { icon: "💬" },
    meeting: { icon: "🏛" },
  };

  return (
    <div className="card card-static">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-sm">
          📜 任务日志
          <span className="ml-2 text-[10px] font-normal" style={{ color: "var(--muted)" }}>
            事件与人工介入记录 · {logs.length} 条
          </span>
        </h2>
      </div>

      <div className={`space-y-1 overflow-y-auto ${expanded ? "max-h-[500px]" : "max-h-52"}`}>
        {(expanded ? logs : logs.slice(0, 12)).map((log, i) => {
          const km = KIND_META[log.kind] || { icon: "•" };
          return (
            <details key={i} className="rounded-lg px-3 py-2 transition-colors"
              style={{ background: "var(--surface-2)" }}>
              <summary className="text-xs cursor-pointer flex items-center gap-2 list-none">
                <span>{km.icon}</span>
                <span className="font-medium">{log.who}</span>
                <span className="text-[9px] px-1.5 py-0.5 rounded"
                  style={{ background: "rgba(99,102,241,0.12)", color: "#a5b4fc" }}>
                  {log.type}
                </span>
                <span className="ml-auto text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>
                  {log.ts.slice(5, 16).replace("T", " ")}
                </span>
              </summary>
              {log.content && (
                <p className="text-[11px] mt-1 pl-6 leading-relaxed whitespace-pre-wrap"
                  style={{ color: "var(--muted)" }}>
                  {log.content}
                </p>
              )}
            </details>
          );
        })}
        {!logs.length && (
          <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
            暂无日志 · 任务启动后事件会自动记录到这里
          </p>
        )}
      </div>

      {logs.length > 12 && (
        <button onClick={() => setExpanded(!expanded)}
          className="mt-2 w-full text-xs py-1.5 rounded-lg transition-colors hover:bg-white/[0.04]"
          style={{ color: "var(--muted)" }}>
          {expanded ? "收起 ▲" : `展开全部 ${logs.length} 条 ▼`}
        </button>
      )}
    </div>
  );
}
