"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

/* ============================================================
   文档风格 DIY：公司自定义风格库 + 批量分配给 Agent
   ============================================================ */

type Style = { id: string; name: string; hint: string; sample?: string | null };
type AgentRow = { id: string; name: string; role?: string | null; doc_style_id?: string | null };

export default function DocStylesPage() {
  const [cid, setCid] = useState("");
  const [styles, setStyles] = useState<Style[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [editing, setEditing] = useState<Style | null>(null);
  const [name, setName] = useState("");
  const [hint, setHint] = useState("");
  const [sample, setSample] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [assignTo, setAssignTo] = useState<string>("");
  const [msg, setMsg] = useState("");
  const sampleRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    setStyles(await api.get(`/doc-styles?company_id=${cs[0].id}`));
    const ags: AgentRow[] = await api.get(`/agents?company_id=${cs[0].id}`);
    setAgents(ags);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save() {
    if (!name.trim() || !hint.trim()) { setMsg("风格名和排版要求必填"); return; }
    if (editing) {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/doc-styles/${editing.id}?company_id=${cid}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ name, hint, sample }),
      });
    } else {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/doc-styles?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ name, hint, sample }),
      });
    }
    setEditing(null); setName(""); setHint(""); setSample("");
    setMsg("✅ 已保存");
    await load();
  }

  async function remove(id: string) {
    if (!confirm("删除该风格？已绑定的员工将自动解除。")) return;
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/doc-styles/${id}?company_id=${cid}`, {
      method: "DELETE", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
    });
    await load();
  }

  async function assign() {
    if (!picked.size) { setMsg("请先勾选员工"); return; }
    const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/doc-styles/assign?company_id=${cid}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
      body: JSON.stringify({ agent_ids: Array.from(picked), style_id: assignTo || null }),
    });
    const d = await r.json();
    setMsg(`✅ 已更新 ${d.changed} 名员工的文档风格`);
    await load();
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{ background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)", border: "1px solid rgba(99,102,241,0.22)" }}>
        <h1 className="text-2xl font-bold tracking-tight">🎨 文档风格工坊</h1>
        <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
          自定义文档排版风格 → 分配给数字员工 · 有专属风格的员工生成文档时自动套用，未分配的用通用公文格式
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* 风格库 */}
        <div className="card p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-sm">风格库（{styles.length}）</h2>
            <button onClick={() => { setEditing(null); setName(""); setHint(""); setSample(""); }}
              className="btn-primary !py-1.5 !px-3 text-xs">＋ 新建风格</button>
          </div>
          {styles.map((st) => (
            <div key={st.id} className="rounded-xl p-3 space-y-1.5" style={{ background: "var(--surface-2)" }}>
              <div className="flex items-center justify-between">
                <span className="font-semibold text-xs">{st.name}</span>
                <div className="flex gap-1.5">
                  <button onClick={() => { setEditing(st); setName(st.name); setHint(st.hint); setSample(st.sample || ""); }}
                    className="text-[11px] text-indigo-300 hover:text-indigo-200">编辑</button>
                  <button onClick={() => remove(st.id)}
                    className="text-[11px] text-red-300 hover:text-red-200">删除</button>
                </div>
              </div>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>{st.hint}</p>
            </div>
          ))}
          {!styles.length && (
            <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>还没有自定义风格</p>
          )}

          {/* 编辑/新建表单 */}
          <div className="rounded-xl p-3 space-y-2 border" style={{ borderColor: "rgba(99,102,241,0.3)" }}>
            <p className="text-xs font-semibold">{editing ? `编辑：${editing.name}` : "新建风格"}</p>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="风格名（如：小红书种草风）"
              className="input w-full !py-2 text-xs" />
            <textarea value={hint} onChange={(e) => setHint(e.target.value)} rows={3}
              placeholder="排版要求提示词（如：文风活泼多用emoji，段落短，每段配一个小标题）"
              className="input w-full !py-2 text-xs resize-none" />
            <textarea value={sample} onChange={(e) => setSample(e.target.value)} rows={2}
              placeholder="示例片段（可选，可直接上传文档自动提取）"
              className="input w-full !py-2 text-xs resize-none" />
            <div className="flex items-center gap-2">
              <input ref={sampleRef} type="file" className="hidden"
                accept=".txt,.md,.docx,.pdf,.xlsx,.pptx"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f || !cid) return;
                  setMsg("正在解析附件…");
                  const fd = new FormData();
                  fd.append("file", f);
                  try {
                    const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/doc-styles/upload-sample?company_id=${cid}`, {
                      method: "POST",
                      headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
                      body: fd,
                    });
                    const d = await r.json();
                    if (d.markdown) { setSample(d.markdown.slice(0, 8000)); setMsg(`✅ 已从 ${f.name} 提取示例`); }
                    else setMsg("❌ 解析失败");
                  } catch { setMsg("❌ 上传失败"); }
                  e.target.value = "";
                }} />
              <button onClick={() => sampleRef.current?.click()}
                className="btn-ghost text-xs">📎 上传示例文档（docx/pdf/md…）</button>
            </div>
            <button onClick={save} disabled={!name.trim() || !hint.trim()}
              className="btn-primary !py-1.5 text-xs w-full disabled:opacity-40">保存</button>
          </div>
        </div>

        {/* 员工分配 */}
        <div className="card p-4 space-y-3">
          <h2 className="font-semibold text-sm">分配给员工</h2>
          <select value={assignTo} onChange={(e) => setAssignTo(e.target.value)}
            className="input w-full !py-2 text-xs">
            <option value="">— 选择要分配的风格 —</option>
            {styles.map((st) => <option key={st.id} value={st.id}>{st.name}</option>)}
            <option value="">（解除绑定 → 恢复通用格式）</option>
          </select>
          <div className="max-h-80 overflow-y-auto space-y-1">
            {agents.map((a) => {
              const bound = styles.find((st) => st.id === a.doc_style_id);
              const on = picked.has(a.id);
              return (
                <button key={a.id}
                  onClick={() => setPicked((prev) => { const n = new Set(prev); on ? n.delete(a.id) : n.add(a.id); return n; })}
                  className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${on ? "border-emerald-500/50 bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}
                  style={on ? {} : { borderColor: "transparent" }}>
                  <span className="truncate flex-1">{a.name}{a.role ? ` · ${a.role}` : ""}</span>
                  {bound && <span className="text-[10px] text-indigo-300 shrink-0">🎨{bound.name}</span>}
                  {on && <span className="text-emerald-300 shrink-0">✓</span>}
                </button>
              );
            })}
          </div>
          <button onClick={assign} disabled={!picked.size}
            className="btn-primary !py-1.5 text-xs w-full disabled:opacity-40">
            应用到选中员工（{picked.size}）
          </button>
        </div>
      </div>

      {msg && <p className="mt-4 text-xs text-indigo-300">{msg}</p>}
    </div>
  );
}
