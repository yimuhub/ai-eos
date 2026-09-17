"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import { createPortal } from "react-dom";

/* ============================================================
   知识库中心 v2：全局公用库 + Agent 私人库（P4）
   双格式存储 · 技能模板一键导入 · 成本模式切换
   ============================================================ */

type Doc = {
  id: string; title: string; scope: string; category?: string | null;
  chunk_count: number; summary?: string; has_original?: boolean;
  original_name?: string | null; skill_template?: string | null;
  created_at: string;
};
type AgentRow = { id: string; name: string };
type Tpl = { key: string; name: string; icon: string; doc_count: number };

const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

const COST_MODES = [
  { key: "precision", name: "高精度", desc: "Top-8 · 不压缩 · 预算6000字", icon: "💎" },
  { key: "balanced", name: "平衡（推荐）", desc: "Top-5 · 轻压缩 · 预算3000字", icon: "⚖️" },
  { key: "economy", name: "极致省成本", desc: "Top-3 · 强压缩 · 预算1200字", icon: "🌱" },
];

export default function KnowledgePage() {
  const [cid, setCid] = useState("");
  const [tab, setTab] = useState<"global" | "private">("global");
  const [globalDocs, setGlobalDocs] = useState<Doc[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [selAgent, setSelAgent] = useState("");
  const [privateDocs, setPrivateDocs] = useState<Doc[]>([]);
  const [templates, setTemplates] = useState<Tpl[]>([]);
  const [search, setSearch] = useState("");

  // 新建文档
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ title: "", content: "", category: "" });

  // 上传
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadMsg, setUploadMsg] = useState("");
  const [msg, setMsg] = useState("");

  // 预览
  const [preview, setPreview] = useState<{ title: string; content: string } | null>(null);

  // 成本模式
  const [costMode, setCostMode] = useState<string>("balanced");
  const [cfgSaved, setCfgSaved] = useState(false);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    api.get(`/kb2/global/documents?company_id=${cs[0].id}`).then(setGlobalDocs).catch(() => {});
    api.get("/agents").then((as: AgentRow[]) => {
      setAgents(as);
      setSelAgent((prev) => prev || as[0]?.id || "");
    }).catch(() => {});
    api.get("/kb2/templates").then(setTemplates).catch(() => {});
  }, []);

  const loadPrivate = useCallback(async () => {
    if (!selAgent) return;
    try {
      const docs = await api.get(`/kb2/agents/${selAgent}/documents`);
      setPrivateDocs(Array.isArray(docs) ? docs : []);
    } catch { setPrivateDocs([]); }
    try {
      const ags = await api.get("/agents");
      const cur = (ags as any[]).find((a) => a.id === selAgent);
      if (cur?.memory_config?.cost_mode) setCostMode(cur.memory_config.cost_mode);
    } catch {}
  }, [selAgent]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadPrivate(); }, [loadPrivate]);

  const curDocs = tab === "global" ? globalDocs : privateDocs;
  const filtered = useMemoList(curDocs, search);

  /* ---------- 操作 ---------- */
  async function createDoc() {
    if (!form.title.trim()) return;
    if (tab === "global") {
      await api.post(`/kb2/global/documents?company_id=${cid}`,
        { ...form });
      const docs = await api.get(`/kb2/global/documents?company_id=${cid}`);
      setGlobalDocs(docs);
    } else if (selAgent) {
      await api.post(`/kb2/agents/${selAgent}/documents`, { ...form });
      await loadPrivate();
    }
    setShowForm(false);
    setForm({ title: "", content: "", category: "" });
  }

  async function deleteDoc(id: string) {
    if (!(await confirmAction({
      title: "删除这篇文档？",
      description: "员工将不再引用其中的内容来回答任务。",
      bullets: ["原始文件副本会一并删除", "此操作不可恢复"],
      confirmText: "删除",
    }))) return;
    await api.del(`/kb2/documents/${id}`);
    if (tab === "global") {
      setGlobalDocs((prev) => prev.filter((d) => d.id !== id));
    } else {
      await loadPrivate();
    }
  }

  async function uploadFile(file: File) {
    if (tab !== "private" || !selAgent) return;
    setUploadMsg(`上传中：${file.name}…`);
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(
      `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/kb2/agents/${selAgent}/upload`,
      { method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` }, body: fd });
    const data = await r.json();
    setUploadMsg(data.converted
      ? `✅ ${file.name} 已入库（${data.chunks} 块索引 + 原始文件保留）`
      : `⚠️ ${file.name} 无法提取文本，仅保留原始文件`);
    await loadPrivate();
  }

  async function importTpl(key: string) {
    if (!selAgent) return;
    const r = await fetch(
      `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/kb2/agents/${selAgent}/import-template/${key}`,
      { method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
    if (r.ok) {
      setUploadMsg(`✅ 模板「${templates.find((t) => t.key === key)?.name}」已导入私人库`);
      await loadPrivate();
    }
  }

  async function copyToAgent(docId: string, target: string) {
    await api.post("/kb2/copy", { doc_ids: [docId], target_agent_id: target });
    setMsg(`已复制到「${agents.find((a) => a.id === target)?.name}」的私人库`);
  }

  async function syncFromGlobal(docId: string) {
    if (!selAgent) return;
    await api.post(`/kb2/agents/${selAgent}/sync-from-global`, { doc_ids: [docId] });
    setMsg("✅ 已同步到私人库");
    await loadPrivate();
  }

  async function saveCostMode(mode: string) {
    setCostMode(mode);
    if (!selAgent) return;
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/kb2/agents/${selAgent}/config`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json",
                   Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ cost_mode: mode }),
      });
    setCfgSaved(true);
    setTimeout(() => setCfgSaved(false), 2000);
  }

  async function openPreview(doc: Doc) {
    try {
      const d = await api.get(`/knowledge/documents/${doc.id}`);
      setPreview({ title: doc.title, content: d.content || "(空)" });
    } catch {
      setPreview({ title: doc.title, content: "预览失败" });
    }
  }

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
            <h1 className="text-2xl font-bold tracking-tight">知识库中心</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              全局公用 + Agent 私人两级体系 · 双格式存储 · Token 成本优化
            </p>
          </div>
          <button onClick={() => setShowForm(!showForm)} className="btn-primary">
            ＋ 添加文档
          </button>
        </div>
      </div>

      {/* 两级切换 + Agent 选择 */}
      <div className="card card-static mb-5 flex items-center gap-3 flex-wrap !py-3">
        <div className="flex rounded-full p-0.5" style={{ background: "var(--surface-2)" }}>
          {([["global", "🌍 全局公用"], ["private", "👤 Agent 私人"]] as const).map(([v, label]) => (
            <button key={v} onClick={() => setTab(v)}
              className={`px-4 py-1.5 rounded-full text-xs font-medium transition-all ${
                tab === v ? "bg-indigo-600 text-white shadow" : ""}`}
              style={tab === v ? {} : { color: "var(--muted)" }}>
              {label}
            </button>
          ))}
        </div>
        {tab === "private" && (
          <select value={selAgent}
            onChange={(e) => { setSelAgent(e.target.value); }}
            className="input w-auto !py-1.5 text-xs ml-auto">
            {agents.map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
          </select>
        )}
        <input placeholder="搜索…" value={search} onChange={(e) => setSearch(e.target.value)}
          className="input w-40 !py-1.5 text-xs ml-auto" />
      </div>

      {/* 新建表单 */}
      {showForm && (
        <div className="card mb-5 space-y-2.5 animate-fade-up">
          <h3 className="font-medium text-sm">添加到{tab === "global" ? "全局公用" : "Agent 私人"}知识库</h3>
          <div className="grid grid-cols-1 sm:grid-cols-[1fr_200px] gap-2.5">
            <input placeholder="文档标题 *" value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })} className="input" />
            <input placeholder="分类标签（可选）" value={form.category}
              onChange={(e) => setForm({ ...form, category: e.target.value })} className="input" />
          </div>
          <textarea rows={4} placeholder="文档内容…" value={form.content}
            onChange={(e) => setForm({ ...form, content: e.target.value })} className="input" />
          <button onClick={createDoc} disabled={!form.title.trim()}
            className="btn-primary disabled:opacity-40">保存并建立索引</button>
        </div>
      )}

      {/* 私人库专属区：上传+模板+成本模式 */}
      {tab === "private" && selAgent && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mb-5">
          {/* 上传 */}
          <div className="card card-static">
            <h3 className="font-semibold text-sm mb-2">📎 上传文件</h3>
            <p className="text-[11px] mb-2 leading-relaxed" style={{ color: "var(--muted)" }}>
              支持 txt/md/csv/json/pdf/docx。<br />原始文件保留 + 自动转换双副本。
            </p>
            <input ref={fileRef} type="file" accept=".txt,.md,.csv,.json,.pdf,.docx"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadFile(f);
                e.target.value = "";
              }} />
            <button onClick={() => fileRef.current?.click()} className="btn-ghost w-full text-xs">
              选择文件上传
            </button>
            {uploadMsg && <p className="mt-2 text-[11px] text-indigo-300">{uploadMsg}</p>}
          </div>

          {/* 技能模板 */}
          <div className="card card-static">
            <h3 className="font-semibold text-sm mb-2">🧩 技能知识库模板</h3>
            <div className="space-y-1.5 max-h-36 overflow-y-auto">
              {templates.map((t) => (
                <button key={t.key} onClick={() => importTpl(t.key)}
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs transition-colors hover:bg-white/[0.05]"
                  style={{ background: "var(--surface-2)" }}>
                  <span>{t.icon}</span>
                  <span className="flex-1 text-left truncate">{t.name}</span>
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>{t.doc_count}篇</span>
                  <span className="text-indigo-300">导入</span>
                </button>
              ))}
            </div>
          </div>

          {/* 成本模式 */}
          <div className="card card-static">
            <div className="flex items-center justify-between mb-2">
              <h3 className="font-semibold text-sm">💰 成本模式</h3>
              {cfgSaved && <span className="text-[11px] text-emerald-400">✓ 已保存</span>}
            </div>
            <div className="space-y-1.5">
              {COST_MODES.map((m) => (
                <button key={m.key} onClick={() => saveCostMode(m.key)}
                  className={`w-full flex items-center gap-2 px-2.5 py-2 rounded-xl text-left transition-all border ${
                    costMode === m.key ? "border-emerald-500/50 bg-emerald-500/10"
                                       : "hover:bg-white/[0.04]"}`}
                  style={costMode === m.key ? {} : { borderColor: "var(--border)" }}>
                  <span>{m.icon}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium">{m.name}</div>
                    <div className="text-[10px]" style={{ color: "var(--muted)" }}>{m.desc}</div>
                  </div>
                  {costMode === m.key && <span className="text-emerald-400 shrink-0">✓</span>}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 文档网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-start">
        {filtered.map((doc, i) => {
          const grad = AVATAR_GRADS[i % AVATAR_GRADS.length];
          return (
            <div key={doc.id} className="card group relative overflow-hidden flex flex-col cursor-pointer"
              onClick={() => openPreview(doc)}>
              <div className="absolute top-0 left-0 w-full h-[2px]"
                style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />
              <div className="flex items-start gap-3">
                <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0"
                  style={{
                    background: `linear-gradient(135deg, ${grad})`,
                    boxShadow: `0 4px 14px -6px ${grad.split(",")[0]}66`,
                  }}>📄</div>
                <div className="min-w-0 flex-1 pr-8">
                  <div className="font-semibold text-sm line-clamp-2 leading-snug">{doc.title}</div>
                  <div className="mt-1 flex items-center gap-2 text-[11px] flex-wrap">
                    {doc.category && (
                      <span className="px-1.5 py-0.5 rounded-md"
                        style={{ background: "rgba(99,102,241,0.12)", color: "#a5b4fc" }}>
                        {doc.category}
                      </span>
                    )}
                    {doc.skill_template && (
                      <span className="px-1.5 py-0.5 rounded-md text-purple-300"
                        style={{ background: "rgba(168,85,247,0.12)" }}>
                        🧩 模板
                      </span>
                    )}
                    {doc.has_original && (
                      <span className="text-emerald-400" title="原始文件已保留">📎</span>
                    )}
                  </div>
                </div>
              </div>

              {doc.summary && (
                <p className="mt-2 text-[11px] line-clamp-2" style={{ color: "var(--muted)" }}>
                  {doc.summary}
                </p>
              )}

              <div className="flex items-center gap-3 mt-2 text-[10px]" style={{ color: "var(--muted)" }}>
                <span>{doc.chunk_count} 块索引</span>
                <span>{doc.created_at.slice(0, 10)}</span>
              </div>

              {/* 操作行 */}
              <div className="mt-auto pt-3 mt-3 border-t flex gap-1.5 opacity-0 group-hover:opacity-100 transition-all duration-200"
                style={{ borderColor: "var(--border)" }}>
                <button onClick={(e) => { e.stopPropagation(); openPreview(doc); }}
                  className="flex-1 text-[11px] py-1.5 rounded-lg hover:bg-white/[0.05] transition-colors"
                  style={{ border: "1px solid var(--border)" }}>👁 预览</button>
                {tab === "global" && agents.length > 0 && (
                  <select title="同步到 Agent 私人库"
                    onChange={(e) => { if (e.target.value) { copyToAgent(doc.id, e.target.value); e.target.value = ""; } }}
                    onClick={(e) => e.stopPropagation()}
                    className="flex-1 text-[11px] py-1.5 rounded-lg input !py-1.5 !px-2">
                    <option value="">📤 同步到…</option>
                    {agents.map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
                  </select>
                )}
                {tab === "private" && agents.length > 1 && (
                  <select title="复制到其他 Agent"
                    onChange={(e) => { if (e.target.value) { copyToAgent(doc.id, e.target.value); e.target.value = ""; } }}
                    onClick={(e) => e.stopPropagation()}
                    className="flex-1 text-[11px] py-1.5 rounded-lg input !py-1.5 !px-2">
                    <option value="">📤 复制到…</option>
                    {agents.filter((a) => a.id !== selAgent).map((a) => (
                      <option key={a.id} value={a.id}>{a.name}</option>))}
                  </select>
                )}
                <button onClick={(e) => { e.stopPropagation(); deleteDoc(doc.id); }}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                  style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑</button>
              </div>
            </div>
          );
        })}

        {!filtered.length && (
          <div className="card card-static text-center py-16 col-span-full text-sm"
            style={{ color: "var(--muted)" }}>
            {search ? "没有符合条件" : tab === "global" ? "公用库暂无文档" :
              "该 Agent 私人库为空 · 可上传文件或导入技能模板"}
          </div>
        )}
      </div>

      {/* 预览弹窗 */}
      {preview &&
        typeof document !== "undefined" &&
        createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.7)", backdropFilter: "blur(6px)" }}
          onClick={() => setPreview(null)}>
          <div className="w-full max-w-3xl max-h-[85vh] rounded-2xl overflow-hidden flex flex-col animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid var(--border)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="px-5 py-3.5 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <h3 className="font-bold text-sm truncate">📄 {preview.title}</h3>
              <button onClick={() => setPreview(null)} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
            </div>
            <pre className="flex-1 overflow-auto p-5 text-xs leading-relaxed whitespace-pre-wrap"
              style={{ fontFamily: "'SF Mono', Menlo, monospace" }}>{preview.content}</pre>
          </div>
        </div>,
        document.body)}
    </div>
  );
}

function useMemoList(docs: Doc[], search: string) {
  return docs.filter((d) =>
    !search || d.title.toLowerCase().includes(search.toLowerCase()));
}
