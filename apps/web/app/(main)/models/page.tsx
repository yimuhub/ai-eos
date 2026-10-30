"use client";
import { useEffect, useMemo, useState } from "react";
import { api, describeError, getActiveCompanyId } from "@/lib/api";
import { useActiveCompanyId, useResource, invalidate } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import type {
  Agent, BatchImportResult, ConvertConfig, ModelConfig, ModelTestResult,
  ProviderModelsResult,
} from "@/lib/types";
import Field from "@/components/Field";
import { useFormValidation } from "@/lib/useFormValidation";
import { V } from "@/lib/validate";
import { notify } from "@/components/Toast";

/* ============================================================
   模型配置中心：管理大模型供应商 + 拉取模型列表 + 勾选启用
   ============================================================ */

// 复用 lib/types 的领域类型：接口一旦改字段名，这里编译期就会报错，
// 而不是等到运行时"某个列表忽然空了"。
type ModelRow = ModelConfig;
type AgentUsage = Record<string, number>; // model_id -> 使用 agent 数

/** 就绪自检的返回形状（`GET /models/diagnostics`）——只报"密钥在不在"，不含密钥内容 */
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
  const cid = useActiveCompanyId();

  const [convertModel, setConvertModel] = useState("");
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

  // 写入密钥（保存到本机 .env，立即生效）
  const [keyEnv, setKeyEnv] = useState("");
  const [keyValue, setKeyValue] = useState("");
  const [keySaving, setKeySaving] = useState(false);

  /* ---- 数据加载：列表 + 员工绑定关系 + 就绪自检 ----
   *
   * 改造前这里有三处问题：
   *   1. 5 个请求**串行**发出（列表→员工→逐个查绑定），员工越多越慢；
   *   2. 没有缓存，每次进页面都从零拉一遍，切回来还要等；
   *   3. 就绪自检接口后端早就有了，前端从来没调用过——
   *      于是"模型配好了但密钥没配"这件事，用户要等到任务失败才知道。
   */
  const {
    data: catalog, error: loadError, loading: loadingList, refresh: refreshModels,
  } = useResource(
    cid ? `models:${cid}` : null,
    async () => {
      const list = await api.get<ModelConfig[]>("/models");
      const agents = await api.get<Agent[]>("/agents");
      // 并发查绑定关系：N 个员工只花一次往返的时间
      const bindings = await Promise.all(
        agents.map((a) =>
          api.get<{ model_id: string }[]>(`/agents/${a.id}/models`).catch(() => [])),
      );
      const usage: AgentUsage = {};
      for (const binds of bindings) {
        for (const b of binds) usage[b.model_id] = (usage[b.model_id] || 0) + 1;
      }
      return { list, agents, usage };
    },
  );

  const models = useMemo(() => catalog?.list ?? [], [catalog]);
  const usage = catalog?.usage ?? {};

  const { data: diag } = useResource<Diagnostics>(
    cid ? `models-diag:${cid}` : null,
    () => api.get<Diagnostics>("/models/diagnostics"),
  );

  const { data: convertCfg } = useResource<ConvertConfig>(
    cid ? `convert-cfg:${cid}` : null,
    () => api.get<ConvertConfig>("/models/convert-config"),
  );

  // 自检结果回来后回填下拉框（下拉框是可编辑控件，必须用本地 state）
  useEffect(() => {
    if (convertCfg) setConvertModel(convertCfg.convert_model_id || "");
  }, [convertCfg]);

  /** 改动数据后：本页重取 + 让别处对模型的引用一起失效（如 Agent 页的模型下拉） */
  async function reload() {
    invalidate("models:");
    invalidate("models-diag:");
    await refreshModels();
  }

  /* ---- 手动添加单个模型配置 ---- */
  //
  // 校验规则与后端 `ModelCreate` 保持一致（名称/模型 ID 必填，URL 与变量名有格式要求）。
  // 放在前端不是"替后端把关"，而是**让用户在提交前就知道问题在哪**——
  // 之前点击"添加配置"若格式不对，只会收到一句技术性的 422。
  const modelForm = useFormValidation<{ name: string; url: string; keyEnv: string; modelId: string }>(
    useMemo(
      () => ({
        name: [V.required("显示名"), V.maxLen(60, "显示名")],
        url: [V.url("Base URL")],
        keyEnv: [V.envVarName("环境变量名")],
        modelId: [V.required("模型 ID"), V.identifier("模型 ID"), V.maxLen(120, "模型 ID")],
      }),
      [],
    ),
  );

  async function addModel() {
    const values = { name: provName, url: provUrl, keyEnv: provKeyEnv, modelId: provModelId };
    if (!modelForm.check(values)) {
      notify("warning", modelForm.first() || "请检查填写内容");
      return;
    }
    try {
      await api.post("/models", {
        name: provName, provider_type: provType,
        base_url: provUrl || null, api_key_env: provKeyEnv || null,
        model_id: provModelId,
        // 必须带上当前公司：模型配置的归属靠这个字段。
        // 不带的话后端会登记成"未归属/共享"，之后既不能改名也不能删除，
        // 用户只会觉得"这东西坏了"。
        company_id: getActiveCompanyId() || null,
      });
      notify("success", `已添加 ${provName}`);
      setProvName(""); setProvUrl(""); setProvKeyEnv(""); setProvModelId("");
      modelForm.reset();
      await reload();
    } catch (e) {
      notify("error", `添加失败：${describeError(e, "添加失败")}`);
    }
  }

  /* ---- 认领无主配置 ----
   *
   * 升级前的遗留配置 `company_id` 为空，能看见但不能改不能删。
   * 后端要求"先认领"，前端就得给出这个按钮——否则用户只会看到
   * 一个点了没反应的删除键。
   */
  async function claimModel(m: ModelRow) {
    const myCid = getActiveCompanyId();
    if (!myCid) { notify("warning", "请先选择公司"); return; }
    try {
      await api.patch(`/models/${m.id}`, { company_id: myCid });
      notify("success", `已把「${m.name}」归入当前公司，现在可以改名或删除了`);
      await reload();
    } catch (e) {
      notify("error", describeError(e, "认领失败"));
    }
  }

  /* ---- 从供应商拉取模型列表 ---- */
  async function fetchModels() {
    if (!fetchUrl) return;
    setFetching(true);
    setRemoteModels([]);
    try {
      const data = await api.post<ProviderModelsResult>(
        "/models/providers/list-models",
        { base_url: fetchUrl, api_key: fetchKey || null },
      );
      if (data.status === "ok") {
        setRemoteModels(data.models);
        if (!importPrefix) {
          try { setImportPrefix(new URL(fetchUrl).hostname.split(".")[0]); } catch { /* 非法 URL 时保持默认前缀 */ }
        }
      } else {
        setMsg(`❌ ${data.error}`);
      }
    } catch (e) {
      setMsg(`❌ ${describeError(e, "拉取模型列表失败")}`);
    }
    setFetching(false);
  }

  /* ---- 批量导入勾选的模型 ---- */
  async function importSelected() {
    if (!checked.size || !fetchUrl) return;
    try {
      const data = await api.post<BatchImportResult>(
        "/models/providers/batch-import",
        {
          name_prefix: importPrefix || "provider",
          provider_type: "openai_compat",
          base_url: fetchUrl,
          model_ids: Array.from(checked),
        },
      );
      setMsg(`✅ 导入 ${data.imported_count} 个模型${
        data.skipped_existing?.length ? `，跳过已存在 ${data.skipped_existing.length} 个` : ""}`);
      setChecked(new Set());
      setRemoteModels([]);
      await reload();
    } catch (e) {
      setMsg(`❌ ${describeError(e, "导入失败")}`);
    }
  }

  /* ---- 连通性测试 ---- */
  async function testModel(mid: string) {
    setTesting(mid);
    try {
      const d = await api.post<ModelTestResult>(`/models/${mid}/test`);
      setMsg(d.status === "ok" ? `✅ 连通正常，模型回复: ${d.reply}` : `❌ ${d.error?.slice(0, 120)}`);
    } catch (e) {
      setMsg(`❌ ${describeError(e, "连通测试失败")}`);
    }
    setTesting(null);
  }

  /**
   * 把密钥写进本机 .env 并立即生效。
   *
   * 过去这只能手工改文件再重启后端；对非技术用户等于"配不了模型"。
   * 安全约束由后端把关（仅超管、变量名白名单、600 权限、不回显），
   * 前端只负责把 Key 安全地提交一次并清空输入框。
   */
  const effectiveKeyEnv =
    keyEnv || diag?.providers?.[0]?.default_env_var || "OPENAI_COMPAT_API_KEY";

  async function saveKey() {
    const key = keyValue.trim();
    if (key.length < 8) {
      notify("warning", "请粘贴完整的 API Key");
      return;
    }
    setKeySaving(true);
    try {
      const r = await api.post<{ env_name: string; masked: string }>(
        "/models/env-key", { env_name: effectiveKeyEnv, api_key: key });
      notify("success", `密钥已写入本机 .env（${r.masked}），已立即生效`);
      setKeyValue("");
      await reload();
    } catch (e) {
      notify("error", describeError(e, "保存密钥失败"));
    }
    setKeySaving(false);
  }

  /** 把这条模型绑给公司里所有还没有首选模型的员工 */
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

  async function delModel(m: ModelRow) {
    if (!(await confirmAction({
      title: `删除配置「${m.name}」？`,
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

  /* ---- 就绪状态：把"缺哪一步"直接说清楚 ----
   *
   * 三种状态要给三种下一步动作，不能混成一句"配置不完整"：
   *   没有任何模型   → 去添加
   *   有模型但没密钥 → 去 .env 填变量名
   *   一切就绪       → 不打扰
   */
  const readiness = useMemo(() => {
    if (!diag) return null;
    if (diag.ready) return { tone: "ok" as const, text: `已就绪 · ${diag.usable_model_ids.length} 个模型可调用` };
    if (!diag.model_count) return { tone: "warn" as const, text: "还没有配置模型，员工无法开工" };
    return { tone: "warn" as const, text: diag.hint || "模型已登记但缺少密钥" };
  }, [diag]);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">模型配置中心</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          管理大模型供应商 · 拉取可用模型 · 为 Agent 配置驱动底座
        </p>
      </div>

      {/* 就绪自检横幅：这是"配完到底能不能跑"的唯一明确回答 */}
      {readiness && (
        <div className="flex items-start gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={readiness.tone === "ok"
            ? { background: "rgba(16,185,129,0.08)", border: "1px solid rgba(16,185,129,0.3)", color: "#6ee7b7" }
            : { background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.3)", color: "#fcd34d" }}>
          <span>{readiness.tone === "ok" ? "✅" : "⚠️"}</span>
          <span className="flex-1">{readiness.text}</span>
        </div>
      )}

      {/* 缺密钥时直接给输入框：本机应用里"让用户去改 .env 再重启"是不现实的 */}
      {diag && !diag.ready && (
        <div className="card p-4 space-y-2">
          <h3 className="font-semibold text-sm">🔐 填写 API Key（保存到本机，立即生效）</h3>
          <p className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
            密钥只写入本机 <code>apps/api/.env</code>（文件权限 600），
            <b>不上传、不入库、不回显</b>。仅系统管理员可操作。
          </p>
          <div className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
            <select
              value={effectiveKeyEnv}
              onChange={(e) => setKeyEnv(e.target.value)}
              className="input sm:w-64 !py-2 text-xs"
              aria-label="密钥变量名"
            >
              {(diag.providers.length ? diag.providers : [
                { provider_type: "openai_compat", default_env_var: "OPENAI_COMPAT_API_KEY" },
              ]).map((p) => (
                <option key={p.default_env_var} value={p.default_env_var}>
                  {PROVIDER_LABELS[p.provider_type] || p.provider_type} · {p.default_env_var}
                </option>
              ))}
            </select>
            <input
              type="password"
              value={keyValue}
              onChange={(e) => setKeyValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void saveKey()}
              placeholder="粘贴 API Key"
              autoComplete="off"
              spellCheck={false}
              className="input flex-1 !py-2 text-xs"
              aria-label="API Key"
            />
            <button
              onClick={() => void saveKey()}
              disabled={keySaving || !keyValue.trim()}
              className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40"
            >
              {keySaving ? "保存中…" : "保存并生效"}
            </button>
          </div>
        </div>
      )}

      {loadError && (
        <div className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
          <span>🔌</span>
          <span className="flex-1">加载模型配置失败：{loadError}</span>
          <button onClick={() => refreshModels()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

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
            {models.map((m) => (
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
          {models.map((m) => {
            const usable = diag?.usable_model_ids?.includes(m.id);
            return (
              <div key={m.id} className="flex items-center gap-3 px-3 py-2.5 rounded-xl transition-colors hover:bg-white/[0.03]"
                style={{ background: "var(--surface-2)" }}>
                {/* 小圆点从"恒定绿色"改为真实状态：绿色=密钥就位，琥珀=登记了但调不通。
                    之前它永远是绿的，等于一个从不报警的告警灯。 */}
                <span className={`w-2 h-2 rounded-full shrink-0 ${usable ? "bg-emerald-400" : "bg-amber-400"}`}
                  title={usable ? "密钥已就位，可直接调用" : "尚未取到密钥，调用会失败"} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm truncate flex items-center gap-1.5">
                    {m.name}
                    {m.unowned && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded shrink-0"
                        style={{ background: "rgba(245,158,11,0.18)", color: "#fcd34d" }}
                        title="升级前登记的遗留配置，未归属任何公司：可认领后再管理">
                        未归属
                      </span>
                    )}
                  </div>
                  <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                    {PROVIDER_LABELS[m.provider_type] || m.provider_type} · {m.model_id}
                    {m.base_url ? ` · ${m.base_url}` : ""}
                  </div>
                </div>
                <span className="text-[11px] px-2 py-0.5 rounded-md shrink-0"
                  style={{ background: "rgba(99,102,241,0.15)", color: "#a5b4fc" }}>
                  {usage[m.id] || 0} 名员工使用
                </span>
                {m.unowned ? (
                  <button onClick={() => claimModel(m)}
                    title="把这条遗留配置归入当前公司，之后即可改删"
                    className="btn-primary !py-1 text-xs shrink-0">
                    认领
                  </button>
                ) : (
                  <>
                    <button onClick={() => void bindAll(m)}
                      title="设为公司全部员工的首选模型；已有首选模型的员工不受影响"
                      className="btn-ghost text-xs py-1 shrink-0">
                      绑定全员
                    </button>
                    <button onClick={() => testModel(m.id)} disabled={testing === m.id}
                      className="btn-ghost text-xs py-1 shrink-0">
                      {testing === m.id ? "测试中…" : "测试"}
                    </button>
                    <button onClick={() => delModel(m)}
                      className="text-xs py-1 px-2 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10 shrink-0">
                      删除
                    </button>
                  </>
                )}
              </div>
            );
          })}
          {!models.length && !loadingList && (
            <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
              还没有配置模型，先从上方拉取或手动添加
            </p>
          )}
          {loadingList && !models.length && (
            <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>加载中…</p>
          )}
        </div>
      </div>

      {/* 手动添加 */}
      <details className="card card-static">
        <summary className="text-sm cursor-pointer select-none" style={{ color: "var(--muted)" }}>
          ➕ 手动添加模型配置（高级）
        </summary>
        <div className="grid grid-cols-2 gap-3 mt-3">
          <Field label="显示名" required error={modelForm.errors.name} hint="在员工配置里看到的名称" htmlFor="pm-name">
            <input id="pm-name" placeholder="如 Agnes Flash" value={provName}
              onChange={(e) => { setProvName(e.target.value); modelForm.clear("name"); }}
              className="input" aria-invalid={!!modelForm.errors.name} />
          </Field>
          <Field label="供应商类型" htmlFor="pm-type">
            <select id="pm-type" value={provType} onChange={(e) => setProvType(e.target.value)} className="input">
              {Object.entries(PROVIDER_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Base URL" error={modelForm.errors.url} hint="留空则使用该供应商的官方地址" htmlFor="pm-url">
            <input id="pm-url" placeholder="https://api.example.com/v1" value={provUrl}
              onChange={(e) => { setProvUrl(e.target.value); modelForm.clear("url"); }}
              className="input" aria-invalid={!!modelForm.errors.url} />
          </Field>
          <Field label="API Key 环境变量名" error={modelForm.errors.keyEnv}
            hint="只填变量名，不要填密钥本身；本地服务可留空" htmlFor="pm-keyenv">
            <input id="pm-keyenv" placeholder="如 HERMES_API_KEY" value={provKeyEnv}
              onChange={(e) => { setProvKeyEnv(e.target.value); modelForm.clear("keyEnv"); }}
              className="input" aria-invalid={!!modelForm.errors.keyEnv} />
          </Field>
          <Field label="模型 ID" required error={modelForm.errors.modelId}
            hint="调用接口时真正传给供应商的标识" htmlFor="pm-modelid" className="col-span-2">
            <input id="pm-modelid" placeholder="如 agnes-2.5-flash / claude-sonnet-4" value={provModelId}
              onChange={(e) => { setProvModelId(e.target.value); modelForm.clear("modelId"); }}
              className="input" aria-invalid={!!modelForm.errors.modelId} />
          </Field>
          <button onClick={addModel} className="btn-primary col-span-2">添加配置</button>
        </div>
      </details>
    </div>
  );
}
