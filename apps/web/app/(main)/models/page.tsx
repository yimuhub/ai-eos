"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/* ============================================================
   模型配置中心：管理大模型供应商 + 拉取模型列表 + 勾选启用
   ============================================================ */

type ModelRow = {
  id: string; name: string; provider_type: string;
  base_url?: string | null; model_id: string;
};
type Agent = { id: string; name: string };
type AgentUsage = Record<string, number>; // model_id -> 使用 agent 数

const PROVIDER_LABELS: Record<string, string> = {
  openai_compat: "OpenAI 兼容",
  anthropic: "Anthropic",
  gemini: "Gemini",
};

// 预置的智能体类型（Agent 底座）
const AGENT_BACKENDS = [
  { id: "hermes", name: "Hermes（本地）", desc: "本机 Hermes Agent，支持全部工具链" },
  { id: "codex", name: "Codex（本地）", desc: "OpenAI Codex CLI，强代码与 GitHub" },
  { id: "openai_compat", name: "OpenAPI 接入", desc: "任意 OpenAI 兼容 API / 云端大模型" },
];

export default function ModelsPage() {
  const [models, setModels] = useState<ModelRow[]>([]);
  const [convertModel, setConvertModel] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [usage, setUsage] = useState<AgentUsage>({});
  const [msg, setMsg] = useState("");

  // 新增供应商表单
  const [provName, setProvName] = useState("");
  const [provType, setProvType] = useState("openai_compat");
  const [provUrl, setProvUrl] = useState("");
  const [provKeyEnv, setProvKeyEnv] = useState("");
  const [provModelId, setProvModelId] = useState("");

  // 拉取模型列表
  const [fetchUrl, setFetchUrl] = useState("");
  const [fetchKey, setFetchKey] = useState("");
  const [fetching, setFetching] = useState(false);
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [importPrefix, setImportPrefix] = useState("");

  // 测试连接中
  const [testing, setTesting] = useState<string | null>(null);

  const load = useCallback(async () => {
    setModels(await api.get("/models"));
    const as: Agent[] = await api.get("/agents");
    setAgents(as);
    // 统计每个模型被多少 agent 使用
    const u: AgentUsage = {};
    for (const a of as) {
      const binds = await api.get(`/agents/${a.id}/models`).catch(() => []);
      for (const b of binds) u[b.model_id] = (u[b.model_id] || 0) + 1;
    }
    setUsage(u);
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/models/convert-config`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
    }).then((r) => r.json()).then((d: any) => setConvertModel(d.convert_model_id || "")).catch(() => {});
  }, []);
  /* ---- 手动添加单个模型配置 ---- */
  async function addModel() {
    if (!provName || !provModelId) return;
    try {
      await api.post("/models", {
        name: provName, provider_type: provType,
        base_url: provUrl || null, api_key_env: provKeyEnv || null,
        model_id: provModelId,
      });
      setMsg(`✅ 已添加 ${provName}`);
      setProvName(""); setProvUrl(""); setProvKeyEnv(""); setProvModelId("");
      await load();
    } catch (e: any) { setMsg(e.message || String(e)); }
  }

  /* ---- 从供应商拉取模型列表 ---- */
  async function fetchModels() {
    if (!fetchUrl) return;
    setFetching(true);
    setRemoteModels([]);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/models/providers/list-models`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ base_url: fetchUrl, api_key: fetchKey || null }),
      });
      const data = await r.json();
      if (data.status === "ok") {
        setRemoteModels(data.models);
        if (!importPrefix) {
          try { setImportPrefix(new URL(fetchUrl).hostname.split(".")[0]); } catch {}
        }
      } else {
        setMsg(`❌ ${data.error}`);
      }
    } catch (e: any) { setMsg(e.message || String(e)); }
    setFetching(false);
  }

  /* ---- 批量导入勾选的模型 ---- */
  async function importSelected() {
    if (!checked.size || !fetchUrl) return;
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/models/providers/batch-import`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({
          name_prefix: importPrefix || "provider",
          provider_type: "openai_compat",
          base_url: fetchUrl,
          model_ids: Array.from(checked),
        }),
      });
      const data = await r.json();
      setMsg(`✅ 导入 ${data.imported_count} 个模型${data.skipped_existing?.length ? `，跳过已存在 ${data.skipped_existing.length} 个` : ""}`);
      setChecked(new Set());
      setRemoteModels([]);
      await load();
    } catch (e: any) { setMsg(e.message || String(e)); }
  }

  /* ---- 连通性测试 ---- */
  async function testModel(mid: string) {
    setTesting(mid);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/models/${mid}/test`, {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
      });
      const d = await r.json();
      setMsg(d.status === "ok" ? `✅ 连通正常，模型回复: ${d.reply}` : `❌ ${d.error?.slice(0, 120)}`);
    } catch (e: any) { setMsg(e.message || String(e)); }
    setTesting(null);
  }

  async function delModel(m: ModelRow) {
    if (!confirm(`删除配置「${m.name}」？（不影响已绑定它的 Agent 的历史记录）`)) return;
    await api.del(`/models/${m.id}`).catch((e: any) => setMsg(e.message));
    await load();
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">模型配置中心</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          管理大模型供应商 · 拉取可用模型 · 为 Agent 配置驱动底座
        </p>
      </div>

      {msg && <p className="text-sm text-indigo-300">{msg}</p>}

      {/* 文档转换专用模型（省 token / 低成本） */}
      <div className="card p-4 space-y-2">
        <h3 className="font-semibold text-sm">📄 文档转换专用大模型</h3>
        <p className="text-[11px]" style={{ color: "var(--muted)" }}>
          上传的训练资料/附件将用此模型转换为结构化 Markdown 入库。建议配置低成本模型以节省开支。
        </p>
        <div className="flex gap-2 items-center">
          <select value={convertModel} onChange={(e) => setConvertModel(e.target.value)}
            className="input flex-1 !py-2 text-xs">
            <option value="">— 未设置（不使用 LLM 整理，仅规则转换） —</option>
            {(models || []).map((m: any) => (
              <option key={m.id} value={m.id}>{m.name} ({m.model_id})</option>
            ))}
          </select>
          <button onClick={async () => {
            try {
              await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/models/convert-config`, {
                method: "POST",
                headers: { "Content-Type": "application/json",
                  Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
                body: JSON.stringify({ convert_model_id: convertModel }),
              });
              setMsg("✅ 转换模型已保存");
            } catch (e: any) { setMsg("❌ " + e.message); }
          }} className="btn-primary !py-2 !px-4 text-xs shrink-0">保存</button>
        </div>
      </div>

      {/* 智能体底座说明 */}
      <div className="grid grid-cols-3 gap-3">
        {AGENT_BACKENDS.map((b, i) => (
          <div key={b.id} className="card card-static">
            <div className="flex items-center gap-2 mb-1">
              <span className="w-6 h-6 rounded-lg flex items-center justify-center text-xs text-white font-bold"
                style={{ background: `linear-gradient(135deg, ${["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#10b981,#14b8a6"][i]})` }}>
                {i + 1}
              </span>
              <span className="text-sm font-medium">{b.name}</span>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>{b.desc}</p>
          </div>
        ))}
      </div>

      {/* 从供应商拉取模型 */}
      <div className="card">
        <h2 className="text-sm font-semibold mb-3">🔌 从供应商拉取模型列表</h2>
        <div className="grid grid-cols-[1fr_220px_auto] gap-2.5 items-center">
          <input placeholder="Base URL（如 https://api.agnes-ai.cn/v1 或 http://localhost:8080/v1）"
            value={fetchUrl} onChange={(e) => setFetchUrl(e.target.value)} className="input" />
          <input placeholder="API Key（本地服务可留空）" type="password"
            value={fetchKey} onChange={(e) => setFetchKey(e.target.value)} className="input" />
          <button onClick={fetchModels} disabled={fetching || !fetchUrl}
            className="btn-primary disabled:opacity-40 whitespace-nowrap">
            {fetching ? "拉取中…" : "拉取模型"}
          </button>
        </div>

        {remoteModels.length > 0 && (
          <div className="mt-4">
            <div className="flex items-center justify-between mb-2">
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                发现 {remoteModels.length} 个模型 · 勾选要启用的：
              </p>
              <input placeholder="导入名称前缀（如 Hermes本地）" value={importPrefix}
                onChange={(e) => setImportPrefix(e.target.value)}
                className="input w-56 !py-1 text-xs" />
            </div>
            <div className="grid grid-cols-3 gap-1.5 max-h-56 overflow-y-auto p-1">
              {remoteModels.map((m) => (
                <label key={m} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs cursor-pointer transition-colors hover:bg-white/[0.04]"
                  style={{ background: checked.has(m) ? "rgba(99,102,241,0.12)" : "var(--surface-2)" }}>
                  <input type="checkbox" checked={checked.has(m)}
                    onChange={() => setChecked((prev) => {
                      const n = new Set(prev); if (n.has(m)) n.delete(m); else n.add(m); return n;
                    })}
                    className="accent-indigo-500 w-3.5 h-3.5 shrink-0" />
                  <span className="truncate">{m}</span>
                </label>
              ))}
            </div>
            <button onClick={importSelected} disabled={!checked.size}
              className="btn-primary mt-3 disabled:opacity-40">
              导入选中的 {checked.size} 个模型
            </button>
          </div>
        )}
      </div>

      {/* 已配置模型列表 */}
      <div className="card card-static">
        <h2 className="text-sm font-semibold mb-3">已配置的模型（{models.length}）</h2>
        <div className="space-y-2">
          {models.map((m) => (
            <div key={m.id} className="flex items-center gap-3 px-3 py-2.5 rounded-xl transition-colors hover:bg-white/[0.03]"
              style={{ background: "var(--surface-2)" }}>
              <span className="w-2 h-2 rounded-full bg-emerald-400 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="text-sm truncate">{m.name}</div>
                <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                  {PROVIDER_LABELS[m.provider_type] || m.provider_type} · {m.model_id}
                  {m.base_url ? ` · ${m.base_url}` : ""}
                </div>
              </div>
              <span className="text-[11px] px-2 py-0.5 rounded-md shrink-0"
                style={{ background: "rgba(99,102,241,0.15)", color: "#a5b4fc" }}>
                {usage[m.id] || 0} 名员工使用
              </span>
              <button onClick={() => testModel(m.id)} disabled={testing === m.id}
                className="btn-ghost text-xs py-1 shrink-0">
                {testing === m.id ? "测试中…" : "测试"}
              </button>
              <button onClick={() => delModel(m)}
                className="text-xs py-1 px-2 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10 shrink-0">
                删除
              </button>
            </div>
          ))}
          {!models.length && (
            <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
              还没有配置模型，先从上方拉取或手动添加
            </p>
          )}
        </div>
      </div>

      {/* 手动添加 */}
      <details className="card card-static">
        <summary className="text-sm cursor-pointer select-none" style={{ color: "var(--muted)" }}>
          ➕ 手动添加模型配置（高级）
        </summary>
        <div className="grid grid-cols-2 gap-2.5 mt-3">
          <input placeholder="显示名（如 Agnes Flash）" value={provName}
            onChange={(e) => setProvName(e.target.value)} className="input" />
          <select value={provType} onChange={(e) => setProvType(e.target.value)} className="input">
            {Object.entries(PROVIDER_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <input placeholder="Base URL" value={provUrl}
            onChange={(e) => setProvUrl(e.target.value)} className="input" />
          <input placeholder="API Key 环境变量名（如 HERMES_API_KEY）" value={provKeyEnv}
            onChange={(e) => setProvKeyEnv(e.target.value)} className="input" />
          <input placeholder="模型 ID（如 agnes-2.5-flash / claude-sonnet-4）" value={provModelId}
            onChange={(e) => setProvModelId(e.target.value)} className="input" />
          <button onClick={addModel} disabled={!provName || !provModelId}
            className="btn-primary disabled:opacity-40">添加配置</button>
        </div>
      </details>
    </div>
  );
}
