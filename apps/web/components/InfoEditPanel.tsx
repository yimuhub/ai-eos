"use client";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";

/* ============================================================
   员工信息编辑面板（完整复刻列表页编辑弹窗全部字段）
   岗位/状态/部门/上级/驱动模型/执行底座/能力包/SystemPrompt
   ============================================================ */

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const auth = () => ({
  Authorization: `Bearer ${localStorage.getItem("aieos_token")}`,
  "Content-Type": "application/json",
});

const STATUS_ZH: Record<string, string> = {
  IDLE: "空闲", ACTIVE: "在线", RUNNING: "工作中", PAUSED: "已暂停",
  DISABLED: "已禁用", ERROR: "异常",
};

const BACKENDS = [
  { key: "", label: "原生大模型", description: "直接由配置的大模型 API 驱动（当前默认）" },
  { key: "hermes", label: "Hermes Agent", description: "由本机 Hermes Agent 驱动：完整工具链、技能系统、文件操作" },
  { key: "codex", label: "Codex Agent", description: "由 OpenAI Codex CLI 驱动：强代码编写、GitHub 操作" },
];

const CAP_PACKS = [
  { key: "computer_use", label: "🖥 电脑操作", risk: "high" },
  { key: "web_browsing", label: "🌐 网页浏览", risk: "medium" },
  { key: "persistent_memory", label: "🧠 持久记忆", risk: "low" },
  { key: "self_learning", label: "📈 自我学习", risk: "medium" },
  { key: "scheduled_work", label: "⏰ 周期性工作", risk: "low" },
];

type FormState = {
  role: string; status: string; department_id: string;
  manager_agent_id: string; model_id: string; backend: string;
  capabilities: Set<string>; system_prompt: string;
};

