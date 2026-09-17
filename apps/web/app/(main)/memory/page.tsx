"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";

type Mem = { id: string; type: string; content: string; importance: number; access_count: number; created_at: string };

export default function MemoryPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState("");
  const [mems, setMems] = useState<Mem[]>([]);
  const [type, setType] = useState("EPISODIC");
  const [content, setContent] = useState("");
  const [memAttach, setMemAttach] = useState<Attachment[]>([]);
  const [importance, setImportance] = useState("0.7");
  const [query, setQuery] = useState("");
  const [msg, setMsg] = useState("");
  type Agent = { id: string; name: string };

  useEffect(() => { api.get("/agents").then(setAgents).catch(() => {}); }, []);
  const cid = async () => (await api.get("/companies"))[0]?.id;

  const load = async (aid = agentId, q = query) => {
    if (!aid) return;
    const c = await cid();
    const r = await api.get(`/memory/recall?company_id=${c}&agent_id=${aid}${q ? `&q=${encodeURIComponent(q)}` : ""}`);
    setMems(r.memories);
  };

  const remember = async () => {
    const c = await cid();
    try {
      const r = await api.post(`/memory/remember?company_id=${c}`,
        { agent_id: agentId, memory_type: type, content, importance: Number(importance) });
      setMsg(r.stored ? "已记住" : "重要度太低，被过滤");
      setContent(""); load();
    } catch (e) { setMsg(String(e)); }
  };

  const forget = async (id: string) => {
    const c = await cid();
    await api.del(`/memory/${agentId}/${id}?company_id=${c}`);
    load();
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4">💭 Agent 记忆</h1>

      <div className="mb-4 flex gap-2 flex-wrap">
        {agents.map((a) => (
          <button key={a.id} onClick={() => { setAgentId(a.id); setTimeout(() => load(a.id), 0); }}
            className={`px-3 py-1.5 rounded border text-sm ${agentId === a.id ? "bg-blue-600 text-white" : "card text-[var(--foreground)]"}`}>
            {a.name}
          </button>
        ))}
      </div>

      {agentId && (
        <>
          <div className="border border-[var(--border)] rounded p-4 mb-4 card space-y-2">
            <div className="flex gap-2">
              <select value={type} onChange={(e) => setType(e.target.value)} className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm">
                {["EPISODIC", "SEMANTIC", "PROCEDURAL", "WORKING", "REFLECTION"].map((t) => (
                  <option key={t}>{t}</option>))}
              </select>
              <input value={importance} onChange={(e) => setImportance(e.target.value)}
                placeholder="重要度0-1" className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm w-28" />
            </div>
            <textarea value={content} onChange={(e) => setContent(e.target.value)}
              placeholder="要记住的内容……" className="w-full bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm h-16" />
            <AttachmentPicker value={memAttach} onChange={setMemAttach} label="添加参考附件" />
            <button onClick={remember} disabled={!content}
              className="btn-primary disabled:opacity-40">记住</button>
          </div>

          <div className="flex gap-2 mb-4">
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="按关键词召回…"
              className="flex-1 bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm" />
            <button onClick={() => load()} className="px-4 py-1 bg-white/[0.04] text-white rounded text-sm">召回</button>
          </div>

          <div className="space-y-2">
            {mems.map((m) => (
              <div key={m.id} className="border border-[var(--border)] rounded p-3 card flex justify-between items-start">
                <div>
                  <span className="text-xs px-1.5 py-0.5 bg-indigo-900/50 text-indigo-300 rounded mr-2">{m.type}</span>
                  <span className="text-xs text-[var(--muted)]">重要度{m.importance} · 被访问{m.access_count}次</span>
                  <p className="text-sm mt-1">{m.content}</p>
                </div>
                <button onClick={() => forget(m.id)} className="px-2 py-1 bg-red-900/40 text-red-300 rounded text-xs">遗忘</button>
              </div>
            ))}
            {!mems.length && agentId && <p className="text-[var(--muted)] text-sm">无记忆记录</p>}
          </div>
        </>
      )}
      {msg && <p className="mt-3 text-sm text-green-700">{msg}</p>}
    </div>
  );
}
