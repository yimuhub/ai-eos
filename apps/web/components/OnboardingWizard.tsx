"use client";

// 新用户冷启动向导。
//
// ## 为什么必须有它
//
// 改造前的真实体验：注册 → 登录 → 看到一片空白页（首页在没有公司时
// 直接 return，不渲染任何内容），左侧 35 个导航项全部点不出东西。
// 用户既不知道"要先建公司"，也不知道"建完公司还得配模型和密钥"——
// 而缺了模型，任何任务都会失败。
//
// 这个向导把冷启动拆成 3 个可理解、可跳过、有明确结果反馈的步骤：
//   1. 创建 AI 公司（否则没有归属，一切数据无处安放）
//   2. 配置模型（否则员工无法思考）
//   3. 组建初始团队（可选，但能立刻看到"有人干活"的效果）
//
// 关于密钥：出于安全考虑，密钥只存在后端 .env 里，不通过接口写入。
// 因此这一步给出明确的文件与变量名提示，并提供"重新检测"。

import { useCallback, useEffect, useState } from "react";
import { api, setActiveCompanyId } from "@/lib/api";
import { notify } from "@/components/Toast";
import DemoMode from "@/components/DemoMode";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

type Diagnostics = {
  model_count: number;
  usable_model_ids: string[];
  ready: boolean;
  hint: string;
  providers: {
    provider_type: string;
    default_env_var: string;
    key_present: boolean;
    model_count: number;
  }[];
};

type Template = { key: string; name: string; builtin: boolean; departments: number; agents: number };

type Company = { id: string; name: string };

/** 常见模型服务预设，降低"该填什么"的门槛。
 *  key_hint：一句话告诉小白"Key 从哪来"，否则默认用户已有 Key 是天堑。 */
const PRESETS = [
  {
    label: "Agnes（推荐 · 国内可用）",
    name: "Agnes Flash",
    provider_type: "openai_compat",
    base_url: "https://api.agnes-ai.cn/v1",
    model_id: "agnes-2.5-flash",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 agnes-ai.cn 注册账号 → 控制台「API 密钥」页面复制 Key，新用户有免费额度可先试。",
    custom: false,
  },
  {
    label: "OpenAI 官方",
    name: "OpenAI GPT",
    provider_type: "openai_compat",
    base_url: "https://api.openai.com/v1",
    model_id: "gpt-4o-mini",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 platform.openai.com 注册 → 右上角头像「API keys」新建并复制 Key（需境外网络）。",
    custom: false,
  },
  {
    label: "DeepSeek",
    name: "DeepSeek Chat",
    provider_type: "openai_compat",
    base_url: "https://api.deepseek.com/v1",
    model_id: "deepseek-chat",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 platform.deepseek.com 注册 → 左侧「API keys」创建并复制 Key，价格相对便宜。",
    custom: false,
  },
  {
    label: "Anthropic Claude",
    name: "Claude",
    provider_type: "anthropic",
    base_url: "https://api.anthropic.com",
    model_id: "claude-3-5-sonnet-latest",
    api_key_env: "ANTHROPIC_API_KEY",
    key_hint: "去 console.anthropic.com 注册 → 「API Keys」页面创建并复制 Key（需境外网络）。",
    custom: false,
  },
  {
    label: "自定义 API",
    name: "",
    provider_type: "openai_compat",
    base_url: "",
    model_id: "",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "任何 OpenAI 兼容接口都行（vLLM / Ollama / OneAPI / 自建网关）。地址填到 /v1 为止。",
    custom: true,
  },
];