export default function InfoEditPanel({ agentId, onClose, onSaved }: {
  agentId: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [form, setForm] = useState<FormState>({
    role: "", status: "IDLE", department_id: "",
    manager_agent_id: "", model_id: "", backend: "",
    capabilities: new Set(), system_prompt: "",
  });
  const [agents, setAgents] = useState<any[]>([]);
  const [depts, setDepts] = useState<any[]>([]);
  const [models, setModels] = useState<any[]>([]);
  const [saving, setSaving] = useState(false);
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    (async () => {
      try {
        // 并行拉取：当前 Agent、全员、部门、模型
        const [curRes, agRes, csRes, mRes] = await Promise.all([
          fetch(`${API}/agents/${agentId}`, { headers: auth() }),
          fetch(`${API}/agents`, { headers: auth() }),
          fetch(`${API}/companies`, { headers: auth() }),
          fetch(`${API}/models`, { headers: auth() }),
        ]);
        const cur = await curRes.json();
        setAgents(await agRes.json());
        const cs = await csRes.json();
        if (cs[0]?.id) {
          fetch(`${API}/companies/${cs[0].id}/departments`,
            { headers: auth() }).then((r) => r.json()).then(setDepts).catch(() => {});
        }
        setModels(await mRes.json());

        // 解析能力包
        const caps = new Set<string>(cur.memory_config?.capabilities || []);
        setForm({
          role: cur.role || "", status: cur.status || "IDLE",
          department_id: cur.department_id || "",
          manager_agent_id: cur.manager_agent_id || "",
          model_id: cur.model_configs?.[0]?.model_id || "",
          backend: cur.memory_config?.backend || "",
          capabilities: caps,
          system_prompt: cur.memory_config?.system_prompt || "",
        });
      } catch (e: any) { setMsg(`加载失败: ${e.message}`); }
    })();
  }, [agentId]);

  async function save() {
    setSaving(true); setMsg("");
    try {
      const curRes = await fetch(`${API}/agents/${agentId}`, { headers: auth() });
      const cur = await curRes.json();
      const cfg = { ...(cur.memory_config || {}) };
      cfg.backend = form.backend || undefined;
      cfg.capabilities = Array.from(form.capabilities);
      if (form.system_prompt) cfg.system_prompt = form.system_prompt;

      await fetch(`${API}/agents/${agentId}`, {
        method: "PATCH", headers: auth(),
        body: JSON.stringify({
          role: form.role, status: form.status,
          department_id: form.department_id || null,
          manager_agent_id: form.manager_agent_id || null,
          memory_config: cfg,
        }),
      });

      // 模型绑定变更时调用 switch-model
      const currentModel = cur.model_configs?.[0]?.model_id;
      if (form.model_id && form.model_id !== currentModel) {
        await fetch(`${API}/agents/${agentId}/switch-model`, {
          method: "PATCH", headers: auth(),
          body: JSON.stringify({ model_id: form.model_id }),
        });
      }
      setMsg("✅ 已保存");
      onSaved?.();
      setTimeout(onClose, 600);
    } catch (e: any) { setMsg(`❌ ${e.message}`); }
    setSaving(false);
  }

  async function deleteAgent() {
    if (!(await confirmAction({
      title: "确认删除此员工？",
      description: "该员工的档案会一并销毁，请确认你已不需要它的任何成果。",
      bullets: ["全部关联数据将同步销毁", "此操作不可恢复"],
      confirmPhrase: "删除",
      confirmText: "删除",
    }))) return;
    setSaving(true);
    try {
      const r = await fetch(`${API}/agents/${agentId}`,
        { method: "DELETE", headers: auth() });
      if (r.status === 204 || r.ok) {
        // 原来用 alert()：阻塞式弹窗、浅色系统样式，与产品观感割裂
        notify("success", "已删除该员工，正在返回列表…");
        window.location.href = "/agents";
      } else {
        setMsg(`❌ 删除失败 HTTP ${r.status}`);
      }
    } catch (e: any) { setMsg(`❌ ${e.message}`); }
    setSaving(false);
  }

  const others = agents.filter((a) => a.id !== agentId);

  if (!mounted) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
      onClick={onClose}>
      <div className="w-full max-w-3xl space-y-3 rounded-2xl p-5 max-h-[90vh] overflow-y-auto animate-fade-up"
        style={{ background: "linear-gradient(180deg,#15151f,#101018)",
                border: "1px solid rgba(99,102,241,0.35)" }}
        onClick={(e) => e.stopPropagation()}>
      <div className="flex items-center justify-between pb-2 border-b" style={{ borderColor: "var(--border)" }}>
        <h3 className="font-semibold text-sm">👤 员工信息设置</h3>
        <button onClick={onClose} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
      </div>

      {msg && <p className="text-xs text-indigo-300">{msg}</p>}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        <div>
          <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>岗位</label>
          <input value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
            placeholder="如：战略规划专家" className="input" />
        </div>
        <div>
          <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>状态</label>
          <select value={form.status}
            onChange={(e) => setForm({ ...form, status: e.target.value })}
            className="input">
            {Object.entries(STATUS_ZH).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>))}
          </select>
        </div>
        <div>
          <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🏢 部门归属</label>
          <select value={form.department_id}
            onChange={(e) => setForm({ ...form, department_id: e.target.value })}
            className="input">
            <option value="">未分配部门</option>
            {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
          </select>
        </div>
        <div>
          <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>↳ 直接上级</label>
          <select value={form.manager_agent_id}
            onChange={(e) => setForm({ ...form, manager_agent_id: e.target.value })}
            className="input">
            <option value="">（无上级 · 顶层）</option>
            {others.map((x) => (
              <option key={x.id} value={x.id}>{x.name}{x.role ? ` · ${x.role}` : ""}</option>
            ))}
          </select>
        </div>
      </div>

      {/* 模型 */}
      <div>
        <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🧬 驱动大模型</label>
        <select value={form.model_id}
          onChange={(e) => setForm({ ...form, model_id: e.target.value })}
          className="input">
          <option value="">未绑定模型</option>
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.name} ({m.provider_type} · {m.model_id})</option>
          ))}
        </select>
        <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
          更多供应商可在「智能体 → 模型配置」页添加
        </p>
      </div>

      {/* 执行底座 */}
      <div>
        <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>🤖 执行底座（本地智能体赋能）</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {BACKENDS.map((b) => (
            <button key={b.key} type="button"
              onClick={() => setForm({ ...form, backend: b.key })}
              className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                form.backend === b.key ? "border-indigo-500 bg-indigo-500/15" : "hover:bg-white/[0.04]"}`}
              style={form.backend === b.key ? {} : { borderColor: "var(--border)" }}>
              <div className="font-medium">{b.label}</div>
              <div className="mt-0.5 opacity-70 line-clamp-1" style={{ color: "var(--muted)" }}>{b.description}</div>
            </button>
          ))}
        </div>
      </div>

      {/* 能力包 */}
      <div>
        <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>⚡ 智能体能力包（可随时增减）</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {CAP_PACKS.map((c) => {
            const on = form.capabilities.has(c.key);
            return (
              <button key={c.key} type="button"
                onClick={() => setForm((prev) => {
                  const n = new Set(prev.capabilities);
                  if (n.has(c.key)) { n.delete(c.key); } else { n.add(c.key); }
                  return { ...prev, capabilities: n };
                })}
                className={`text-left px-3 py-2 rounded-xl text-xs transition-all border ${
                  on ? "border-emerald-500/60 bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}
                style={on ? {} : { borderColor: "var(--border)" }}>
                <div className="flex items-center justify-between">
                  <span>{c.label}</span>
                  {on && <span className="text-emerald-400">✓</span>}
                  {c.risk === "high" && !on && <span className="text-[9px] px-1 rounded bg-red-500/20 text-red-300">高危</span>}
                  {c.risk === "medium" && !on && <span className="text-[9px] px-1 rounded bg-amber-500/20 text-amber-300">中风险</span>}
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* System Prompt */}
      <div>
        <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>📝 System Prompt（人格与职责）</label>
        <textarea rows={2} value={form.system_prompt}
          onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
          placeholder="留空使用默认人格" className="input" />
      </div>

      {/* 操作区 */}
      <div className="flex items-center gap-2 pt-2 border-t" style={{ borderColor: "var(--border)" }}>
        <button onClick={save} disabled={saving}
          className="btn-primary disabled:opacity-40">
          {saving ? "保存中…" : "确定保存"}
        </button>
        <div className="flex-1" />
        <button onClick={deleteAgent} disabled={saving}
          className="text-xs py-1.5 px-3 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10 transition-colors"
          style={{ border: "1px solid rgba(248,113,113,0.25)" }}>
          🗑 删除此员工
        </button>
      </div>
      </div>
    </div>,
    document.body
  );
}