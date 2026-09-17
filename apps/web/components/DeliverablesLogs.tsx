"use client";
import { useCallback, useEffect, useState } from "react";
import { api, describeError, downloadText, downloadURL, fetchBlobURL, fetchText } from "@/lib/api";
import { notify } from "@/components/Toast";

/* ============================================================
   交付成果与日志面板（项目详情 / 任务详情共用）
   - 成果列表：点击弹出预览窗口
   - 项目日志：Agent 沟通/想法，支持导出 MD
   ============================================================ */

/** 交付文件夹里的一条文件（`/delivery/by-scope` 返回的 files 元素） */
export type FolderFile = { id: string; name: string; size?: number; path?: string };

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
      // 原实现用 r.text() 取原始响应体——而 /assets/{id} 返回的是 JSON，
      // 于是预览窗口里显示的是一整段 {"id":...,"content":"..."} 而不是正文。
      const d = await api.get<{ content?: unknown }>(`/assets/${a.id}`);
      const text = d.content !== undefined ? String(d.content) : JSON.stringify(d, null, 2);
      setPreview({ name: a.name, content: text });
    } catch (e) {
      setPreview({ name: a.name, content: `预览失败：${describeError(e, "预览失败")}` });
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
                {/* 下载接口需要 Bearer token：不能用 <a href>（浏览器不会带 header，
                    点了只会拿到 401），必须先取回 blob 再本地保存 */}
                <button type="button"
                  onClick={async () => {
                    const assetId = assets.find((x) => x.name === preview.name)?.id;
                    if (!assetId) { notify("error", "找不到该成果，无法下载"); return; }
                    try {
                      downloadURL(await fetchBlobURL(`/assets/${assetId}/download`), preview.name);
                    } catch (e: any) {
                      notify("error", `下载失败：${describeError(e)}`);
                    }
                  }}
                  className="btn-primary !py-1.5 !px-3 text-xs">下载</button>
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
      const data = await api.get<{ logs?: LogEntry[] }>(`/projects/${projectId}/logs`);
      setLogs(data.logs || []);
    } catch { setLogs([]); }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  const KIND_META: Record<string, { icon: string; label: string }> = {
    collab: { icon: "💬", label: "协作" },
    meeting: { icon: "🏛", label: "会议" },
    event: { icon: "⚙", label: "事件" },
  };

  async function exportMd() {
    // 原来用 window.open(接口地址)：导出接口同样需要 token，
    // 新标签页打开只会显示 401 的 JSON，用户以为"导出了但没内容"。
    try {
      const md = await fetchText(`/projects/${projectId}/logs/export?format=md`);
      downloadText(`项目日志-${projectId.slice(0, 8)}.md`, md);
      notify("success", "日志已导出为 Markdown");
    } catch (e: any) {
      notify("error", `导出失败：${describeError(e)}`);
    }
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
      const data = await api.get<{ logs?: LogEntry[] }>(`/tasks/${taskId}/logs`);
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
