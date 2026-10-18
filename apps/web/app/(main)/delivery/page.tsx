"use client";
import { useCallback, useEffect, useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { notify } from "@/components/Toast";
import type { Company } from "@/lib/types";

/* ============================================================
   交付中心：Agent 专属交付文件夹
   双视图：📁 按 Agent 文件夹 / 📂 按项目
   ============================================================ */

type FileRow = {
  id: string; name: string; size: number; mime_type?: string;
  agent_id?: string | null; project_id?: string | null;
  exists?: boolean; created_at: string;
};
type Folder = {
  agent_id: string; name: string; role?: string | null;
  file_count: number; total_size: number; latest?: string | null;
  files: FileRow[];
};
type Group = {
  key: string; name: string; is_project: boolean;
  file_count: number; total_size: number; files: FileRow[];
};

const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

function fmtSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
const fileIcon = (name: string, mime?: string | null) => {
  const n = name.toLowerCase();
  if (n.endsWith(".docx") || n.endsWith(".doc")) return "📄";
  if (n.endsWith(".xlsx") || n.endsWith(".xls") || n.endsWith(".csv")) return "📊";
  if (n.endsWith(".pptx") || n.endsWith(".ppt")) return "📽";
  if (n.endsWith(".md")) return "📝";
  if ((mime || "").startsWith("image/")) return "🖼";
  return "📄";
};

export default function DeliveryCenterPage() {
  const [cid, setCid] = useState("");
  const [view, setView] = useState<"agent" | "project" | "task">("agent");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const cs = await api.get<Company[]>("/companies");
    if (!cs.length) { setLoading(false); return; }  // 早退也必须复位 loading，否则骨架屏永远转
    setCid(cs[0].id);
    try {
      if (view === "agent") {
        const r = await api.get(`/delivery/by-agent?company_id=${cs[0].id}`);
        setFolders(r.folders || []);
      } else if (view === "task") {
        const r = await api.get(`/delivery/by-task?company_id=${cs[0].id}`);
        setGroups(r.groups || []);
      } else {
        const r = await api.get(`/delivery/by-project?company_id=${cs[0].id}`);
        setGroups(r.groups || []);
      }
    } catch {}
    setLoading(false);
  }, [view]);

  useEffect(() => { load(); }, [load]);

  async function download(id: string, name: string) {
    try {
      // 下载接口要求 Bearer token，<a href> 带不上——必须取回 blob 再触发保存
      downloadURL(await fetchBlobURL(`/delivery/download/${id}`), name);
    } catch (e: any) {
      notify("error", `下载失败：${describeError(e)}`);
    }
  }

  const match = (n: string) => !search || n.toLowerCase().includes(search.toLowerCase());

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative flex items-end justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">📦 交付中心</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              每位数字员工的专属交付文件夹 · 全部产出集中管理
            </p>
          </div>
          {/* 视图切换 */}
          <div className="flex rounded-full p-0.5" style={{ background: "var(--surface-2)" }}>
            {([["agent", "📁 按 Agent"], ["project", "📂 按项目"], ["task", "🗂 按任务"]] as const).map(([v, label]) => (
              <button key={v} onClick={() => setView(v)}
                className={`px-4 py-1.5 rounded-full text-xs font-medium transition-all ${
                  view === v ? "bg-indigo-600 text-white shadow" : ""}`}
                style={view === v ? {} : { color: "var(--muted)" }}>
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <input placeholder="搜索…" value={search}
        onChange={(e) => setSearch(e.target.value)}
        className="input w-56 !py-2 text-xs mb-5" />

      {loading && (
        <p className="text-sm text-center py-16" style={{ color: "var(--muted)" }}>加载中…</p>
      )}

      {/* ===== 按 Agent 文件夹视图 ===== */}
      {!loading && view === "agent" && (
        <div className="space-y-3">
          {folders.filter((f) => match(f.name)).map((f, i) => {
            const grad = AVATAR_GRADS[i % AVATAR_GRADS.length];
            const open = openKey === f.agent_id;
            return (
              <div key={f.agent_id} className="card overflow-hidden">
                <button onClick={() => setOpenKey(open ? null : f.agent_id)}
                  className="w-full flex items-center gap-4 px-5 py-4 text-left transition-colors hover:bg-white/[0.03]">
                  <div className="w-11 h-11 rounded-xl flex items-center justify-center text-xl shrink-0"
                    style={{
                      background: `linear-gradient(135deg, ${grad})`,
                      boxShadow: `0 4px 14px -6px ${grad.split(",")[0]}66`,
                    }}>
                    📁
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold text-sm">{f.name}</div>
                    <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                      {f.role ? `${f.role} · ` : ""}{f.file_count} 个文件 · {fmtSize(f.total_size)}
                      {f.latest && ` · 最近 ${f.latest.slice(0, 10)}`}
                    </div>
                  </div>
                  <span className="text-xs shrink-0" style={{ color: "var(--muted)" }}>
                    {open ? "▲ 收起" : "▼ 展开"}
                  </span>
                </button>

                {open && (
                  <div className="border-t px-5 py-3 space-y-1.5 animate-fade-up"
                    style={{ borderColor: "var(--border)", background: "rgba(255,255,255,0.02)" }}>
                    {f.files.filter((x) => match(x.name)).map((file) => (
                      <div key={file.id} className="flex items-center gap-3 px-3 py-2 rounded-xl text-xs"
                        style={{ background: "var(--surface-2)" }}>
                        <span>{fileIcon(file.name, file.mime_type)}</span>
                        <span className="truncate flex-1">{file.name}</span>
                        <span className="shrink-0 tabular-nums" style={{ color: "var(--muted)" }}>
                          {fmtSize(file.size)}
                        </span>
                        <span className="shrink-0 tabular-nums" style={{ color: "var(--muted)" }}>
                          {file.created_at.slice(0, 10)}
                        </span>
                        <button onClick={() => download(file.id, file.name)}
                          className="shrink-0 text-indigo-300 hover:text-indigo-200 text-[11px] px-2 py-0.5 rounded-md"
                          style={{ background: "rgba(99,102,241,0.12)" }}>
                          ⬇ 下载
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          {!folders.length && (
            <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
              暂无交付文件 · Agent 完成任务或在聊天中生成文档后会出现在这里
            </div>
          )}
        </div>
      )}

      {/* ===== 按项目视图 ===== */}
      {!loading && (view === "project" || view === "task") && (
        <div className="space-y-3">
          {groups.filter((g) => match(g.name)).map((g) => {
            const open = openKey === g.key;
            return (
              <div key={g.key} className="card overflow-hidden">
                <button onClick={() => setOpenKey(open ? null : g.key)}
                  className="w-full flex items-center gap-4 px-5 py-4 text-left transition-colors hover:bg-white/[0.03]">
                  <div className="w-11 h-11 rounded-xl flex items-center justify-center text-xl shrink-0"
                    style={{ background: g.is_project ? "rgba(34,211,238,0.15)" : "var(--surface-2)" }}>
                    {g.is_project ? "📂" : "📥"}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold text-sm">{g.name}</div>
                    <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                      {g.file_count} 个文件 · {fmtSize(g.total_size)}
                    </div>
                  </div>
                  <span className="text-xs shrink-0" style={{ color: "var(--muted)" }}>
                    {open ? "▲ 收起" : "▼ 展开"}
                  </span>
                </button>

                {open && (
                  <div className="border-t px-5 py-3 space-y-1.5 animate-fade-up"
                    style={{ borderColor: "var(--border)", background: "rgba(255,255,255,0.02)" }}>
                    {g.files.map((file) => {
                      const ag = folders.find((x) => x.agent_id === file.agent_id);
                      return (
                        <div key={file.id} className="flex items-center gap-3 px-3 py-2 rounded-xl text-xs"
                          style={{ background: "var(--surface-2)" }}>
                          <span>{fileIcon(file.name, file.mime_type)}</span>
                          <span className="truncate flex-1">{file.name}</span>
                          {ag && <span className="shrink-0 text-indigo-300">📁 {ag.name}</span>}
                          <span className="shrink-0 tabular-nums" style={{ color: "var(--muted)" }}>
                            {fmtSize(file.size)}
                          </span>
                          <button onClick={() => download(file.id, file.name)}
                            className="shrink-0 text-indigo-300 hover:text-indigo-200 text-[11px] px-2 py-0.5 rounded-md"
                            style={{ background: "rgba(99,102,241,0.12)" }}>
                            ⬇
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
          {!groups.length && (
            <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
              暂无按项目归档的文件
            </div>
          )}
        </div>
      )}
    </div>
  );
}
