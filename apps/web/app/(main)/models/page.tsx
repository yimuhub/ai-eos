"use client";
import { useEffect, useMemo, useState } from "react";
import { api, describeError, getActiveCompanyId } from "@/lib/api";
import { useActiveCompanyId, useResource, invalidate } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import type {
  Agent, ConvertConfig, DiscoveredModel, ModelConfig, ModelProvider,
  ModelTestResult, ProviderDiscoverResult, ProviderImportResult,
} from "@/lib/types";
import Field from "@/components/Field";
import { notify } from "@/components/Toast";

/* ============================================================
   模型配置中心：供应商（地址 + 密钥）→ 拉取模型 → 选择与配置
   一个供应商可以拉取任意多个模型；模型按文本/图片/视频/音频/向量分类，
   每个模型带一份按类型生成的默认配置，可逐项手改。
   ============================================================ */

const PROVIDER_LABELS: Record<string, string> = {
  openai_compat: "OpenAI 兼容",
  anthropic: "Anthropic",
  gemini: "Gemini",
};

const PROVIDER_HINTS: Record<string, string> = {
  openai_compat: "任意兼容 /v1/models 的服务：OpenAI、DeepSeek、通义、本地 Ollama 等",
  anthropic: "官方地址可留空，默认 https://api.anthropic.com",
  gemini: "官方地址可留空，默认 Google Generative Language API",
};

const TYPE_ORDER = ["text", "image", "video", "audio", "embedding", "other"];

const TYPE_META: Record<string, { label: string; icon: string; color: string; bg: string }> = {
  text: { label: "文本", icon: "💬", color: "#a5b4fc", bg: "rgba(99,102,241,0.16)" },
  image: { label: "图片", icon: "🎨", color: "#f9a8d4", bg: "rgba(236,72,153,0.16)" },
  video: { label: "视频", icon: "🎬", color: "#fdba74", bg: "rgba(249,115,22,0.16)" },
  audio: { label: "音频", icon: "🎧", color: "#67e8f9", bg: "rgba(6,182,212,0.16)" },
  embedding: { label: "向量", icon: "🧮", color: "#6ee7b7", bg: "rgba(16,185,129,0.16)" },
  other: { label: "其他", icon: "•", color: "#cbd5e1", bg: "rgba(148,163,184,0.16)" },
};

// 配置项的展示语言。上游可能给很多参数，这里只把常用项翻译成人话；
// 不认识的键仍会原样列出，避免"看不见的配置"。
const CONFIG_LABELS: Record<string, string> = {
  temperature: "温度",
  max_tokens: "最大输出 Token",
  context_window: "上下文窗口",
  max_output_tokens: "上游输出上限",
  supports_streaming: "支持流式输出",
  default_size: "默认图片尺寸",
  sizes: "可选尺寸（逗号分隔）",
  quality: "图片质量",
  response_format: "返回格式",
  max_images: "单次最多生成张数",
  default_duration: "默认时长（秒）",
  durations: "可选时长（秒）",
  resolutions: "可选分辨率",
  aspect_ratios: "可选画幅",
  fps: "帧率",
  default_format: "默认音频格式",
  formats: "可选格式",
  default_voice: "默认音色",
  voices: "可用音色",
  batch_size: "批处理大小",
  dimensions: "向量维度",
};

const CONFIG_HINTS: Record<string, string> = {
  temperature: "越高越发散，0–2；代码/事实类建议 0.1–0.3",
  max_tokens: "单次回复的最大长度；越大越贵",
  context_window: "上游声明的上下文长度，只读参考",
  dimensions: "留空表示使用模型默认维度",
  response_format: "url 返回可访问链接；b64_json 返回 base64",
};

const CONFIG_ORDER = [
  "temperature", "max_tokens", "context_window", "max_output_tokens",
  "supports_streaming", "default_size", "sizes", "quality",
  "response_format", "max_images", "default_duration", "durations",
  "resolutions", "aspect_ratios", "fps", "default_format", "formats",
  "default_voice", "voices", "batch_size", "dimensions",
];

type Diagnostics = {
  model_count: number;
  usable_model_ids: string[];
  ready: boolean;
  providers: {
    provider_type: string;
    default_env_var: string;
    key_present: boolean;
    models_ready: number;
    model_count: number;
  }[];
  hint: string;
};

type ModelRow = ModelConfig;
type AgentUsage = Record<string, number>;

function typeMeta(t?: string | null) {
  return TYPE_META[t || "other"] || TYPE_META.other;
}

function TypeBadge({ type, multimodal }: { type?: string | null; multimodal?: boolean | null }) {
  const meta = typeMeta(type);
  return (
    <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-md shrink-0"
      style={{ background: meta.bg, color: meta.color }}>
      <span aria-hidden>{meta.icon}</span>{meta.label}
    </span>
  );
}

function MultiBadge({ on }: { on?: boolean | null }) {
  if (!on) return null;
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded-md shrink-0"
      style={{ background: "rgba(139,92,246,0.16)", color: "#c4b5fd" }}
      title="支持文本以外的输入/输出（如图片、音频）">
      🌐 多模态
    </span>
  );
}

function SourceBadge({ source }: { source?: string | null }) {
  if (source === "manual") {
    return (
      <span className="text-[10px] px-1.5 py-0.5 rounded-md shrink-0"
        style={{ background: "rgba(245,158,11,0.16)", color: "#fcd34d" }}
        title="你手工改过配置；再次导入不会覆盖，除非选择覆盖">
        手动配置
      </span>
    );
  }
  if (source === "auto" || source === "default") {
    return (
      <span className="text-[10px] px-1.5 py-0.5 rounded-md shrink-0"
        style={{ background: "rgba(16,185,129,0.14)", color: "#6ee7b7" }}
        title="按模型类型自动生成的默认配置">
        系统默认
      </span>
    );
  }
  return null;
}

function configSummary(m: ModelRow): string {
  const cfg = m.config || {};
  const parts: string[] = [];
  const add = (label: string, key: string) => {
    const v = cfg[key];
    if (v !== undefined && v !== null && v !== "") parts.push(`${label} ${String(v)}`);
  };
  if (m.model_type === "image") {
    add("尺寸", "default_size"); add("质量", "quality");
  } else if (m.model_type === "video") {
    add("时长", "default_duration"); add("分辨率", "resolutions");
  } else if (m.model_type === "audio") {
    add("格式", "default_format");
  } else if (m.model_type === "embedding") {
    add("批大小", "batch_size"); add("维度", "dimensions");
  } else {
    add("温度", "temperature"); add("max_tokens", "max_tokens");
    add("上下文", "context_window");
  }
  return parts.join(" · ");
}

