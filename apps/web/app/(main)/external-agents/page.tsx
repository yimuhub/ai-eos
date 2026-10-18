"use client";
import { useMemo, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

type Conn = { id: string; name: string; provider: string; endpoint?: string; auth_env_var?: string; capabilities?: string[]; status?: string; attachments?: Attachment[] };

export default function ExternalAgentsPage() {
  const cid = useActiveCompanyId();
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("API");
  const [endpoint, setEndpoint] = useState("");
  const [envVar, setEnvVar] = useState("");
  // V5.5 B2：外部 MCP Server 开关（capabilities=["mcp"]）
  const [isMcp, setIsMcp] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [msg, setMsg] = useState("");

  const { data, refresh } = useResource(
    cid ? `external-agents:${cid}` : null,
    () => api.get<Conn[]>(`/external-agents?company_id=${cid}`),
  );
  const conns = useMemo(() => data ?? [], [data]);

  async function register() {
    if (!name || !cid) return;
    try {
      await api.post(`/external-agents?company_id=${cid}`, {
        name, provider_type: isMcp ? "REMOTE" : provider,
        endpoint: endpoint || undefined,
        auth_env_var: envVar || undefined,
        capabilities: isMcp ? ["mcp"] : undefined,
        attachments: attachments.length ? attachments : undefined,
      });
      setMsg("✅ 已注册");
      setName(""); setEndpoint(""); setEnvVar(""); setAttachments([]); setIsMcp(false);
      await refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "注册失败")}`); }
  }

  async function toggleStatus(c: Conn) {
    const enabling = c.status !== "ENABLED";
    try {
      // 原来是裸 fetch + PUT `/external-agents/{id}/ENABLED`，
      // 而后端只有 POST /external-agents/{cid}/enable 与 /disable ——
      // 这条路径从来就没生效过，界面却照样提示"已启用"。
      // 现在按后端真实契约调用，且失败会真的报错。
      if (enabling) await api.post(`/external-agents/${c.id}/enable`);
      else await api.post(`/external-agents/${c.id}/disable`);
      setMsg(enabling ? "已启用" : "已停用");
      await refresh();
    } catch (e) { setMsg(`❌ ${describeError(e, "切换状态失败")}`); }
  }

  async function testConnection(id: string) {
    try {
      const r = await api.post<{ ok: boolean; error?: string }>(`/external-agents/${id}/test`);
      setMsg(r.ok ? "✅ 连接成功" : `❌ ${r.error || "失败"}`);
    } catch (e) { setMsg(`❌ ${describeError(e, "测试失败")}`); }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4 text-[var(--foreground)]">🔌 外部接入</h1>
      <div className="grid grid-cols-2 gap-4">
        {/* 注册 */}
        <div className="card card-static p-4 space-y-2">
          <h3 className="font-medium text-[var(--foreground)]">注册新接入</h3>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称"
            className="input" />
          <select value={provider} onChange={(e) => setProvider(e.target.value)}
            className="w-full bg-white/[0.04] border border-[var(--border)] rounded px-3 py-1.5 text-sm text-[var(--foreground)] focus:outline-none">
            <option value="API">API</option>
            <option value="LOCAL">本地</option>
            <option value="REMOTE">远程</option>
          </select>
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="Endpoint（如 http://localhost:8080）"
            className="input" />
          <input value={envVar} onChange={(e) => setEnvVar(e.target.value)} placeholder="API Key 环境变量名"
            className="input" />
          {/* V5.5 B2：外部 MCP Server */}
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none"
            style={{ color: "var(--muted)" }}>
            <input type="checkbox" checked={isMcp} onChange={(e) => setIsMcp(e.target.checked)} />
            <span>
              🔌 这是外部 <b>MCP Server</b>（填 JSON-RPC 端点，如 <code>/mcp</code> 或第三方 MCP 服务）
            </span>
          </label>
          {isMcp && (
            <p className="text-[11px] leading-relaxed p-2 rounded-lg"
              style={{ background: "rgba(34,211,238,0.07)", border: "1px solid rgba(34,211,238,0.25)", color: "var(--muted)" }}>
              执行时任务内容按 <code>{"{"}tool, arguments{"}"}</code> JSON 直通外部 MCP 的 tools/call；
              自然语言则先 tools/list 取首个工具。出站方向：AI-EOS 自身也是 MCP Server
              （POST /mcp，7 个 aieos.* 工具），可被 Claude Code / Codex 等外部 Agent 调用。
            </p>
          )}
          <AttachmentPicker value={attachments} onChange={setAttachments} label="📎 关联附件（接入文档 / 密钥说明）" />
          <button onClick={register} disabled={!name}
            className="btn-primary disabled:opacity-40">注册</button>
        </div>
        {/* 列表 */}
        <div className="card card-static p-4 space-y-2">
          <h3 className="font-medium text-[var(--foreground)]">已接入</h3>
          {conns.map((c) => (
            <div key={c.id} className="rounded-xl p-2.5 bg-white/[0.03] border border-[var(--border)] text-sm text-[var(--foreground)] flex justify-between items-center">
              <div>
                <span className="font-medium text-[var(--foreground)]">{c.name}</span>
                <span className="ml-2 text-xs text-[var(--muted)]">{c.provider}</span>
                {(c.endpoint) && <span className="ml-1 text-xs text-[var(--muted)]">{c.endpoint}</span>}
                {!!c.attachments?.length && (
                  <span className="ml-1.5 text-xs px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300">📎 {c.attachments!.length}</span>
                )}
                <span className={`ml-2 text-xs px-1.5 py-0.5 rounded ${c.status === "ENABLED" ? "bg-green-900/40 text-green-300" : "bg-white/[0.04] text-[var(--muted)]"}`}>
                  {c.status === "ENABLED" ? "启用" : "停用"}
                </span>
              </div>
              <div className="flex gap-1">
                <button onClick={() => testConnection(c.id)}
                  className="px-2 py-0.5 bg-white/[0.06] hover:bg-white/[0.1] text-[var(--foreground)] rounded text-xs">测试</button>
                <button onClick={() => toggleStatus(c)}
                  className="px-2 py-0.5 bg-white/[0.06] hover:bg-white/[0.1] text-[var(--foreground)] rounded text-xs">
                  {c.status === "ENABLED" ? "停用" : "启用"}
                </button>
              </div>
            </div>
          ))}
          {!conns.length && <p className="text-xs text-[var(--muted)]">暂无接入</p>}
        </div>
      </div>
      {msg && <p className="mt-3 text-sm text-blue-300">{msg}</p>}
    </div>
  );
}