export default function OnboardingWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);

  // ---- 步骤 1：公司 ----
  const [companyName, setCompanyName] = useState("");
  const [company, setCompany] = useState<Company | null>(null);
  const [companyAttachments, setCompanyAttachments] = useState<Attachment[]>([]); // 📎 公司资料

  // ---- 步骤 2：模型 ----
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);
  const [presetIdx, setPresetIdx] = useState(0);
  const [mForm, setMForm] = useState({ ...PRESETS[0] });
  const [modelCreated, setModelCreated] = useState(false);
  // 模型 ID 优先从服务商读取后下拉选择；读不到再允许手填
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [listing, setListing] = useState(false);
  const [manualModel, setManualModel] = useState(false);
  const [listError, setListError] = useState("");

  // ---- 演示模式（免 Key）----
  const [demoOpen, setDemoOpen] = useState(false);

  // ---- 密钥就地保存（写本机 .env，立即生效）----
  const [keyInput, setKeyInput] = useState("");
  const [keySaving, setKeySaving] = useState(false);
  const [keyMasked, setKeyMasked] = useState("");

  // ---- 步骤 3：团队 ----
  const [templates, setTemplates] = useState<Template[]>([]);
  const [tplKey, setTplKey] = useState("general");
  const [teamDone, setTeamDone] = useState(false);

  const refreshDiag = useCallback(async () => {
    setDiagLoading(true);
    try {
      setDiag(await api.get<Diagnostics>("/models/diagnostics"));
    } catch (e) {
      notify("error", e instanceof Error ? e.message : "检测模型配置失败");
    } finally {
      setDiagLoading(false);
    }
  }, []);

  // 进入第 2 步时检测模型状态；进入第 3 步时拉模板
  useEffect(() => {
    if (step === 2) refreshDiag();
    if (step === 3 && !templates.length) {
      api.get<Template[]>("/governance/templates")
        .then((t) => setTemplates(Array.isArray(t) ? t.filter((x) => x.builtin) : []))
        .catch(() => setTemplates([]));
    }
  }, [step, refreshDiag, templates.length]);

  // ---------- 步骤 1：创建公司 ----------
  async function createCompany() {
    const name = companyName.trim();
    if (!name) {
      notify("warning", "请填写公司名称");
      return;
    }
    setBusy(true);
    try {
      const c = await api.post<Company>("/companies", {
        name,
        attachments: companyAttachments.length ? companyAttachments : undefined, // 📎 公司资料
      });
      setCompany(c);
      setActiveCompanyId(c.id);
      notify("success", `公司「${c.name}」已创建`);
      setStep(2);
    } catch (e) {
      notify("error", e instanceof Error ? e.message : "创建公司失败");
    } finally {
      setBusy(false);
    }
  }

  // ---------- 步骤 2：登记模型 ----------
  async function createModel() {
    if (!mForm.base_url?.trim() || !mForm.model_id?.trim()) {
      notify("warning", "请填写接口地址，并读取或选择模型");
      return;
    }
    setBusy(true);
    try {
      await api.post("/models", {
        name: mForm.name.trim() || mForm.model_id,
        provider_type: mForm.provider_type,
        base_url: mForm.base_url.trim(),
        model_id: mForm.model_id.trim(),
        api_key_env: mForm.api_key_env,
        config: {},
      });
      setModelCreated(true);
      notify("success", "模型已登记，正在检测密钥…");
      await refreshDiag();
    } catch (e) {
      notify("error", e instanceof Error ? e.message : "登记模型失败");
    } finally {
      setBusy(false);
    }
  }

  // ---------- 步骤 2：从服务商读取可用模型 ----------
  async function fetchModels() {
    const base = mForm.base_url.trim();
    if (!base) {
      notify("warning", "请先填写接口地址");
      return;
    }
    setListing(true);
    setListError("");
    try {
      const r = await api.post<{ status: string; models?: string[]; error?: string }>(
        "/models/providers/list-models",
        {
          base_url: base,
          api_key: keyInput.trim() || undefined,
          provider_type: mForm.provider_type,
        },
      );
      const list = r?.status === "ok" ? (r.models ?? []) : [];
      if (!list.length) {
        setModelOptions([]);
        setListError(r?.error || "没有读到模型，可手动填写模型 ID");
        return;
      }
      setModelOptions(list);
      setManualModel(false);
      setMForm((f) => ({
        ...f,
        model_id: list.includes(f.model_id) ? f.model_id : list[0],
        name:
          f.name.trim() && f.name !== f.model_id
            ? f.name
            : list.includes(f.model_id)
              ? f.name
              : list[0],
      }));
    } catch (e) {
      setListError(e instanceof Error ? e.message : "读取模型列表失败");
    } finally {
      setListing(false);
    }
  }

  // ---------- 步骤 3：套用模板组建团队 ----------
  async function applyTemplate() {
    if (!company) return;
    setBusy(true);
    try {
      const r = await api.post<{ created_agents?: unknown[] }>(
        `/governance/create-company-from-template?owner_company_id=${company.id}`,
        { template_key: tplKey },
      );
      const n = Array.isArray(r?.created_agents) ? r.created_agents.length : undefined;
      setTeamDone(true);
      notify("success", n ? `已组建 ${n} 名数字员工` : "团队已组建");
    } catch (e) {
      notify("error", e instanceof Error ? e.message : "套用模板失败");
    } finally {
      setBusy(false);
    }
  }

  const modelReady = diag?.ready === true;

  /**
   * 直接在界面上保存密钥：后端写入本机 .env（600 权限）并立即生效。
   * 过去这里教用户"打开 apps/api/.env 手动粘贴再重启"——对普通用户是天堑，
   * 结果就是模型登记了、任务却永远失败在「缺少密钥」。
   */
  async function saveKey() {
    const key = keyInput.trim();
    if (key.length < 8) {
      notify("warning", "请粘贴完整的 API Key（至少 8 个字符）");
      return;
    }
    setKeySaving(true);
    try {
      const r = await api.post<{ masked: string }>("/models/env-key", {
        env_name: mForm.api_key_env || "OPENAI_COMPAT_API_KEY",
        api_key: key,
      });
      setKeyMasked(r.masked);
      setKeyInput("");
      notify("success", "密钥已保存到本机 .env，立即生效");
      await refreshDiag();
    } catch (e) {
      notify("error", e instanceof Error ? e.message : "保存密钥失败");
    } finally {
      setKeySaving(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto py-10 px-4 animate-fade-up">
      {/* 欢迎语 */}
      <div className="text-center mb-8">
        <h1 className="text-3xl font-bold tracking-tight">👋 欢迎使用 AI-EOS</h1>
        <p className="text-sm mt-2" style={{ color: "var(--muted)" }}>
          三步之后，你就拥有一支能替你干活的数字员工团队。全程约 1 分钟。
        </p>
        <button onClick={() => setDemoOpen(true)} className="btn-ghost text-xs mt-3">
          🎭 先体验演示（免密钥，约 30 秒）
        </button>
      </div>
      {demoOpen && <DemoMode onClose={() => setDemoOpen(false)} />}

      {/* 步骤指示 */}
      <div className="flex items-center justify-center gap-2 mb-8">
        {[
          { n: 1, label: "创建公司" },
          { n: 2, label: "配置模型" },
          { n: 3, label: "组建团队" },
        ].map((s, i) => (
          <div key={s.n} className="flex items-center gap-2">
            <div
              className="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs transition-all"
              style={{
                background: step === s.n ? "rgba(99,102,241,0.18)" : "rgba(255,255,255,0.03)",
                border: `1px solid ${step >= s.n ? "var(--accent)" : "var(--border)"}`,
                color: step >= s.n ? "var(--foreground)" : "var(--muted)",
              }}
            >
              <span
                className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold"
                style={{ background: step > s.n ? "var(--success)" : "transparent",
                         border: step > s.n ? "none" : "1px solid currentColor" }}
              >
                {step > s.n ? "✓" : s.n}
              </span>
              {s.label}
            </div>
            {i < 2 && <span style={{ color: "var(--border)" }}>—</span>}
          </div>
        ))}
      </div>

      {/* ---------------- 步骤 1 ---------------- */}
      {step === 1 && (
        <div className="card card-static p-5 space-y-4">
          <div>
            <h2 className="font-semibold text-lg">给你的 AI 公司起个名字</h2>
            <p className="text-xs mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
              公司是所有数据（员工、任务、成果、知识库）的归属单元。
              你可以创建多家公司，并在指挥中心随时切换。
            </p>
          </div>
          <input
            autoFocus
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createCompany()}
            placeholder="例如：星河文化传媒"
            className="input"
          />
          <AttachmentPicker value={companyAttachments} onChange={setCompanyAttachments}
            label="📎 挂公司资料（营业执照 / 业务规划 / 品牌手册，可选）" />
          <div className="flex justify-end">
            <button onClick={createCompany} disabled={busy || !companyName.trim()}
              className="btn-primary disabled:opacity-40">
              {busy ? "创建中…" : "创建公司 →"}
            </button>
          </div>
        </div>
      )}

      {/* ---------------- 步骤 2 ---------------- */}
      {step === 2 && (
        <div className="card card-static p-5 space-y-4">
          <div>
            <h2 className="font-semibold text-lg">配置一个能干活的模型</h2>
            <p className="text-xs mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
              数字员工的「大脑」来自大模型服务。没有它，员工接不了任务。
              如果你已经有可用的 API Key，这一步 30 秒就能完成。
            </p>
          </div>

          {/* 当前状态 */}
          <div
            className="rounded-xl px-3 py-2.5 text-xs leading-relaxed"
            style={{
              background: modelReady ? "rgba(52,211,153,0.1)" : "rgba(251,191,36,0.1)",
              border: `1px solid ${modelReady ? "rgba(52,211,153,0.35)" : "rgba(251,191,36,0.35)"}`,
            }}
          >
            {diagLoading ? (
              <span style={{ color: "var(--muted)" }}>正在检测…</span>
            ) : modelReady ? (
              <span style={{ color: "var(--success)" }}>
                ✅ 模型已就绪（{diag?.usable_model_ids.length} 个可用），可以开始使用。
              </span>
            ) : (
              <>
                <div style={{ color: "var(--warning)" }}>⚠️ {diag?.hint || "模型尚未就绪"}</div>
                {diag && diag.model_count > 0 && (
                  <div className="mt-2 space-y-1">
                    {diag.providers.map((p) => (
                      <div key={p.provider_type} style={{ color: "var(--muted)" }}>
                        · {p.provider_type}：密钥 {p.key_present ? "✅ 已配置" : "❌ 未检测到"}
                        <span className="ml-1">（环境变量 <code>{p.default_env_var}</code>）</span>
                      </div>
                    ))}
                    <div className="mt-2 rounded-lg p-2.5 space-y-1.5 text-[11px] leading-relaxed"
                      style={{ background: "rgba(0,0,0,0.3)", color: "var(--muted)" }}>
                      <p className="font-medium" style={{ color: "var(--foreground)" }}>
                        把 Key 交给后端（只存在你自己的机器上，不会上传）
                      </p>
                      <div className="flex gap-1.5">
                        <input
                          type="password"
                          value={keyInput}
                          onChange={(e) => setKeyInput(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && void saveKey()}
                          placeholder={`粘贴 ${mForm.api_key_env} 的值`}
                          autoComplete="off"
                          spellCheck={false}
                          className="input !py-1.5 text-[11px] flex-1"
                          aria-label="API Key"
                        />
                        <button
                          onClick={() => void saveKey()}
                          disabled={keySaving || !keyInput.trim()}
                          className="btn-primary !py-1.5 text-[11px] shrink-0 disabled:opacity-40"
                        >
                          {keySaving ? "保存中…" : "保存并生效"}
                        </button>
                      </div>
                      <p>
                        保存为 <code>{mForm.api_key_env}</code>，写入本机 <code>apps/api/.env</code>
                        （权限 600），立即生效，无需重启。
                      </p>
                      {keyMasked && (
                        <p style={{ color: "var(--success)" }}>已保存：{keyMasked}</p>
                      )}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>

          {/* 预设 */}
          {!modelCreated && (
            <>
              <div>
                <label className="text-xs mb-1.5 block" style={{ color: "var(--muted)" }}>
                  选择服务商预设（填好地址和 Key 后点「读取模型」）
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {PRESETS.map((p, i) => (
                    <button
                      key={p.label}
                      onClick={() => {
                        setPresetIdx(i);
                        setMForm({ ...p });
                        setModelOptions([]);
                        setListError("");
                        setManualModel(p.custom);
                      }}
                      className="px-2.5 py-1 rounded-lg text-[11px] border transition-all"
                      style={{
                        borderColor: presetIdx === i ? "var(--accent)" : "var(--border)",
                        background: presetIdx === i ? "rgba(99,102,241,0.15)" : "transparent",
                        color: presetIdx === i ? "var(--foreground)" : "var(--muted)",
                      }}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                {mForm.key_hint && (
                  <p className="text-[11px] mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
                    🔑 Key 从哪来：{mForm.key_hint}
                  </p>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                <input value={mForm.base_url}
                  onChange={(e) => {
                    setMForm({ ...mForm, base_url: e.target.value });
                    setModelOptions([]);
                    setListError("");
                  }}
                  placeholder="接口地址 https://.../v1" className="input sm:col-span-2" />
                <div className="sm:col-span-2 flex gap-2">
                  {modelOptions.length > 0 && !manualModel ? (
                    <select className="input flex-1 min-w-0"
                      value={mForm.model_id}
                      onChange={(e) => {
                        const id = e.target.value;
                        setMForm((f) => ({
                          ...f,
                          model_id: id,
                          name: f.name.trim() && f.name !== f.model_id ? f.name : id,
                        }));
                      }}>
                      <option value="" disabled>选择一个模型</option>
                      {modelOptions.map((id) => (
                        <option key={id} value={id}>{id}</option>
                      ))}
                    </select>
                  ) : (
                    <input value={mForm.model_id}
                      onChange={(e) => setMForm({ ...mForm, model_id: e.target.value })}
                      placeholder={manualModel ? "手动填写模型 ID" : "点右侧「读取模型」自动获取"}
                      className="input flex-1 min-w-0"
                      disabled={!manualModel} />
                  )}
                  <button onClick={fetchModels} disabled={listing || !mForm.base_url.trim()}
                    className="btn-ghost text-xs shrink-0 disabled:opacity-40">
                    {listing ? "读取中…" : modelOptions.length ? "重新读取" : "读取模型"}
                  </button>
                </div>
                <div className="sm:col-span-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px]"
                  style={{ color: "var(--muted)" }}>
                  {modelOptions.length > 0 && !manualModel ? (
                    <span style={{ color: "var(--success)" }}>✓ 已读取 {modelOptions.length} 个模型</span>
                  ) : (
                    <span>{manualModel ? "手动填写模式" : "先填接口地址，再点「读取模型」"}</span>
                  )}
                  <button onClick={() => setManualModel((s) => !s)} className="underline underline-offset-2"
                    style={{ color: "var(--muted)" }}>
                    {manualModel ? "改为从列表选择" : "服务商不支持读取？手动填写"}
                  </button>
                  {listError && <span style={{ color: "var(--warning)" }}>{listError}</span>}
                </div>
                <input value={mForm.name} onChange={(e) => setMForm({ ...mForm, name: e.target.value })}
                  placeholder="显示名称（留空用模型 ID）" className="input" />
                <input value={mForm.api_key_env} onChange={(e) => setMForm({ ...mForm, api_key_env: e.target.value })}
                  placeholder="密钥所在的环境变量名" className="input" />
              </div>

              <button onClick={createModel} disabled={busy} className="btn-primary disabled:opacity-40">
                {busy ? "登记中…" : "登记模型"}
              </button>
            </>
          )}

          <div className="flex justify-between items-center pt-1">
            <button
              onClick={() => setStep(3)}
              className="text-xs"
              style={{ color: "var(--muted)" }}
            >
              先跳过，稍后在「模型配置」里设置
            </button>
            <div className="flex gap-2">
              {modelCreated && (
                <button onClick={refreshDiag} disabled={diagLoading} className="btn-ghost text-xs">
                  {diagLoading ? "检测中…" : "重新检测"}
                </button>
              )}
              <button onClick={() => setStep(3)} className="btn-primary">下一步 →</button>
            </div>
          </div>
        </div>
      )}

      {/* ---------------- 步骤 3 ---------------- */}
      {step === 3 && (
        <div className="card card-static p-5 space-y-4">
          <div>
            <h2 className="font-semibold text-lg">组建你的初始团队</h2>
            <p className="text-xs mt-1.5 leading-relaxed" style={{ color: "var(--muted)" }}>
              选一个模板，一键生成对应的部门与数字员工（含职责与人格设定）。
              新建的员工已自动绑定首选模型（你在上一步登记的第一个模型），
              可在员工页随时调整。之后也可以随时增删调整团队。
            </p>
          </div>

          <div className="space-y-2">
            {templates.length === 0 && (
              <p className="text-xs" style={{ color: "var(--muted)" }}>正在加载模板…</p>
            )}
            {templates.map((t) => {
              const active = tplKey === t.key;
              return (
                <button
                  key={t.key}
                  onClick={() => setTplKey(t.key)}
                  className="w-full text-left rounded-xl px-3 py-2.5 border transition-all"
                  style={{
                    borderColor: active ? "var(--accent)" : "var(--border)",
                    background: active ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)",
                  }}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{t.name}</span>
                    {active && <span className="text-xs" style={{ color: "var(--accent-2)" }}>✓ 已选</span>}
                  </div>
                  <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                    {t.departments} 个部门 · {t.agents} 名员工
                  </p>
                </button>
              );
            })}
          </div>

          {teamDone && (
            <div className="rounded-xl px-3 py-2 text-xs"
              style={{ background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.35)",
                       color: "var(--success)" }}>
              ✅ 团队已就位。点击下方「进入指挥中心」开始下达任务。
            </div>
          )}

          <div className="flex justify-between items-center pt-1">
            <button onClick={() => setStep(2)} className="text-xs" style={{ color: "var(--muted)" }}>
              ← 上一步
            </button>
            <div className="flex gap-2">
              {!teamDone && (
                <button onClick={applyTemplate} disabled={busy || !templates.length}
                  className="btn-ghost text-xs disabled:opacity-40">
                  {busy ? "组建中…" : "套用该模板"}
                </button>
              )}
              <button onClick={onDone} className="btn-primary">
                {teamDone ? "进入指挥中心 →" : "先跳过，直接进入 →"}
              </button>
            </div>
          </div>
        </div>
      )}

      <p className="text-center text-[11px] mt-6" style={{ color: "var(--muted)" }}>
        所有数据保存在你自己的机器上（SQLite）。你的密钥只写在本地 .env，不会上传。
      </p>
    </div>
  );
}