function splitList(value: string): (string | number)[] {
  const parts = value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
  return parts.map((p) => (/^-?\d+(\.\d+)?$/.test(p) ? Number(p) : p));
}

/* ---------------- 配置项输入：按值的真实类型给控件 ---------------- */

function ConfigValueInput({ value, onChange }: {
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (typeof value === "boolean") {
    return (
      <label className="flex items-center gap-2 text-xs cursor-pointer">
        <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)}
          className="accent-indigo-500 w-3.5 h-3.5" />
        <span>{value ? "开启" : "关闭"}</span>
      </label>
    );
  }
  if (typeof value === "number") {
    return (
      <input type="number" value={value} step="any"
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        className="input !py-1.5 text-xs" />
    );
  }
  if (Array.isArray(value)) {
    return (
      <input value={value.join(", ")}
        onChange={(e) => onChange(splitList(e.target.value))}
        className="input !py-1.5 text-xs" />
    );
  }
  if (value && typeof value === "object") {
    return (
      <textarea defaultValue={JSON.stringify(value, null, 2)}
        onBlur={(e) => {
          try { onChange(JSON.parse(e.target.value)); }
          catch { notify("warning", "这段 JSON 格式不正确，未保存该项修改"); }
        }}
        rows={3} className="input !py-1.5 text-xs font-mono" />
    );
  }
  return (
    <input value={value == null ? "" : String(value)}
      placeholder={value === null ? "未设置（可留空）" : ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className="input !py-1.5 text-xs" />
  );
}

