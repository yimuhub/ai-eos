"use client";
import { useMemo, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";

type Mem = { id: string; type: string; content: string; importance: number; access_count: number; created_at: string };
type Agent = { id: string; name: string };

export default function MemoryPage() {
  // 激活公司来自可订阅的 useActiveCompanyId：切公司后记忆列表自动跟随，
  // 不再用一次性的 `/companies → cs[0]` 快照。
  const cid = useActiveCompanyId();
  const [agentId, setAgentId] = useState("");
  // 输入框与"已生效的查询词"分开：输入不触发请求，点「召回」才重取。
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [type, setType] = useState("EPISODIC");
  const [content, setContent] = useState("");
  const [memAttach, setMemAttach] = useState<Attachment[]>([]);
  const [importance, setImportance] = useState("0.7");
  const [msg, setMsg] = useState("");

  // 员工列表：接口仍是 /agents（不额外加过滤），key 带上 cid 只用于
  // "切公司时重取 + 与其它页面的 agents: 缓存互不串味"。
  const { data: agentData } = useResource<Agent[]>(
    cid ? `memory:agents:${cid}` : null,
    () => api.get<Agent[]>("/agents"),
  );
  const agents = useMemo<Agent[]>(() => agentData ?? [], [agentData]);

  // 记忆召回：cid + 员工 + 已生效查询词共同决定内容
  const { data: memData, error: memError, loading, refresh: refreshMems } = useResource<Mem[]>(
    cid && agentId ? `memory:recall:${cid}:${agentId}:${appliedQuery}` : null,
    async () => {
      // 查询串用 URLSearchParams 拼：既不用在模板里嵌三元（契约扫描看不懂），
      // 也顺手解决了"内容含 & 或中文时查询串被截断/编码错"的老问题。
      const qs = new URLSearchParams({ company_id: cid ?? "", agent_id: agentId });
      if (appliedQuery) qs.set("q", appliedQuery);
      const r = await api.get<{ memories: Mem[] }>(`/memory/recall?${qs}`);
      return r.memories || [];
    },
  );
  const mems = useMemo<Mem[]>(() => memData ?? [], [memData]);

  function recall() {
    // 查询词变了 → 换 key（effect 自动重取）；没变 → 强制重取一次
    if (query !== appliedQuery) setAppliedQuery(query);
    else refreshMems();
  }

  const remember = async () => {
    if (!cid) return;
    try {
      const r = await api.post(`/memory/remember?company_id=${cid}`,
        { agent_id: agentId, memory_type: type, content, importance: Number(importance) });
      setMsg(r.stored ? "已记住" : "重要度太低，被过滤");
      setContent(""); refreshMems();
    } catch (e) { setMsg(describeError(e, "操作失败")); }
  };

  const forget = async (id: string) => {
    if (!cid) return;
    // 原实现点一下"忘记"就直接删，没有任何确认和失败反馈。
    // 记忆是员工的经验积累，误删等于让它重犯同一个错。
    if (!(await confirmAction({
      title: "确认让该员工忘记这条记忆？",
      description: "之后的任务中它不会再引用这条事实，且无法恢复。",
      confirmText: "忘记",
    }))) return;
    try {
      await api.del(`/memory/${agentId}/${id}?company_id=${cid}`);
      notify("success", "已忘记该条记忆");
    } catch (e) {
      notify("error", `删除失败：${(e as Error).message || e}`);
    }
    refreshMems();
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4">💭 Agent 记忆</h1>

      <div className="mb-4 flex gap-2 flex-wrap">
        {agents.map((a) => (
          <button key={a.id} onClick={() => setAgentId(a.id)}
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
            <button onClick={recall} className="px-4 py-1 bg-white/[0.04] text-white rounded text-sm">召回</button>
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
            {!loading && !mems.length && agentId && <p className="text-[var(--muted)] text-sm">无记忆记录</p>}
          </div>
        </>
      )}
      {memError && (
        <p className="mt-3 text-sm text-red-300">❌ 召回失败：{memError}
          <button onClick={() => refreshMems()} className="ml-2 underline">重试</button>
        </p>
      )}
      {msg && <p className="mt-3 text-sm text-green-700">{msg}</p>}
    </div>
  );
}