export default function ModelsPage() {
  const cid = useActiveCompanyId();

  const [msg, setMsg] = useState("");
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);

  // 系统默认密钥：没有供应商、或旧版直连模型仍指向默认变量时，用它补上最后一步
  const [fallbackKeyEnv, setFallbackKeyEnv] = useState("OPENAI_COMPAT_API_KEY");
  const [fallbackKeyValue, setFallbackKeyValue] = useState("");
  const [fallbackSaving, setFallbackSaving] = useState(false);

  /* ---- 供应商列表 + 模型列表 + 员工绑定关系 ---- */
  const {
    data: catalog, error: loadError, loading: loadingList, refresh: refreshCatalog,
  } = useResource(
    cid ? `models:${cid}` : null,
    async () => {
      const [list, agents, providers] = await Promise.all([
        api.get<ModelConfig[]>("/models"),
        api.get<Agent[]>("/agents"),
        api.get<ModelProvider[]>("/model-providers"),
      ]);
      const bindings = await Promise.all(
        agents.map((a) =>
          api.get<{ model_id: string }[]>(`/agents/${a.id}/models`).catch(() => [])),
      );
      const usage: AgentUsage = {};
      for (const binds of bindings) {
        for (const b of binds) usage[b.model_id] = (usage[b.model_id] || 0) + 1;
      }
      return { list, agents, providers, usage };
    },
  );

  const models = useMemo(() => catalog?.list ?? [], [catalog]);
  const providers = useMemo(() => catalog?.providers ?? [], [catalog]);
  const usage = catalog?.usage ?? {};

  const { data: diag } = useResource<Diagnostics>(
    cid ? `models-diag:${cid}` : null,
    () => api.get<Diagnostics>("/models/diagnostics"),
  );

  const { data: convertCfg } = useResource<ConvertConfig>(
    cid ? `convert-cfg:${cid}` : null,
    () => api.get<ConvertConfig>("/models/convert-config"),
  );

  const [convertModel, setConvertModel] = useState("");
  useEffect(() => {
    if (convertCfg) setConvertModel(convertCfg.convert_model_id || "");
  }, [convertCfg]);

  // 默认选中第一个供应商；供应商被删后自动回落到第一个
  useEffect(() => {
    if (!providers.length) {
      if (selectedProviderId) setSelectedProviderId(null);
      return;
    }
    if (!selectedProviderId || !providers.some((p) => p.id === selectedProviderId)) {
      setSelectedProviderId(providers[0].id);
    }
  }, [providers, selectedProviderId]);

  const provider = providers.find((p) => p.id === selectedProviderId) || null;
  const providerModels = useMemo(
    () => provider ? models.filter((m) => m.provider_id === provider.id) : [],
    [models, provider],
  );
  const legacyModels = useMemo(
    () => models.filter((m) => !m.provider_id),
    [models],
  );

  // 切换供应商时清掉上一次的拉取结果与密钥输入，避免误以为结果属于新供应商
  useEffect(() => {
    setDiscovered(null);
    setPicked(new Set());
    setProviderKey("");
    setPullError("");
    setConfigEdit(null);
  }, [selectedProviderId]);

  async function reload() {
    invalidate("models:");
    invalidate("models-diag:");
    await refreshCatalog();
  }

  /* ================= 供应商：新增 / 编辑 / 删除 ================= */

  const [showAdd, setShowAdd] = useState(false);
  const [addName, setAddName] = useState("");
  const [addType, setAddType] = useState("openai_compat");
  const [addUrl, setAddUrl] = useState("");
  const [addKey, setAddKey] = useState("");
  const [adding, setAdding] = useState(false);

  async function createProvider() {
    const myCid = getActiveCompanyId();
    if (!myCid) { notify("warning", "请先选择公司"); return; }
    if (!addName.trim()) { notify("warning", "请填写供应商名称"); return; }
    if (addType === "openai_compat" && !addUrl.trim()) {
      notify("warning", "OpenAI 兼容供应商必须填写 Base URL"); return;
    }
    if (addKey.trim() && addKey.trim().length < 8) {
      notify("warning", "API Key 太短，请检查是否复制完整"); return;
    }
    setAdding(true);
    try {
      const created = await api.post<ModelProvider>("/model-providers", {
        name: addName.trim(),
        provider_type: addType,
        base_url: addUrl.trim() || null,
        company_id: myCid,
      });
      if (addKey.trim()) {
        try {
          await api.post(`/model-providers/${created.id}/key`,
            { api_key: addKey.trim() });
        } catch (e) {
          notify("warning",
            `供应商已添加，但密钥未能保存：${describeError(e, "保存密钥失败")}。`
            + "密钥只能由系统管理员写入。");
        }
      }
      notify("success", `已添加供应商「${created.name}」`);
      setAddName(""); setAddUrl(""); setAddKey(""); setAddType("openai_compat");
      setShowAdd(false);
      setSelectedProviderId(created.id);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "添加供应商失败"));
    }
    setAdding(false);
  }

  const [editingProvider, setEditingProvider] = useState(false);
  const [editName, setEditName] = useState("");
  const [editType, setEditType] = useState("openai_compat");
  const [editUrl, setEditUrl] = useState("");
  const [savingProvider, setSavingProvider] = useState(false);

  function startEditProvider(p: ModelProvider) {
    setEditName(p.name); setEditType(p.provider_type); setEditUrl(p.base_url || "");
    setEditingProvider(true);
  }

  async function saveProvider() {
    if (!provider) return;
    if (!editName.trim()) { notify("warning", "供应商名称不能为空"); return; }
    if (editType === "openai_compat" && !editUrl.trim()) {
      notify("warning", "OpenAI 兼容供应商必须填写 Base URL"); return;
    }
    setSavingProvider(true);
    try {
      await api.patch(`/model-providers/${provider.id}`, {
        name: editName.trim(), provider_type: editType,
        base_url: editUrl.trim() || null,
      });
      notify("success", "供应商已更新；其下模型的地址与密钥变量已同步");
      setEditingProvider(false);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "保存供应商失败"));
    }
    setSavingProvider(false);
  }

  async function claimProvider(p: ModelProvider) {
    const myCid = getActiveCompanyId();
    if (!myCid) { notify("warning", "请先选择公司"); return; }
    try {
      await api.patch(`/model-providers/${p.id}`, { company_id: myCid });
      notify("success", `已把供应商「${p.name}」归入当前公司`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "认领失败"));
    }
  }

  async function deleteProvider(p: ModelProvider) {    if (p.model_count > 0) {
      notify("warning", `「${p.name}」下还有 ${p.model_count} 个模型，请先删除或转移模型`);
      return;
    }
    if (!(await confirmAction({
      title: `删除供应商「${p.name}」？`,
      description: "仅删除供应商连接配置；本机 .env 里的密钥不会被删除。此操作不可撤销。",
      confirmText: "删除",
    }))) return;
    try {
      await api.del(`/model-providers/${p.id}`);
      notify("success", `已删除供应商「${p.name}」`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "删除供应商失败"));
    }
  }

  /* ================= 密钥 / 测试 / 拉取 / 导入 ================= */

  const [providerKey, setProviderKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [testingProvider, setTestingProvider] = useState(false);

  async function saveProviderKey() {
    if (!provider) return;
    const key = providerKey.trim();
    if (key.length < 8) { notify("warning", "请粘贴完整的 API Key"); return; }
    setSavingKey(true);
    try {
      const r = await api.post<{ env_name: string; masked: string }>(
        `/model-providers/${provider.id}/key`, { api_key: key });
      notify("success", `密钥已写入本机 .env（${r.masked}），已立即生效`);
      setProviderKey("");
      await reload();
    } catch (e) {
      notify("error", describeError(e, "保存密钥失败"));
    }
    setSavingKey(false);
  }

  async function testProvider() {
    if (!provider) return;
    setTestingProvider(true);
    try {
      const r = await api.post<ProviderDiscoverResult>(
        `/model-providers/${provider.id}/test`);
      if (r.status === "ok") {
        notify("success", `连接正常，上游返回 ${r.model_count} 个模型`);
      } else {
        notify("error", r.error || "连接失败");
      }
      await reload();
    } catch (e) {
      notify("error", describeError(e, "连接测试失败"));
    }
    setTestingProvider(false);
  }

  const [discovered, setDiscovered] = useState<DiscoveredModel[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [pulling, setPulling] = useState(false);
  const [pullError, setPullError] = useState("");
  const [importing, setImporting] = useState(false);

  async function pullModels() {
    if (!provider) return;
    setPulling(true);
    setPullError("");
    setDiscovered(null);
    try {
      const body = providerKey.trim() ? { api_key: providerKey.trim() } : {};
      const r = await api.post<ProviderDiscoverResult>(
        `/model-providers/${provider.id}/pull`, body);
      if (r.status === "ok") {
        setDiscovered(r.items);
        const existing = new Set(providerModels.map((m) => m.model_id));
        // 默认勾选尚未导入的模型：用户点「拉取」通常就是想启用它们
        setPicked(new Set(r.items.filter((m) => !existing.has(m.id)).map((m) => m.id)));
        if (!r.items.length) setPullError("上游没有返回任何模型");
      } else {
        setPullError(r.error || "拉取失败");
      }
    } catch (e) {
      setPullError(describeError(e, "拉取模型失败"));
    }
    setPulling(false);
  }

  function togglePicked(id: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function pickAll(items: DiscoveredModel[], on: boolean) {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const m of items) { if (on) next.add(m.id); else next.delete(m.id); }
      return next;
    });
  }

  async function importPicked() {
    if (!provider || !discovered) return;
    const chosen = discovered.filter((m) => picked.has(m.id));
    if (!chosen.length) { notify("warning", "请至少勾选一个模型"); return; }
    setImporting(true);
    try {
      const r = await api.post<ProviderImportResult>(
        `/model-providers/${provider.id}/import`, {
          models: chosen.map((m) => ({
            model_id: m.id,
            name: m.name,
            model_type: m.model_type,
            multimodal: m.multimodal,
            capabilities: m.capabilities,
            // 只回传上游声明的长度；配置由后端按类型生成，
            // 这样 config_source 仍是 auto（之后手改才会变 manual）
            upstream: {
              context_window: typeof m.config.context_window === "number"
                ? m.config.context_window : undefined,
              max_output_tokens: typeof m.config.max_output_tokens === "number"
                ? m.config.max_output_tokens : undefined,
            },
          })),
        });
      notify("success",
        `已导入 ${r.imported_count} 个模型`
        + (r.updated.length ? `，更新 ${r.updated.length} 个` : "")
        + (r.adopted.length ? `，收编旧配置 ${r.adopted.length} 个` : ""));
      setDiscovered(null);
      setPicked(new Set());
      await reload();
    } catch (e) {
      notify("error", describeError(e, "导入模型失败"));
    }
    setImporting(false);
  }

  /* ================= 模型：测试 / 启停 / 绑定 / 删除 ================= */

  const [testingModel, setTestingModel] = useState<string | null>(null);

  async function testModel(m: ModelRow) {
    setTestingModel(m.id);
    try {
      const d = await api.post<ModelTestResult>(`/models/${m.id}/test`);
      if (d.status === "ok") notify("success", `连通正常，模型回复：${d.reply}`);
      else notify("error", (d.error || "调用失败").slice(0, 160));
    } catch (e) {
      notify("error", describeError(e, "连通测试失败"));
    }
    setTestingModel(null);
  }

  async function toggleEnabled(m: ModelRow) {
    try {
      await api.patch(`/models/${m.id}`, { enabled: m.enabled === false });
      notify("success", m.enabled === false ? `已启用「${m.name}」` : `已停用「${m.name}」`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "切换启用状态失败"));
    }
  }

  async function bindAll(m: ModelRow) {
    const myCid = getActiveCompanyId();
    if (!myCid) { notify("warning", "请先选择公司"); return; }
    try {
      const r = await api.post<{ bound: number; skipped: number; agent_count: number }>(
        `/models/${m.id}/bind-agents`, { company_id: myCid });
      notify("success", r.bound
        ? `已绑定 ${r.bound} 名员工${r.skipped ? `，${r.skipped} 名已有首选模型未改动` : ""}`
        : `全部 ${r.agent_count} 名员工都已有首选模型，未改动`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "绑定失败"));
    }
  }

  async function deleteModel(m: ModelRow) {
    if (!(await confirmAction({
      title: `删除模型「${m.name}」？`,
      description: "绑定过它的员工会自动回到「未绑定」状态（历史记录不受影响），新任务需要重新接入模型。",
      confirmText: "删除",
    }))) return;
    try {
      await api.del(`/models/${m.id}`);
      notify("success", `已删除「${m.name}」`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "删除失败"));
    }
  }

  async function claimModel(m: ModelRow) {
    const myCid = getActiveCompanyId();
    if (!myCid) { notify("warning", "请先选择公司"); return; }
    try {
      await api.patch(`/models/${m.id}`, { company_id: myCid });
      notify("success", `已把「${m.name}」归入当前公司，现在可以改删了`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "认领失败"));
    }
  }

  async function moveToProvider(m: ModelRow) {
    if (!provider) return;
    try {
      if (!m.company_id) {
        const myCid = getActiveCompanyId();
        if (!myCid) { notify("warning", "请先选择公司"); return; }
        await api.patch(`/models/${m.id}`, { company_id: myCid });
      }
      await api.patch(`/models/${m.id}`, { provider_id: provider.id });
      notify("success", `已把「${m.name}」归入「${provider.name}」`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "转移失败"));
    }
  }

  /* ================= 模型配置编辑 ================= */

  type ConfigEdit = {
    modelId: string;
    type: string;
    multimodal: boolean;
    enabled: boolean;
    config: Record<string, unknown>;
  };
  const [configEdit, setConfigEdit] = useState<ConfigEdit | null>(null);
  const [savingConfig, setSavingConfig] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  function openConfig(m: ModelRow) {
    if (configEdit?.modelId === m.id) { setConfigEdit(null); return; }
    setConfigEdit({
      modelId: m.id,
      type: (m.model_type as string) || "text",
      multimodal: !!m.multimodal,
      enabled: m.enabled !== false,
      config: { ...(m.config || {}) },
    });
    setNewKey(""); setNewValue("");
  }

  function setCfg(key: string, value: unknown) {
    setConfigEdit((d) => d ? { ...d, config: { ...d.config, [key]: value } } : d);
  }

  function removeCfg(key: string) {
    setConfigEdit((d) => {
      if (!d) return d;
      const next = { ...d.config };
      delete next[key];
      return { ...d, config: next };
    });
  }

  function addCfg() {
    const key = newKey.trim();
    if (!key) return;
    setCfg(key, newValue);
    setNewKey(""); setNewValue("");
  }

  async function saveConfig(m: ModelRow) {
    if (!configEdit) return;
    setSavingConfig(true);
    try {
      await api.patch(`/models/${m.id}`, {
        model_type: configEdit.type,
        multimodal: configEdit.multimodal,
        enabled: configEdit.enabled,
        config: configEdit.config,
      });
      notify("success", `「${m.name}」配置已保存（标记为手动配置）`);
      setConfigEdit(null);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "保存配置失败"));
    }
    setSavingConfig(false);
  }

  async function resetConfig(m: ModelRow) {
    if (!configEdit) return;
    setSavingConfig(true);
    try {
      await api.patch(`/models/${m.id}`, {
        model_type: configEdit.type,
        reset_config: true,
      });
      notify("success", `已按「${typeMeta(configEdit.type).label}」重新生成默认配置`);
      setConfigEdit(null);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "重置配置失败"));
    }
    setSavingConfig(false);
  }

  const editConfigKeys = useMemo(() => {
    if (!configEdit) return [];
    const keys = Object.keys(configEdit.config);
    return [
      ...CONFIG_ORDER.filter((k) => keys.includes(k)),
      ...keys.filter((k) => !CONFIG_ORDER.includes(k)),
    ];
  }, [configEdit]);

  /* ================= 就绪状态 ================= */

  const readiness = useMemo(() => {
    if (!diag) return null;
    if (diag.ready) {
      return { tone: "ok" as const, text: `已就绪 · ${diag.usable_model_ids.length} 个模型可调用` };
    }
    if (!diag.model_count) return { tone: "warn" as const, text: "还没有可调用的模型：先添加供应商并拉取模型" };
    return { tone: "warn" as const, text: diag.hint || "模型已登记但缺少密钥" };
  }, [diag]);

  // 只有文本模型能当员工的对话模型；图片/视频/向量模型走别的接口，
  // 绑上去每轮都是上游 400（后端 bind / switch 同规则会拒）。
  const chatCapable = (m: ModelRow) => !m.model_type || m.model_type === "text";
  const textModels = models.filter(
    (m) => m.enabled !== false && chatCapable(m));

  async function saveFallbackKey() {
    const key = fallbackKeyValue.trim();
    if (key.length < 8) { notify("warning", "请粘贴完整的 API Key"); return; }
    setFallbackSaving(true);
    try {
      const r = await api.post<{ env_name: string; masked: string }>(
        "/models/env-key", { env_name: fallbackKeyEnv, api_key: key });
      notify("success", `密钥已写入本机 .env（${r.masked}），旧版直连模型也会立即使用它`);
      setFallbackKeyValue("");
      await reload();
    } catch (e) {
      notify("error", describeError(e, "保存密钥失败"));
    }
    setFallbackSaving(false);
  }

  /* ---------------- 渲染模型行 ---------------- */

  function renderModelRow(m: ModelRow, legacy = false) {
    const isEditing = configEdit?.modelId === m.id;
    const usable = diag?.usable_model_ids?.includes(m.id);
    const disabled = m.enabled === false;
    return (
      <div key={m.id} className="rounded-xl overflow-hidden"
        style={{ background: "var(--surface-2)", opacity: disabled ? 0.55 : 1 }}>
        <div className="flex items-center gap-2.5 px-3 py-2.5 flex-wrap sm:flex-nowrap">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${usable ? "bg-emerald-400" : "bg-amber-400"}`}
            title={usable ? "密钥已就位，可直接调用" : "尚未取到密钥，调用会失败"} />
          <div className="min-w-0 flex-1">
            <div className="text-sm flex items-center gap-1.5 flex-wrap">
              <span className="truncate">{m.name}</span>
              <TypeBadge type={m.model_type} multimodal={m.multimodal} />
              <MultiBadge on={m.multimodal} />
              <SourceBadge source={m.config_source} />
              {m.unowned && (
                <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0"
                  style={{ background: "rgba(245,158,11,0.18)", color: "#fcd34d" }}
                  title="升级前登记的遗留配置，未归属任何公司：可认领后再管理">
                  未归属
                </span>
              )}
            </div>
            <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>
              <span className="font-mono">{m.model_id}</span>
              {legacy && m.base_url ? ` · ${m.base_url}` : ""}
              {` · ${usage[m.id] || 0} 名员工使用`}
            </div>
            {configSummary(m) && (
              <div className="text-[11px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                {configSummary(m)}
              </div>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0 flex-wrap">
            {m.unowned ? (
              <>
                <button onClick={() => claimModel(m)} className="btn-primary !py-1 !px-2 text-xs">认领</button>
                {provider && (
                  <button onClick={() => moveToProvider(m)} className="btn-ghost !py-1 !px-2 text-xs"
                    title={`认领并归入「${provider.name}」`}>
                    归入当前供应商
                  </button>
                )}
              </>
            ) : (
              <>
                {legacy && provider && (
                  <button onClick={() => moveToProvider(m)} className="btn-ghost !py-1 !px-2 text-xs"
                    title={`把这条直连模型挂到「${provider.name}」下`}>
                    归入供应商
                  </button>
                )}
                <button onClick={() => openConfig(m)} className="btn-ghost !py-1 !px-2 text-xs">
                  {isEditing ? "收起配置" : "配置"}
                </button>
                <button onClick={() => testModel(m)} disabled={testingModel === m.id}
                  className="btn-ghost !py-1 !px-2 text-xs">
                  {testingModel === m.id ? "测试中…" : "测试"}
                </button>
                {chatCapable(m) && (
                  <button onClick={() => bindAll(m)} className="btn-ghost !py-1 !px-2 text-xs"
                    title="设为公司全部员工的首选模型；已有首选模型的员工不受影响">
                    绑定全员
                  </button>
                )}
                <button onClick={() => toggleEnabled(m)} className="btn-ghost !py-1 !px-2 text-xs">
                  {disabled ? "启用" : "停用"}
                </button>
                <button onClick={() => deleteModel(m)}
                  className="text-xs py-1 px-2 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10">
                  删除
                </button>
              </>
            )}
          </div>
        </div>

        {isEditing && configEdit && (
          <div className="px-3 pb-3 pt-2 space-y-3" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
              <Field label="模型类型" hint="决定调用方式与默认配置">
                <select value={configEdit.type}
                  onChange={(e) => setConfigEdit({ ...configEdit, type: e.target.value })}
                  className="input !py-1.5 text-xs">
                  {TYPE_ORDER.map((t) => (
                    <option key={t} value={t}>{typeMeta(t).icon} {typeMeta(t).label}</option>
                  ))}
                </select>
              </Field>
              <Field label="启用状态">
                <label className="flex items-center gap-2 text-xs h-[30px] cursor-pointer">
                  <input type="checkbox" checked={configEdit.enabled}
                    onChange={(e) => setConfigEdit({ ...configEdit, enabled: e.target.checked })}
                    className="accent-indigo-500 w-3.5 h-3.5" />
                  <span>{configEdit.enabled ? "启用" : "停用（员工不会再用它）"}</span>
                </label>
              </Field>
              <Field label="多模态" hint="支持图片/音频等非文本输入输出">
                <label className="flex items-center gap-2 text-xs h-[30px] cursor-pointer">
                  <input type="checkbox" checked={configEdit.multimodal}
                    onChange={(e) => setConfigEdit({ ...configEdit, multimodal: e.target.checked })}
                    className="accent-indigo-500 w-3.5 h-3.5" />
                  <span>{configEdit.multimodal ? "是" : "否"}</span>
                </label>
              </Field>
              <div className="flex items-end">
                <button onClick={() => resetConfig(m)} disabled={savingConfig}
                  className="btn-ghost !py-1.5 text-xs w-full"
                  title="按当前类型重新生成系统默认配置（会覆盖手动值）">
                  按类型重置配置
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
              {editConfigKeys.map((key) => (
                <div key={key} className="relative group">
                  <Field label={CONFIG_LABELS[key] || key}
                    hint={CONFIG_HINTS[key]}>
                    <div className="flex items-center gap-1">
                      <ConfigValueInput value={configEdit.config[key]}
                        onChange={(v) => setCfg(key, v)} />
                      <button onClick={() => removeCfg(key)}
                        className="text-[11px] text-red-400/70 hover:text-red-300 shrink-0 px-1"
                        title="移除这一项">
                        ✕
                      </button>
                    </div>
                  </Field>
                </div>
              ))}
            </div>

            <div className="flex flex-col sm:flex-row gap-2 items-end">
              <Field label="新增配置项" hint="上游没有给出、但你需要显式指定的参数">
                <div className="flex gap-1.5">
                  <input value={newKey} onChange={(e) => setNewKey(e.target.value)}
                    placeholder="参数名，如 seed" className="input !py-1.5 text-xs w-36" />
                  <input value={newValue} onChange={(e) => setNewValue(e.target.value)}
                    placeholder="值（数字会自动识别）" className="input !py-1.5 text-xs flex-1" />
                </div>
              </Field>
              <button onClick={addCfg} disabled={!newKey.trim()}
                className="btn-ghost !py-1.5 text-xs shrink-0 disabled:opacity-40">
                添加
              </button>
              <div className="flex-1" />
              <button onClick={() => saveConfig(m)} disabled={savingConfig}
                className="btn-primary !py-1.5 !px-4 text-xs shrink-0 disabled:opacity-40">
                {savingConfig ? "保存中…" : "保存配置"}
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  /* ---------------- 渲染 ---------------- */

  return (
    <div className="p-6 max-w-[1500px] mx-auto animate-fade-up space-y-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">模型配置中心</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            添加供应商 → 拉取模型 → 勾选类型与配置；员工绑定后即可开工
          </p>
        </div>
        <button onClick={() => setShowAdd((v) => !v)} className="btn-primary">
          {showAdd ? "收起" : "＋ 添加供应商"}
        </button>
      </div>

      {readiness && (
        <div className="flex items-start gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={readiness.tone === "ok"
            ? { background: "rgba(16,185,129,0.08)", border: "1px solid rgba(16,185,129,0.3)", color: "#6ee7b7" }
            : { background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.3)", color: "#fcd34d" }}>
          <span>{readiness.tone === "ok" ? "✅" : "⚠️"}</span>
          <span className="flex-1">{readiness.text}</span>
        </div>
      )}

      {/* 旧版直连模型没有供应商可点，仍需要一条"补密钥"的路径 */}
      {diag && !diag.ready && (
        <div className="card p-4 space-y-2">
          <h3 className="font-semibold text-sm">🔐 填写 API Key（保存到本机，立即生效）</h3>
          <p className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
            用于尚未归入供应商的旧版直连模型。密钥只写入本机 <code>apps/api/.env</code>
            （权限 600），<b>不上传、不入库、不回显</b>；仅系统管理员可操作。
          </p>
          <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
            <select value={fallbackKeyEnv}
              onChange={(e) => setFallbackKeyEnv(e.target.value)}
              className="input sm:w-72 !py-2 text-xs" aria-label="密钥变量名">
              {(diag.providers.length ? diag.providers : [
                { provider_type: "openai_compat", default_env_var: "OPENAI_COMPAT_API_KEY", key_present: false },
                { provider_type: "anthropic", default_env_var: "ANTHROPIC_API_KEY", key_present: false },
                { provider_type: "gemini", default_env_var: "GEMINI_API_KEY", key_present: false },
              ]).map((p) => (
                <option key={p.default_env_var} value={p.default_env_var}>
                  {PROVIDER_LABELS[p.provider_type] || p.provider_type} · {p.default_env_var}
                  {p.key_present ? "（已配置）" : ""}
                </option>
              ))}
            </select>
            <input type="password" value={fallbackKeyValue}
              onChange={(e) => setFallbackKeyValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void saveFallbackKey()}
              placeholder={diag.providers.find((p) => p.default_env_var === fallbackKeyEnv)?.key_present
                ? "已配置；如需更换请粘贴新 Key" : "粘贴 API Key"}
              autoComplete="off" spellCheck={false}
              className="input flex-1 !py-2 text-xs" aria-label="API Key" />
            <button onClick={saveFallbackKey}
              disabled={fallbackSaving || !fallbackKeyValue.trim()}
              className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40">
              {fallbackSaving ? "保存中…" : "保存并生效"}
            </button>
          </div>
        </div>
      )}

      {loadError && (
        <div className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
          <span>🔌</span>
          <span className="flex-1">加载模型配置失败：{loadError}</span>
          <button onClick={() => refreshCatalog()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

      {msg && <p className="text-sm text-indigo-300">{msg}</p>}

      {/* 新增供应商 */}
      {showAdd && (
        <div className="card p-4 space-y-3">
          <h2 className="text-sm font-semibold">添加模型供应商</h2>
          <p className="text-[11px]" style={{ color: "var(--muted)" }}>
            可以无限添加。每个供应商独立保存地址与密钥；密钥只写入本机 .env，
            <b>不上传、不入库、不回显</b>。
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Field label="供应商名称" required hint="用于区分不同来源，可重复添加同类供应商">
              <input value={addName} onChange={(e) => setAddName(e.target.value)}
                placeholder="如：官方 OpenAI / 自建中转 / 本地 Ollama"
                className="input" autoFocus />
            </Field>
            <Field label="供应商类型" hint={PROVIDER_HINTS[addType]}>
              <select value={addType} onChange={(e) => setAddType(e.target.value)} className="input">
                {Object.entries(PROVIDER_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </Field>
            <Field label="Base URL" required={addType === "openai_compat"}
              hint="填写到 /v1 这一级；拉取时会自动请求 /v1/models">
              <input value={addUrl} onChange={(e) => setAddUrl(e.target.value)}
                placeholder={addType === "openai_compat"
                  ? "https://api.example.com/v1"
                  : "留空使用官方地址"}
                className="input" />
            </Field>
            <Field label="API Key（可选）" hint="仅系统管理员可写入；也可稍后在供应商详情里填写">
              <input type="password" value={addKey} onChange={(e) => setAddKey(e.target.value)}
                placeholder="粘贴密钥，留空则稍后填写" autoComplete="off" spellCheck={false}
                className="input" />
            </Field>
          </div>
          <div className="flex gap-2">
            <button onClick={createProvider} disabled={adding}
              className="btn-primary disabled:opacity-40">
              {adding ? "添加中…" : "添加供应商"}
            </button>
            <button onClick={() => setShowAdd(false)} className="btn-ghost">取消</button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[290px_minmax(0,1fr)] gap-4 items-start">
        {/* 左：供应商列表 */}
        <div className="card card-static p-2 space-y-1.5">
          <div className="px-2 py-1 text-xs font-semibold" style={{ color: "var(--muted)" }}>
            供应商（{providers.length}）
          </div>
          {providers.map((p) => {
            const active = p.id === selectedProviderId;
            return (
              <button key={p.id} onClick={() => setSelectedProviderId(p.id)}
                className="w-full text-left px-3 py-2.5 rounded-xl transition-colors"
                style={{
                  background: active ? "rgba(99,102,241,0.14)" : "var(--surface-2)",
                  border: active ? "1px solid rgba(99,102,241,0.45)" : "1px solid transparent",
                }}>
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${p.api_key_present ? "bg-emerald-400" : "bg-amber-400"}`}
                    title={p.api_key_present ? "密钥已配置" : "尚未配置密钥"} />
                  <span className="text-sm font-medium truncate flex-1">{p.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0"
                    style={{ background: "rgba(255,255,255,0.06)", color: "var(--muted)" }}>
                    {p.model_count}
                  </span>
                </div>
                <div className="text-[11px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                  {PROVIDER_LABELS[p.provider_type] || p.provider_type}
                  {p.base_url ? ` · ${p.base_url}` : ""}
                </div>
                <div className="text-[10px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                  {p.last_check_ok === true
                    ? `上次检查：正常${p.last_check_at ? ` · ${p.last_check_at.slice(0, 16).replace("T", " ")}` : ""}`
                    : p.last_check_ok === false
                      ? `上次检查：失败${p.last_check_error ? ` · ${p.last_check_error}` : ""}`
                      : "尚未测试连接"}
                </div>
              </button>
            );
          })}
          {!providers.length && !loadingList && (
            <p className="text-xs text-center py-6 px-3" style={{ color: "var(--muted)" }}>
              还没有供应商。点击右上角「添加供应商」，填好地址和密钥后即可拉取模型。
            </p>
          )}
          {loadingList && !providers.length && (
            <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>加载中…</p>
          )}

          {legacyModels.length > 0 && (
            <div className="mt-2 pt-2 px-2" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                另有 {legacyModels.length} 个升级前的直连模型，未归入供应商
              </p>
            </div>
          )}
        </div>

        {/* 右：供应商详情 */}
        <div className="space-y-4 min-w-0">
          {!provider ? (
            <div className="card p-8 text-center space-y-2">
              <p className="text-sm">先添加一个模型供应商</p>
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                供应商 = 地址 + 密钥；同一家供应商可以反复添加，互不覆盖
              </p>
              <button onClick={() => setShowAdd(true)} className="btn-primary mt-2">
                ＋ 添加供应商
              </button>
            </div>
          ) : (
            <>
              {/* 供应商信息与操作 */}
              <div className="card p-4 space-y-3">
                {!editingProvider ? (
                  <>
                    <div className="flex items-start justify-between gap-3 flex-wrap">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h2 className="text-base font-semibold truncate">{provider.name}</h2>
                          <span className="text-[10px] px-1.5 py-0.5 rounded-md"
                            style={{ background: "rgba(99,102,241,0.15)", color: "#a5b4fc" }}>
                            {PROVIDER_LABELS[provider.provider_type] || provider.provider_type}
                          </span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded-md ${provider.api_key_present
                            ? "bg-emerald-400/15 text-emerald-300" : "bg-amber-400/15 text-amber-300"}`}>
                            {provider.api_key_present ? "🔐 密钥已就位" : "⚠️ 缺密钥"}
                          </span>
                          {provider.unowned && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-amber-400/15 text-amber-300">
                              未归属
                            </span>
                          )}
                        </div>
                        <p className="text-xs mt-1 font-mono break-all" style={{ color: "var(--muted)" }}>
                          {provider.base_url || "（使用官方默认地址）"}
                        </p>
                        <p className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                          密钥变量名 <code>{provider.api_key_env || "—"}</code>
                          {" · "}模型 {provider.model_count} 个（启用 {provider.enabled_model_count}）
                        </p>
                      </div>
                      <div className="flex gap-1.5 flex-wrap">
                        <button onClick={testProvider} disabled={testingProvider}
                          className="btn-ghost !py-1.5 !px-3 text-xs">
                          {testingProvider ? "测试中…" : "测试连接"}
                        </button>
                        {provider.unowned ? (
                          <button onClick={() => claimProvider(provider)}
                            className="btn-primary !py-1.5 !px-3 text-xs">
                            认领供应商
                          </button>
                        ) : (
                          <>
                            <button onClick={() => startEditProvider(provider)}
                              className="btn-ghost !py-1.5 !px-3 text-xs">编辑</button>
                            <button onClick={() => deleteProvider(provider)}
                              className="text-xs py-1.5 px-3 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10">
                              删除
                            </button>
                          </>
                        )}
                      </div>
                    </div>

                    <div className="rounded-xl p-3 space-y-2" style={{ background: "var(--surface-2)" }}>
                      <div className="text-xs font-medium">🔐 连接密钥</div>
                      <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                        只写入本机 <code>apps/api/.env</code>（权限 600），保存后立即生效，无需重启。
                        拉取时若尚未保存，也会临时使用下面填写的 Key（不落盘）。
                      </p>
                      <div className="flex flex-col sm:flex-row gap-2">
                        <input type="password" value={providerKey}
                          onChange={(e) => setProviderKey(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && void saveProviderKey()}
                          placeholder={provider.api_key_present ? "如需更换，粘贴新 Key" : "粘贴 API Key"}
                          autoComplete="off" spellCheck={false}
                          className="input flex-1 !py-2 text-xs" />
                        <button onClick={saveProviderKey} disabled={savingKey || !providerKey.trim()}
                          className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40">
                          {savingKey ? "保存中…" : "保存密钥"}
                        </button>
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    <h2 className="text-sm font-semibold">编辑供应商</h2>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <Field label="名称" required>
                        <input value={editName} onChange={(e) => setEditName(e.target.value)}
                          className="input" />
                      </Field>
                      <Field label="类型" hint={PROVIDER_HINTS[editType]}>
                        <select value={editType} onChange={(e) => setEditType(e.target.value)}
                          className="input">
                          {Object.entries(PROVIDER_LABELS).map(([k, v]) => (
                            <option key={k} value={k}>{v}</option>
                          ))}
                        </select>
                      </Field>
                      <Field label="Base URL" required={editType === "openai_compat"}>
                        <input value={editUrl} onChange={(e) => setEditUrl(e.target.value)}
                          placeholder={editType === "openai_compat" ? "https://api.example.com/v1" : "留空使用官方地址"}
                          className="input" />
                      </Field>
                    </div>
                    <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                      修改地址 / 类型 / 密钥变量后，其下所有模型会自动同步，避免界面与实际请求不一致。
                    </p>
                    <div className="flex gap-2">
                      <button onClick={saveProvider} disabled={savingProvider}
                        className="btn-primary disabled:opacity-40">
                        {savingProvider ? "保存中…" : "保存"}
                      </button>
                      <button onClick={() => setEditingProvider(false)} className="btn-ghost">取消</button>
                    </div>
                  </>
                )}
              </div>

              {/* 拉取模型 */}
              <div className="card p-4 space-y-3">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <h2 className="text-sm font-semibold">🔌 拉取模型</h2>
                  <button onClick={pullModels} disabled={pulling}
                    className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40">
                    {pulling ? "拉取中…" : "拉取模型列表"}
                  </button>
                </div>
                <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                  会按上游协议自动请求模型列表，并识别每个模型的类型（文本 / 图片 / 视频 /
                  音频 / 向量）与是否多模态。识别不到的类型可在导入后手动改正。
                </p>
                {provider.unowned && (
                  <div className="px-3 py-2 rounded-lg text-xs"
                    style={{ background: "rgba(245,158,11,0.1)", color: "#fcd34d" }}>
                    该供应商是升级遗留的无主配置：可以拉取查看，但导入前需要先认领。
                  </div>
                )}

                {pullError && (
                  <div className="px-3 py-2 rounded-lg text-xs"
                    style={{ background: "rgba(248,113,113,0.1)", color: "#fca5a5" }}>
                    {pullError}
                  </div>
                )}

                {discovered && discovered.length > 0 && (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <p className="text-xs" style={{ color: "var(--muted)" }}>
                        上游共 {discovered.length} 个模型 · 已选 {picked.size} 个
                      </p>
                      <div className="flex gap-1.5">
                        <button onClick={() => pickAll(discovered, true)}
                          className="btn-ghost !py-1 !px-2 text-xs">全选</button>
                        <button onClick={() => pickAll(discovered, false)}
                          className="btn-ghost !py-1 !px-2 text-xs">清空</button>
                      </div>
                    </div>

                    {TYPE_ORDER.map((type) => {
                      const items = discovered.filter((m) => (m.model_type || "other") === type);
                      if (!items.length) return null;
                      const meta = typeMeta(type);
                      const existing = new Set(providerModels.map((m) => m.model_id));
                      return (
                        <div key={type} className="rounded-xl p-2.5 space-y-1.5"
                          style={{ background: "var(--surface-2)" }}>
                          <div className="flex items-center justify-between">
                            <span className="text-xs font-medium" style={{ color: meta.color }}>
                              {meta.icon} {meta.label}（{items.length}）
                            </span>
                            <button onClick={() => pickAll(items, true)}
                              className="text-[11px] px-1.5 py-0.5 rounded hover:bg-white/[0.06]"
                              style={{ color: "var(--muted)" }}>
                              选该类
                            </button>
                          </div>
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-1.5 max-h-56 overflow-y-auto">
                            {items.map((m) => (
                              <label key={m.id}
                                className="flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs cursor-pointer hover:bg-white/[0.04]">
                                <input type="checkbox" checked={picked.has(m.id)}
                                  onChange={() => togglePicked(m.id)}
                                  className="accent-indigo-500 w-3.5 h-3.5 shrink-0" />
                                <span className="truncate flex-1 font-mono">{m.id}</span>
                                {existing.has(m.id) && (
                                  <span className="text-[10px] shrink-0" style={{ color: "var(--muted)" }}>
                                    已导入
                                  </span>
                                )}
                                <MultiBadge on={m.multimodal} />
                              </label>
                            ))}
                          </div>
                        </div>
                      );
                    })}

                    <button onClick={importPicked} disabled={importing || !picked.size || provider.unowned}
                      className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40"
                      title={provider.unowned ? "无主供应商不能导入，请先认领" : undefined}>
                      {importing ? "导入中…" : `导入选中的 ${picked.size} 个模型`}
                    </button>
                  </div>
                )}
              </div>

              {/* 已导入模型 */}
              <div className="card card-static p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-semibold">
                    该供应商的模型（{providerModels.length}）
                  </h2>
                  <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                    绿色圆点 = 密钥就位
                  </span>
                </div>
                <div className="space-y-2">
                  {providerModels.map((m) => renderModelRow(m))}
                  {!providerModels.length && (
                    <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>
                      还没有模型。点击上方「拉取模型列表」，勾选后导入。
                    </p>
                  )}
                </div>
              </div>
            </>
          )}

          {/* 旧版直连模型 */}
          {legacyModels.length > 0 && (
            <div className="card card-static p-4 space-y-2">
              <h2 className="text-sm font-semibold">旧版直连模型（{legacyModels.length}）</h2>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                这些是升级前登记的模型，仍可正常调用。可认领归属，或归入上方选中的供应商。
              </p>
              <div className="space-y-2">
                {legacyModels.map((m) => renderModelRow(m, true))}
              </div>
            </div>
          )}

          {/* 文档转换专用模型 */}
          <div className="card p-4 space-y-2">
            <h3 className="font-semibold text-sm">📄 文档转换专用大模型</h3>
            <p className="text-[11px]" style={{ color: "var(--muted)" }}>
              上传的训练资料/附件将用此模型转换为结构化 Markdown 入库。建议选择低成本的文本模型。
            </p>
            <div className="flex gap-2 items-center">
              <select value={convertModel} onChange={(e) => setConvertModel(e.target.value)}
                className="input flex-1 !py-2 text-xs">
                <option value="">— 未设置（仅规则转换，不调用 LLM） —</option>
                {textModels.map((m) => (
                  <option key={m.id} value={m.id}>{m.name} ({m.model_id})</option>
                ))}
              </select>
              <button onClick={async () => {
                try {
                  await api.post("/models/convert-config", { convert_model_id: convertModel });
                  invalidate("convert-cfg:");
                  notify("success", "转换模型已保存");
                } catch (e) { notify("error", describeError(e, "保存失败")); }
              }} className="btn-primary !py-2 !px-4 text-xs shrink-0">保存</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
