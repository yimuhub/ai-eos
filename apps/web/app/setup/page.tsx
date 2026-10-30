"use client";

// 首次安装引导（/setup）——**公开页**。
//
// ## 为什么是独立页面而不是 (main) 里的向导
// (main)/layout.tsx 的第一件事是 `GET /auth/me` 校验登录态，没有 token 就 replace("/login")。
// 首次安装时系统里一个账号都没有，任何"建账号"的界面都必须待在守卫之外，
// 否则用户会被弹回登录页，永远走不完安装。
//
// ## 四步
//   ① 管理员账号：创建唯一的 superadmin（契约 POST /setup）
//   ② 你的公司：名称 / 简介 / 行业（12 个 chip，只影响组织架构推荐）
//   ③ 组织架构：空公司 vs 按行业推荐，再选规模（个体户 / 工作室 / 中小公司 / 集团大厂）
//   ④ 接入大模型：登记模型 + 填 Key + 测试连通 + 绑给全部 AI 员工（可跳过）
//
// 第 4 步发生在安装成功之后（此时已有 token）：
// 没有模型，员工接任务只会回「尚未绑定任何模型」——这是过去新装系统的默认结局。
// Key 只写进本机 apps/api/.env（后端原子写入、600 权限、立即生效），
// 不入库、不上传、不回显。
//
// ## 关键约束
// - 预设 id 不写死：GET /org-presets 列表 + 行业字段模糊匹配，匹配不到才用 `tech`；
//   部门名一律取自 GET /org-presets/{pid}，规模只决定"取前几个"。
// - apply 失败**不阻塞安装**：公司已经建好了，组织架构漏了也能进系统后在「组织架构」里补。
// - 预设目录 GET /org-presets 与详情是公开的（安装前没有账号也能预览部门）；
//   仍然带 `noAuthRedirect`，避免旧后端返回 401 时 api 层把正在填的表单整页跳走。
// - 完成页提供「推倒重建」（POST /setup/rebuild）：清掉这次安装的全部数据回到第 1 步。

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import {
  api,
  ApiError,
  clearToken,
  describeError,
  setActiveCompanyId,
  setRefreshToken,
  setToken,
  type RequestOptions,
} from "@/lib/api";
import { V, runRules } from "@/lib/validate";
import Field from "@/components/Field";
import {
  FALLBACK_PRESET_ID,
  INDUSTRIES,
  SCALES,
  departmentSubset,
  industryLabel,
  matchPresetId,
  passwordStrength,
  presetErrorMessage,
  presetPool,
  previewCounts,
  setupErrorMessage,
  setupStatusErrorMessage,
  type ApplyResult,
  type OrgScale,
  type PresetDetail,
  type PresetSummary,
  type SetupResult,
  type SetupStatus,
} from "@/lib/setup";

/** 预设类请求的统一选项：静默 + 401 不整页跳登录（安装前本就没有登录态） */
const PRESET_OPTS: RequestOptions = {
  silent: true,
  timeoutMs: 20_000,
  noAuthRedirect: true,
};

type Step = 1 | 2 | 3 | 4;
type FieldKey = "username" | "email" | "password" | "confirm";

type OrgOutcome =
  | { status: "skipped"; departments: 0; agents: 0; note: string }
  | { status: "created"; departments: number; agents: number; presetIndustry: string }
  | { status: "failed"; departments: 0; agents: 0; reason: string };

/** 第 4 步的结果：接入成功（含测试是否通过）或用户主动跳过 */
type ModelOutcome =
  | { status: "bound"; modelId: string; modelName: string; modelIdOnProvider: string;
      bound: number; agentCount: number; testOk: boolean; testError?: string }
  | { status: "skipped" };

type DoneState = {
  username: string;
  email: string;
  companyName: string;
  org: OrgOutcome;
  model: ModelOutcome | null;
};

/** 安装成功后的上下文：第 4 步要用它登记模型、绑员工 */
type Installed = {
  companyId: string;
  companyName: string;
  username: string;
  email: string;
  org: OrgOutcome;
};

const STEPS: { n: Step; title: string; sub: string }[] = [
  { n: 1, title: "管理员账号", sub: "唯一的管理者" },
  { n: 2, title: "你的公司", sub: "名称与业务方向" },
  { n: 3, title: "组织架构", sub: "预设 · 规模" },
  { n: 4, title: "接入大模型", sub: "让员工能思考" },
];

/** 常见模型服务预设：降低"我该填什么"的门槛；key_hint 说明 Key 从哪来 */
const MODEL_PRESETS = [
  {
    label: "Agnes（推荐 · 国内可用）",
    name: "Agnes Flash",
    provider_type: "openai_compat",
    base_url: "https://api.agnes-ai.cn/v1",
    model_id: "agnes-2.5-flash",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 agnes-ai.cn 注册 → 控制台「API 密钥」复制 Key，新用户有免费额度可先试。",
    custom: false,
  },
  {
    label: "DeepSeek",
    name: "DeepSeek Chat",
    provider_type: "openai_compat",
    base_url: "https://api.deepseek.com/v1",
    model_id: "deepseek-chat",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 platform.deepseek.com 注册 → 左侧「API keys」创建并复制，价格相对便宜。",
    custom: false,
  },
  {
    label: "OpenAI 官方",
    name: "OpenAI GPT",
    provider_type: "openai_compat",
    base_url: "https://api.openai.com/v1",
    model_id: "gpt-4o-mini",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint: "去 platform.openai.com 注册 → 右上角「API keys」新建并复制（需境外网络）。",
    custom: false,
  },
  {
    label: "Anthropic Claude",
    name: "Claude",
    provider_type: "anthropic",
    base_url: "https://api.anthropic.com",
    model_id: "claude-3-5-sonnet-latest",
    api_key_env: "ANTHROPIC_API_KEY",
    key_hint: "去 console.anthropic.com 注册 → 「API Keys」创建并复制（需境外网络）。",
    custom: false,
  },
  {
    label: "自定义 API",
    name: "",
    provider_type: "openai_compat",
    base_url: "",
    model_id: "",
    api_key_env: "OPENAI_COMPAT_API_KEY",
    key_hint:
      "任何 OpenAI 兼容接口都行：vLLM、Ollama、OneAPI、自建网关、公司代理站…地址填到 /v1 为止；本机无鉴权服务可以把 Key 留空。",
    custom: true,
  },
];

/** 让某一步在屏幕上至少停留这么久，避免一闪而过看不清 */
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const HEAD: Record<Step, { title: string; desc: string }> = {
  1: {
    title: "先创建管理员账号",
    desc: "这是整台系统的 owner：拥有全部公司数据，可以创建更多账号。它只在首次初始化时创建一次，之后所有入口都走正常登录。",
  },
  2: {
    title: "给你的 AI 公司起个名字",
    desc: "公司是员工、项目、任务、成果、知识库的归属单元。之后还可以创建更多公司，并在工作台随时切换。",
  },
  3: {
    title: "组织架构",
    desc: "先定骨架，AI 员工随后自动配齐；也可以 0 部门空公司进入，之后再搭。",
  },
  4: {
    title: "给 AI 员工接上大脑",
    desc: "员工的思考来自大模型服务。选一个服务商、粘贴 API Key，系统会自动登记模型、保存密钥、测试连通，并绑定给公司里全部 AI 员工。",
  },
};

export default function SetupPage() {
  const router = useRouter();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* ---------------- 入口守卫：已初始化就回登录页 ---------------- */
  const [gate, setGate] = useState<"checking" | "ready" | "unknown">("checking");
  const [gateError, setGateError] = useState("");

  const checkStatus = useCallback(async () => {
    setGate("checking");
    setGateError("");
    try {
      const s = await api.get<SetupStatus>("/setup/status", { silent: true, timeoutMs: 15_000 });
      if (!alive.current) return;
      if (s?.needs_setup === false) {
        router.replace("/login");
        return;
      }
      setGate("ready");
    } catch (e) {
      if (!alive.current) return;
      // 状态接口挂了不能白屏，也不能把用户锁在这一页：
      // 留在原地给出明确原因 + 重试 + 「先去登录页」出口（登录页在同样失败时不会把人送回来）。
      setGateError(setupStatusErrorMessage(e));
      setGate("unknown");
    }
  }, [router]);

  useEffect(() => {
    void checkStatus();
  }, [checkStatus]);

  /* ---------------- 步骤与表单 ---------------- */
  const [step, setStep] = useState<Step>(1);

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [touched, setTouched] = useState<Record<FieldKey, boolean>>({
    username: false,
    email: false,
    password: false,
    confirm: false,
  });
  const [attempted1, setAttempted1] = useState(false);

  const [companyName, setCompanyName] = useState("");
  const [description, setDescription] = useState("");
  const [industry, setIndustry] = useState(INDUSTRIES[0].key);
  const [attempted2, setAttempted2] = useState(false);

  const [orgMode, setOrgMode] = useState<"preset" | "empty">("preset");
  const [scale, setScale] = useState<OrgScale>("smb");
  const [presets, setPresets] = useState<PresetSummary[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [presetsError, setPresetsError] = useState("");
  const [presetManual, setPresetManual] = useState<string | null>(null);
  const [detail, setDetail] = useState<PresetDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [submitStatus, setSubmitStatus] = useState<number | null>(null);
  // 创建过程的分阶段进度：0=账号，1=公司，2=部门，3=AI 员工，4=收尾
  const [stageIdx, setStageIdx] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [done, setDone] = useState<DoneState | null>(null);

  // 推倒重建：完成页上的二次确认
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [rebuildText, setRebuildText] = useState("");
  const [rebuildBusy, setRebuildBusy] = useState(false);
  const [rebuildError, setRebuildError] = useState("");
  const [resetNotice, setResetNotice] = useState("");

  /* ---------------- 第 4 步：接入大模型（发生在安装成功之后） ---------------- */
  const [installed, setInstalled] = useState<Installed | null>(null);
  const [modelPresetIdx, setModelPresetIdx] = useState(0);
  const [modelForm, setModelForm] = useState(() => ({ ...MODEL_PRESETS[0] }));
  const [keyInput, setKeyInput] = useState("");
  const [showKey, setShowKey] = useState(false);
  // 「模型 ID」默认从服务商读取后下拉选择；读取失败或服务商不支持列模型时，可切回手填
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [modelFetchBusy, setModelFetchBusy] = useState(false);
  const [modelFetchError, setModelFetchError] = useState("");
  const [manualModel, setManualModel] = useState(false);
  // 显示名称跟随所选模型自动更新，用户改过之后就不再覆盖
  const [nameAuto, setNameAuto] = useState(true);
  const [modelBusy, setModelBusy] = useState(false);
  // 0=未开始，1..4=正在做第几件事（登记→存密钥→测连通→绑员工），5=全部完成
  const [modelStage, setModelStage] = useState(0);
  const [modelError, setModelError] = useState("");
  const [modelOutcome, setModelOutcome] = useState<ModelOutcome | null>(null);
  // 已经登记过的模型 id：失败重试时不再重复创建，避免一次失败留下两条配置
  const [modelCreatedId, setModelCreatedId] = useState<string | null>(null);

  const installedAgents = installed?.org.status === "created" ? installed.org.agents : 0;

  /**
   * 从服务商读取可用模型列表（GET /v1/models）。
   *
   * Key 只在这次请求里用于鉴权，不落库；列表读回来后用户直接下拉选择，
   * 不再手敲模型 ID——模型 ID 拼错是最常见的"接入失败"原因。
   */
  async function fetchModelOptions() {
    const baseUrl = modelForm.base_url.trim();
    if (!baseUrl) {
      setModelFetchError("请先填写接口地址，再读取模型列表。");
      return;
    }
    setModelFetchBusy(true);
    setModelFetchError("");
    try {
      const r = await api.post<{ status: string; models?: string[]; error?: string }>(
        "/models/providers/list-models",
        {
          base_url: baseUrl,
          api_key: keyInput.trim() || undefined,
          provider_type: modelForm.provider_type,
        },
        { silent: true, timeoutMs: 30_000 },
      );
      const list = r?.status === "ok" ? (r.models ?? []) : [];
      if (!list.length) {
        setModelOptions([]);
        setModelFetchError(
          r?.error || "该服务商没有返回任何模型，可改为手动填写模型 ID。");
        return;
      }
      setModelOptions(list);
      setManualModel(false);
      setModelForm((f) => ({
        ...f,
        model_id: list.includes(f.model_id) ? f.model_id : list[0],
        name: nameAuto ? (list.includes(f.model_id) ? f.name : list[0]) : f.name,
      }));
    } catch (e) {
      setModelOptions([]);
      setModelFetchError(describeError(e, "读取模型列表失败"));
    } finally {
      setModelFetchBusy(false);
    }
  }

  /**
   * 第 4 步：登记模型 → 保存密钥 → 测试连通 → 绑定全部员工。
   *
   * 绑定必须做：运行时只认 agent_model_configs，缺了它员工仍会报
   * 「尚未绑定任何模型」。连通测试失败不阻塞——Key 可能有效但该模型
   * 暂时不可用，先绑定、进系统后换模型即可；结果里如实标出测试失败。
   */
  async function handleConnectModel() {
    if (!installed || modelBusy) return;
    const name = modelForm.name.trim() || modelForm.model_id.trim();
    const baseUrl = modelForm.base_url.trim();
    const modelId = modelForm.model_id.trim();
    const key = keyInput.trim();
    if (!baseUrl) {
      setModelError("请填写接口地址。");
      return;
    }
    if (!modelId) {
      setModelError("请先点「读取模型」从列表里选一个，或改为手动填写。");
      return;
    }
    // 本机服务（Ollama / vLLM 未开鉴权）可留空；填了就必须像样
    if (key && key.length < 8) {
      setModelError("API Key 至少 8 个字符；本机无鉴权服务可以留空。");
      return;
    }

    setModelBusy(true);
    setModelError("");
    try {
      let mid = modelCreatedId;
      if (!mid) {
        setModelStage(1);
        const m = await api.post<{ id: string }>(
          "/models",
          {
            name,
            provider_type: modelForm.provider_type,
            base_url: baseUrl,
            model_id: modelId,
            api_key_env: modelForm.api_key_env,
            config: {},
            company_id: installed.companyId,
          },
          { silent: true, timeoutMs: 20_000 },
        );
        mid = m.id;
        setModelCreatedId(mid);
      } else {
        // 失败重试时表单可能改过（换了模型 / 地址），先把已登记的配置对齐
        await api.patch(
          `/models/${mid}`,
          {
            name,
            provider_type: modelForm.provider_type,
            base_url: baseUrl,
            model_id: modelId,
            api_key_env: modelForm.api_key_env,
          },
          { silent: true, timeoutMs: 20_000 },
        );
      }

      setModelStage(2);
      await sleep(350);
      if (key) {
        await api.post(
          "/models/env-key",
          { env_name: modelForm.api_key_env, api_key: key },
          { silent: true, timeoutMs: 20_000 },
        );
      }

      setModelStage(3);
      await sleep(250);
      let testOk = false;
      let testError = "";
      try {
        const t = await api.post<{ status: string; error?: string }>(
          `/models/${mid}/test`, {}, { silent: true, timeoutMs: 30_000 });
        testOk = t?.status === "ok";
        testError = t?.error || "";
      } catch (e) {
        testError = describeError(e, "连通测试失败");
      }

      setModelStage(4);
      await sleep(250);
      let bound = 0;
      let total = installedAgents;
      if (installedAgents > 0) {
        const b = await api.post<{ bound: number; agent_count: number }>(
          `/models/${mid}/bind-agents`,
          { company_id: installed.companyId },
          { silent: true, timeoutMs: 30_000 },
        );
        bound = b?.bound ?? 0;
        total = b?.agent_count ?? installedAgents;
      }

      setModelStage(5);
      setModelOutcome({
        status: "bound",
        modelId: mid,
        modelName: name,
        modelIdOnProvider: modelId,
        bound,
        agentCount: total,
        testOk,
        testError: testError || undefined,
      });
      setKeyInput("");
    } catch (e) {
      setModelError(describeError(e, "接入大模型失败"));
    } finally {
      setModelBusy(false);
    }
  }

  function finishSetup(model: ModelOutcome | null = modelOutcome) {
    if (!installed) return;
    setDone({
      username: installed.username,
      email: installed.email,
      companyName: installed.companyName,
      org: installed.org,
      model,
    });
  }

  useEffect(() => {
    if (!submitting) return;
    setElapsed(0);
    const t = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [submitting]);

  /* ---------------- 校验（实时计算，只按 touched 决定是否展示） ---------------- */
  const errors = useMemo(
    () => ({
      username:
        runRules(username, [V.required("用户名"), V.minLen(2, "用户名"), V.maxLen(32, "用户名")]) ?? "",
      email: runRules(email, [V.required("邮箱"), V.email()]) ?? "",
      password: runRules(password, [V.required("密码"), V.password()]) ?? "",
      confirm: !confirm ? "请再次输入密码" : confirm !== password ? "两次输入的密码不一致" : "",
    }),
    [username, email, password, confirm],
  );
  const step1Valid = !errors.username && !errors.email && !errors.password && !errors.confirm;

  const companyError =
    runRules(companyName, [V.required("公司名称"), V.maxLen(60, "公司名称")]) ?? "";
  const descError = runRules(description, [V.maxLen(200, "简介")]) ?? "";

  function visibleError(key: FieldKey): string {
    return touched[key] || attempted1 ? errors[key] : "";
  }

  const strength = passwordStrength(password);

  /* ---------------- 预设：列表 + 自动匹配 + 详情 ---------------- */
  const listLoaded = useRef(false);

  const loadPresets = useCallback(async () => {
    setListLoading(true);
    setPresetsError("");
    try {
      const raw = await api.get<PresetSummary[]>("/org-presets", PRESET_OPTS);
      if (!alive.current) return;
      setPresets(presetPool(Array.isArray(raw) ? raw : []));
      listLoaded.current = true;
    } catch (e) {
      if (!alive.current) return;
      setPresetsError(presetErrorMessage(e));
      listLoaded.current = false; // 下次进入第 3 步再试一次
    } finally {
      if (alive.current) setListLoading(false);
    }
  }, []);

  useEffect(() => {
    if (step !== 3 || listLoaded.current) return;
    void loadPresets();
  }, [step, loadPresets]);

  const autoPresetId = useMemo(
    () => (presets.length ? matchPresetId(presets, industry) : FALLBACK_PRESET_ID),
    [presets, industry],
  );
  const presetId = presetManual ?? autoPresetId;

  useEffect(() => {
    if (step !== 3 || orgMode !== "preset" || !presetId) return;
    if (detail && detail.id === presetId) return; // 已缓存
    let cancelled = false;
    setDetailLoading(true);
    setDetailError("");
    api
      .get<PresetDetail>(`/org-presets/${encodeURIComponent(presetId)}`, PRESET_OPTS)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setDetailError(presetErrorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [step, orgMode, presetId, detail]);

  const activeDetail = detail && detail.id === presetId ? detail : null;
  const presetIndustry =
    activeDetail?.industry ?? presets.find((p) => p.id === presetId)?.industry ?? "";

  // 空公司 / 个体户是确定的 0；其余要等预设详情，取不到就显示"—"而不是假 0
  const orgCounts = useMemo(() => {
    if (orgMode === "empty" || scale === "solo") return { departments: 0, agents: 0, names: [] as string[] };
    return previewCounts(activeDetail, scale);
  }, [orgMode, scale, activeDetail]);

  // 预览取不到数据时的说明：列表级错误已在上方展示过，这里不重复长文案
  const previewFallback = detailLoading
    ? "正在读取预设数据…"
    : presetsError
      ? "预设数据取不到（原因见上方），部门名单无法预览；提交时会再取一次，取到就按所选规模生成。"
      : detailError || "预设数据取不到，部门名单无法预览；提交时会再取一次，取到就按所选规模生成。";

  function chooseIndustry(key: string) {
    setIndustry(key);
    setPresetManual(null); // 行业变了就回到自动匹配
  }

  /* ---------------- 提交 ---------------- */
  /**
   * 创建过程的阶段清单：与真实请求一一对应，不是纯装饰。
   *
   * 后端把"账号 + 公司 + owner 成员关系"放在同一个事务里，部门与 AI 员工
   * 由 /org-presets/{pid}/apply 一次建完。因此这里把两次真实请求拆成
   * 可见的几段，每段**至少停留一小会儿**，让用户看清正在发生什么；
   * 完成与否只由真实响应决定，失败就退回表单并给出原因。
   */
  const creatingStages = useMemo(() => {
    const list: { key: string; title: string; hint: string }[] = [
      { key: "account", title: "创建管理员账号", hint: "写入超级管理员，建立登录身份" },
      { key: "company", title: "建立公司档案", hint: `为「${companyName.trim() || "你的公司"}」准备数据空间` },
    ];
    if (orgMode === "preset" && scale !== "solo") {
      list.push({
        key: "org",
        title: "搭建部门骨架",
        hint: `按「${presetIndustry || industryLabel(industry)}」蓝图生成部门`,
      });
      list.push({ key: "agents", title: "配置 AI 员工", hint: "给每个岗位配上专家人设" });
    }
    list.push({ key: "finish", title: "初始化工作台", hint: "准备就绪，马上带你进去" });
    return list;
  }, [orgMode, scale, companyName, presetIndustry, industry]);

  const advanceTo = useCallback(
    (key: string) => {
      const idx = creatingStages.findIndex((s) => s.key === key);
      if (idx >= 0 && alive.current) setStageIdx(idx);
    },
    [creatingStages],
  );

  /**
   * 落 apply 用的预设 id。
   *
   * 安装前页面里拿不到预设列表（接口要鉴权），此时 presetId 只是兜底值；
   * POST /setup 成功后已有 token，这里再按行业重新匹配一次——
   * 否则第一次安装会永远落到 tech，用户在步骤 2 选的行业就白选了。
   */
  async function resolvePresetId(): Promise<string> {
    if (presetManual) return presetManual;
    if (presets.length) return matchPresetId(presets, industry);
    try {
      const raw = await api.get<PresetSummary[]>("/org-presets", PRESET_OPTS);
      const pool = presetPool(Array.isArray(raw) ? raw : []);
      if (pool.length) {
        const matched = matchPresetId(pool, industry);
        setPresets(pool); // 顺手把下拉列表补齐（完成页返回时不再空着）
        return matched;
      }
    } catch {
      /* 列表仍取不到：用兜底 id，apply 若失败也不会阻塞安装 */
    }
    return FALLBACK_PRESET_ID;
  }

  async function createOrg(companyId: string): Promise<OrgOutcome> {
    if (orgMode === "empty") {
      return { status: "skipped", departments: 0, agents: 0, note: "按你的选择创建了空公司" };
    }
    if (scale === "solo") {
      return { status: "skipped", departments: 0, agents: 0, note: "个体户：不生成部门与 AI" };
    }
    const pid = await resolvePresetId();
    try {
      // 部门名必须来自接口返回，不能自己编；本地有缓存就复用，没有就现取
      const d =
        activeDetail && activeDetail.id === pid
          ? activeDetail
          : await api.get<PresetDetail>(`/org-presets/${encodeURIComponent(pid)}`, PRESET_OPTS);      const names = departmentSubset(d, scale);
      if (!names.length) {
        return { status: "skipped", departments: 0, agents: 0, note: `预设「${d.industry}」没有可引入的部门` };
      }
      const r = await api.post<ApplyResult>(
        `/org-presets/${encodeURIComponent(pid)}/apply`,
        { company_id: companyId, departments: names, dry_run: false },
        PRESET_OPTS,
      );
      return {
        status: "created",
        departments: r?.created_departments ?? names.length,
        agents: r?.created_agents ?? 0,
        presetIndustry: r?.industry || d.industry,
      };
    } catch (e) {
      // apply 失败绝不阻塞安装：公司已经建好了，这里只记录原因
      return { status: "failed", departments: 0, agents: 0, reason: describeError(e, "组织架构生成失败") };
    }
  }

  async function handleInstall() {
    if (submitting) return;
    if (!step1Valid) {
      setAttempted1(true);
      setStep(1);
      return;
    }
    if (companyError) {
      setAttempted2(true);
      setStep(2);
      return;
    }
    setSubmitting(true);
    setStageIdx(0);
    setSubmitError("");
    setSubmitStatus(null);

    let res: SetupResult;
    try {
      res = await api.post<SetupResult>(
        "/setup",
        {
          username: username.trim(),
          email: email.trim(),
          password,
          company_name: companyName.trim(),
          description: description.trim() || undefined,
        },
        { silent: true, timeoutMs: 60_000 },
      );
    } catch (e) {
      if (!alive.current) return;
      setSubmitStatus(e instanceof ApiError ? e.status : null);
      setSubmitError(setupErrorMessage(e));
      setSubmitting(false);
      return;
    }

    // 安装成功：token 先落地，后续 apply 与"去工作台"都靠它
    if (!res?.access_token || !res?.company?.id) {
      if (!alive.current) return;
      setSubmitError("安装接口返回异常：没有拿到 token 或公司信息。请重试；若持续出现，请检查后端是否为当前版本。");
      setSubmitting(false);
      return;
    }
    setToken(res.access_token);
    if (res.refresh_token) setRefreshToken(res.refresh_token);
    setActiveCompanyId(res.company.id);

    // 账号与公司 + owner 成员在同一个事务里落库，界面上分两段呈现，
    // 让"建了什么"可见；真实失败只可能发生在 POST /setup 那一步（上面已处理）。
    advanceTo("company");
    await sleep(700);
    if (!alive.current) return;

    const hasOrgStage = creatingStages.some((s) => s.key === "org");
    if (hasOrgStage) advanceTo("org");
    const orgStart = Date.now();
    // apply 的任何异常都在这里被兜住，绝不让"公司已建好"这一步白费
    let org: OrgOutcome;
    try {
      org = await createOrg(res.company.id);
    } catch (e) {
      org = { status: "failed", departments: 0, agents: 0, reason: describeError(e, "组织架构生成失败") };
    }
    if (hasOrgStage) {
      await sleep(Math.max(0, 1100 - (Date.now() - orgStart)));
      if (!alive.current) return;
      // 组织架构没建成就不假装配好了 AI 员工，直接进入收尾
      if (org.status === "created") {
        advanceTo("agents");
        await sleep(900);
        if (!alive.current) return;
      }
    }
    advanceTo("finish");
    await sleep(600);
    if (!alive.current) return;
    // 不直接收尾：先落到第 4 步接模型。没有模型的系统进去后每个员工都"无法执行任务"。
    setInstalled({
      companyId: res.company.id,
      companyName: res.company?.name || companyName.trim(),
      username: res.user?.username || username.trim(),
      email: res.user?.email || email.trim(),
      org,
    });
    setModelStage(0);
    setModelError("");
    setModelOutcome(null);
    setSubmitting(false);
    setStep(4);
  }

  /**
   * 推倒重建：删掉这次安装建出来的账号、公司、部门与 AI 员工，回到第 1 步。
   *
   * 后端只允许"系统里只剩这一个超管"时整体重建（否则 409），并要求公司名
   * 一字不差——这里用输入框做二次确认，避免误点把刚建好的东西清掉。
   */
  async function handleRebuild(companyName: string) {
    if (rebuildBusy) return;
    setRebuildBusy(true);
    setRebuildError("");
    try {
      await api.post("/setup/rebuild", { company_name: companyName }, { silent: true, timeoutMs: 60_000 });
    } catch (e) {
      if (!alive.current) return;
      setRebuildError(describeError(e, "重建失败"));
      setRebuildBusy(false);
      return;
    }
    if (!alive.current) return;
    clearToken();
    localStorage.removeItem("aieos_active_cid");
    setDone(null);
    setRebuildOpen(false);
    setRebuildText("");
    setRebuildBusy(false);
    setStageIdx(0);
    setSubmitting(false);
    // 清空表单，让用户从第 1 步完整重走一遍
    setStep(1);
    setUsername("");
    setEmail("");
    setPassword("");
    setConfirm("");
    setShowPw(false);
    setTouched({ username: false, email: false, password: false, confirm: false });
    setAttempted1(false);
    setCompanyName("");
    setDescription("");
    setIndustry(INDUSTRIES[0].key);
    setAttempted2(false);
    setOrgMode("preset");
    setScale("smb");
    setPresetManual(null);
    setDetail(null);
    setPresetsError("");
    setDetailError("");
    setSubmitError("");
    setSubmitStatus(null);
    setInstalled(null);
    setModelPresetIdx(0);
    setModelForm({ ...MODEL_PRESETS[0] });
    setKeyInput("");
    setShowKey(false);
    setModelBusy(false);
    setModelStage(0);
    setModelError("");
    setModelOutcome(null);
    setModelCreatedId(null);
    setResetNotice("已清空这次安装（账号、公司与组织架构都已删除），可以从第 1 步重新创建。");
    void checkStatus();
  }

  /* ---------------- 渲染：状态探测中 ---------------- */
  if (gate === "checking") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-3 px-4">
        <span
          className="w-6 h-6 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
          aria-hidden
        />
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          正在确认系统是否已初始化…
        </p>
      </div>
    );
  }

  /* ---------------- 渲染：创建中（分阶段等待） ---------------- */
  if (submitting) {
    return (
      <Creating
        stages={creatingStages}
        index={stageIdx}
        companyName={companyName.trim() || "你的公司"}
        elapsed={elapsed}
      />
    );
  }

  /* ---------------- 渲染：完成页 ---------------- */
  if (done) {
    const org = done.org;
    const orgText =
      org.status === "created"
        ? `${org.departments} 个部门 / ${org.agents} 位 AI（${org.presetIndustry}）`
        : org.status === "skipped"
          ? `未生成 · ${org.note}`
          : "未生成 · 可进系统后再补";
    const m = done.model;
    const modelText =
      m?.status === "bound"
        ? `${m.modelName}（${m.modelIdOnProvider}）· 已绑定 ${m.bound}/${m.agentCount} 位员工${
            m.testOk ? "" : " · 连通测试未通过"
          }`
        : "未接入 · 进入系统后可在「模型配置」完成";
    return (
      <div className="min-h-screen flex items-center justify-center px-4 py-10">
        <div className="w-full max-w-[520px] text-center animate-fade-up">
          <div
            className="w-16 h-16 rounded-full mx-auto flex items-center justify-center text-2xl"
            style={{
              background: "rgba(52,211,153,0.14)",
              border: "1px solid rgba(52,211,153,0.4)",
              color: "var(--success)",
              boxShadow: "0 0 40px -10px rgba(52,211,153,0.5)",
            }}
            aria-hidden
          >
            ✓
          </div>
          <h1 className="mt-4 text-xl sm:text-2xl font-semibold">初始化完成</h1>
          <p className="mt-2 text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
            以后打开 AI-EOS 会直接进入登录页，不会再出现这个引导。
          </p>

          <div
            className="mt-6 rounded-[13px] px-4 py-3 text-left text-[12.5px]"
            style={{ border: "1px solid var(--border)", background: "rgba(0,0,0,0.28)" }}
          >
            <Row label="用户名" value={done.username} />
            <Row label="邮箱" value={done.email} />
            <Row label="公司" value={done.companyName} />
            <Row label="组织架构" value={orgText} />
            <Row label="大模型" value={modelText} />
          </div>

          {m?.status === "bound" && !m.testOk && (
            <div
              className="mt-4 text-left rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
              style={{
                border: "1px solid rgba(251,191,36,0.35)",
                background: "rgba(251,191,36,0.08)",
                color: "#fde68a",
              }}
              role="status"
            >
              <div className="font-medium" style={{ color: "var(--warning)" }}>
                模型已登记并绑定，但连通测试未通过
              </div>
              {m.testError && <p className="mt-1">原因：{m.testError}</p>}
              <p className="mt-1" style={{ color: "var(--muted)" }}>
                常见原因：Key 复制不完整、模型 ID 与服务商不符、需要代理网络。
                进入系统后打开「模型配置」调整并点「测试」，无需重装。
              </p>
            </div>
          )}

          {m?.status === "skipped" && (
            <div
              className="mt-4 text-left rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
              style={{
                border: "1px solid rgba(251,191,36,0.35)",
                background: "rgba(251,191,36,0.08)",
                color: "#fde68a",
              }}
              role="status"
            >
              <div className="font-medium" style={{ color: "var(--warning)" }}>
                还没有接入大模型
              </div>
              <p className="mt-1" style={{ color: "var(--muted)" }}>
                AI 员工接任务会提示「尚未绑定任何模型」。进入工作台后顶部会有提示条，
                点它即可完成接入。
              </p>
            </div>
          )}

          {org.status === "failed" && (
            <div
              className="mt-4 text-left rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
              style={{
                border: "1px solid rgba(251,191,36,0.35)",
                background: "rgba(251,191,36,0.08)",
                color: "#fde68a",
              }}
              role="status"
            >
              <div className="font-medium" style={{ color: "var(--warning)" }}>
                公司已创建，组织架构可进系统后再生成
              </div>
              <p className="mt-1">原因：{org.reason}</p>
              <p className="mt-1" style={{ color: "var(--muted)" }}>
                进入系统后打开「组织架构」→「行业预设」，即可一键引入部门与 AI 员工。
              </p>
            </div>
          )}

          <div className="mt-6 flex flex-col sm:flex-row gap-2.5 justify-center">
            <button
              type="button"
              className="btn-primary w-full sm:w-auto px-6 py-2.5"
              style={{ color: "inherit" }}
              onClick={() => router.replace("/")}
            >
              去工作台 →
            </button>
            <span style={{ color: "var(--muted)" }}>
              <button
                type="button"
                className="btn-ghost w-full sm:w-auto"
                style={{ color: "inherit" }}
                onClick={() => router.replace("/login")}
              >
                去登录页
              </button>
            </span>
          </div>
          <div className="mt-8 pt-5 text-left" style={{ borderTop: "1px solid var(--border)" }}>
            {!rebuildOpen ? (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span className="text-[11.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                  结果不满意？可以清空这次安装，回到第 1 步重新创建。
                </span>
                <button
                  type="button"
                  className="btn-ghost shrink-0"
                  style={{ color: "var(--danger)", borderColor: "rgba(248,113,113,0.35)" }}
                  onClick={() => {
                    setRebuildOpen(true);
                    setRebuildError("");
                  }}
                >
                  推倒重建
                </button>
              </div>
            ) : (
              <div
                role="alertdialog"
                aria-label="确认推倒重建"
                className="rounded-[12px] px-4 py-3.5"
                style={{ border: "1px solid rgba(248,113,113,0.35)", background: "rgba(248,113,113,0.07)" }}
              >
                <div className="text-[12.5px] font-medium" style={{ color: "var(--danger)" }}>
                  推倒重建会删除
                </div>
                <ul className="mt-1.5 space-y-0.5 text-[11.5px] leading-relaxed" style={{ color: "#f3b6b6" }}>
                  <li>· 管理员账号 {done.username} 与它的登录凭据</li>
                  <li>· 公司「{done.companyName}」及其全部数据</li>
                  <li>· 刚生成的组织架构：{orgText}</li>
                </ul>
                <p className="mt-2 text-[11.5px]" style={{ color: "var(--muted)" }}>
                  删除后无法恢复。输入公司名确认：
                </p>
                <input
                  className="input mt-2"
                  value={rebuildText}
                  onChange={(e) => setRebuildText(e.target.value)}
                  placeholder={done.companyName}
                  autoComplete="off"
                  disabled={rebuildBusy}
                  aria-label="输入公司名称以确认重建"
                />
                {rebuildError && (
                  <p className="mt-2 text-[11.5px]" style={{ color: "var(--danger)" }} role="alert">
                    {rebuildError}
                  </p>
                )}
                <div className="mt-3 flex gap-2 justify-end">
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={rebuildBusy}
                    onClick={() => {
                      setRebuildOpen(false);
                      setRebuildText("");
                      setRebuildError("");
                    }}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="btn-primary px-4 py-2 disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      background: "linear-gradient(135deg,#dc2626,#f87171)",
                      color: "#fff",
                      boxShadow: "0 2px 12px -2px rgba(248,113,113,0.45)",
                    }}
                    disabled={rebuildBusy || rebuildText.trim() !== done.companyName}
                    onClick={() => void handleRebuild(done.companyName)}
                  >
                    {rebuildBusy ? "正在清空…" : "确认重建"}
                  </button>
                </div>
              </div>
            )}
          </div>

          <p className="mt-4 text-[11px]" style={{ color: "var(--muted)" }}>
            所有数据都保存在你自己的机器上（SQLite）。
          </p>
        </div>
      </div>
    );
  }

  /* ---------------- 渲染：四步向导 ---------------- */
  const footHint =
    step === 1
      ? "这个账号将自动成为「超级管理员」。"
      : step === 2
        ? "行业只影响推荐的组织架构，不限制后续能力。"
        : step === 3
          ? orgCounts
            ? `将创建 ${orgCounts.departments} 个部门 / ${orgCounts.agents} 位 AI`
            : "预设数据取不到时，安装仍会继续。"
          : modelOutcome?.status === "bound"
            ? `已为 ${modelOutcome.bound}/${modelOutcome.agentCount} 位员工接上模型，点「完成，进入工作台」即可开始`
            : installedAgents > 0
              ? `接入后会把公司现有 ${installedAgents} 位 AI 员工一起绑定`
              : "暂时没有 AI 员工；接入后可随时在「模型配置」里绑定。";

  return (
    <div className="min-h-screen flex items-start md:items-center justify-center px-3 py-5 sm:px-5 sm:py-8">
      <div
        className="w-full max-w-[1120px] min-w-0 grid md:grid-cols-[288px_1fr] rounded-[20px] overflow-hidden"
        style={{
          border: "1px solid var(--border)",
          background: "linear-gradient(180deg, rgba(255,255,255,0.025), rgba(255,255,255,0.008))",
          boxShadow: "0 30px 80px -40px rgba(0,0,0,0.9)",
        }}
      >
        <Rail step={step} />

        <main className="flex flex-col min-w-0">
          {/* 移动端：左栏折叠成一条进度 */}
          <div className="md:hidden flex items-center gap-2.5 px-4 pt-4">
            <span className="text-[11px] shrink-0" style={{ color: "var(--muted)" }}>
              第 {step} / 4 步
            </span>
            <span className="flex-1 h-[3px] rounded-full" style={{ background: "var(--border)" }} aria-hidden>
              <span
                className="block h-full rounded-full transition-all"
                style={{ width: `${(step / 4) * 100}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }}
              />
            </span>
            <span className="text-[11px] shrink-0">{STEPS[step - 1].title}</span>
          </div>

          {resetNotice && (
            <div
              className="mx-4 mt-4 sm:mx-8 sm:mt-6 rounded-[11px] px-3.5 py-2.5 text-[11.5px] leading-relaxed"
              style={{
                border: "1px solid rgba(52,211,153,0.35)",
                background: "rgba(52,211,153,0.08)",
                color: "#a7f3d0",
              }}
              role="status"
            >
              {resetNotice}
            </div>
          )}

          {gate === "unknown" && (
            <div
              className="mx-4 mt-4 sm:mx-8 sm:mt-6 rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
              style={{
                border: "1px solid rgba(251,191,36,0.35)",
                background: "rgba(251,191,36,0.08)",
              }}
              role="alert"
            >
              <div className="font-medium" style={{ color: "var(--warning)" }}>
                无法确认系统是否已初始化
              </div>
              <p className="mt-1" style={{ color: "#e8d9a8" }}>
                {gateError}
              </p>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2">
                <span style={{ color: "var(--muted)" }}>
                  <button
                    type="button"
                    onClick={() => void checkStatus()}
                    className="underline underline-offset-2"
                    style={{ color: "inherit" }}
                  >
                    重试检测
                  </button>
                </span>
                <span style={{ color: "var(--muted)" }}>
                  <button
                    type="button"
                    onClick={() => router.replace("/login")}
                    className="underline underline-offset-2"
                    style={{ color: "inherit" }}
                  >
                    先去登录页
                  </button>
                </span>
              </div>
              <p className="mt-1.5" style={{ color: "var(--muted)" }}>
                你仍可继续填写；提交时若后端未就绪，会给出具体错误。
              </p>
            </div>
          )}

          <div className="px-4 pt-5 sm:px-8 sm:pt-7">
            <div className="text-[11px] tracking-[1.6px] uppercase" style={{ color: "var(--accent-2)" }}>
              Step {step} / 4
            </div>
            <h1 className="mt-2 text-xl sm:text-2xl font-semibold">{HEAD[step].title}</h1>
            <p className="mt-2 text-[12.5px] leading-relaxed max-w-[560px]" style={{ color: "var(--muted)" }}>
              {HEAD[step].desc}
            </p>
          </div>

          <div className="flex-1 px-4 py-5 sm:px-8 sm:py-6">
            {submitError && (
              <div
                className="mb-4 rounded-[10px] px-3 py-2.5 text-[11.5px] leading-relaxed"
                style={{
                  background: "rgba(248,113,113,0.12)",
                  border: "1px solid rgba(248,113,113,0.4)",
                  color: "#fca5a5",
                }}
                role="alert"
              >
                <div>{submitError}</div>
                <div className="mt-1.5 flex flex-wrap gap-x-4">
                  {submitStatus === 409 ? (
                    <span>
                      <button
                        type="button"
                        onClick={() => router.replace("/login")}
                        className="underline underline-offset-2"
                        style={{ color: "inherit" }}
                      >
                        去登录页 →
                      </button>
                    </span>
                  ) : (
                    <span>
                      <button
                        type="button"
                        onClick={() => void handleInstall()}
                        disabled={submitting}
                        className="underline underline-offset-2"
                        style={{ color: "inherit" }}
                      >
                        重试
                      </button>
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* ---------------- 步骤 1 ---------------- */}
            {step === 1 && (
              <div className="space-y-4">
                <div
                  className="rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
                  style={{
                    border: "1px solid rgba(99,102,241,0.28)",
                    background: "rgba(99,102,241,0.08)",
                    color: "#c9c9d6",
                  }}
                >
                  <span className="font-semibold" style={{ color: "var(--foreground)" }}>
                    密码无法找回
                  </span>
                  （后端只存加密哈希），请当场记好。
                </div>

                <div className="grid md:grid-cols-2 gap-x-4 gap-y-4">
                  <Field
                    label="用户名"
                    required
                    htmlFor="su-username"
                    error={visibleError("username")}
                    hint="至少 2 个字符，登录时也可以用它"
                  >
                    <input
                      id="su-username"
                      className="input"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      onBlur={() => setTouched((t) => ({ ...t, username: true }))}
                      placeholder="例如 boss"
                      autoComplete="username"
                      autoFocus
                      disabled={submitting}
                    />
                  </Field>

                  <Field
                    label="邮箱"
                    required
                    htmlFor="su-email"
                    error={visibleError("email")}
                    hint="用于登录，之后也能作为找回联系方式"
                  >
                    <input
                      id="su-email"
                      type="email"
                      className="input"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      onBlur={() => setTouched((t) => ({ ...t, email: true }))}
                      placeholder="例如 boss@example.com"
                      autoComplete="email"
                      disabled={submitting}
                    />
                  </Field>

                  <Field
                    label="密码"
                    required
                    htmlFor="su-password"
                    error={visibleError("password")}
                    hint="至少 8 位，需同时包含字母和数字"
                  >
                    <div className="relative">
                      <input
                        id="su-password"
                        type={showPw ? "text" : "password"}
                        className="input pr-14"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        onBlur={() => setTouched((t) => ({ ...t, password: true }))}
                        placeholder="至少 8 位，含字母和数字"
                        autoComplete="new-password"
                        disabled={submitting}
                      />
                      <span className="absolute right-2 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }}>
                        <button
                          type="button"
                          onClick={() => setShowPw((s) => !s)}
                          className="text-[11px] px-1.5 py-0.5 rounded"
                          style={{ color: "inherit" }}
                          tabIndex={-1}
                        >
                          {showPw ? "隐藏" : "显示"}
                        </button>
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 mt-2" aria-hidden>
                      {[1, 2, 3].map((i) => (
                        <span
                          key={i}
                          className="h-1 flex-1 rounded-full transition-all"
                          style={{
                            background:
                              i <= strength
                                ? strength === 1
                                  ? "var(--danger)"
                                  : strength === 2
                                    ? "var(--warning)"
                                    : "var(--success)"
                                : "var(--border)",
                          }}
                        />
                      ))}
                      <span className="text-[10px] ml-1 shrink-0" style={{ color: "var(--muted)" }}>
                        {strength === 0 ? "" : strength === 1 ? "弱" : strength === 2 ? "中" : "强"}
                      </span>
                    </div>
                  </Field>

                  <Field
                    label="确认密码"
                    required
                    htmlFor="su-confirm"
                    error={visibleError("confirm")}
                    hint="再输入一次，避免手误"
                  >
                    <input
                      id="su-confirm"
                      type={showPw ? "text" : "password"}
                      className="input"
                      value={confirm}
                      onChange={(e) => setConfirm(e.target.value)}
                      onBlur={() => setTouched((t) => ({ ...t, confirm: true }))}
                      placeholder="再输入一次"
                      autoComplete="new-password"
                      disabled={submitting}
                    />
                  </Field>
                </div>
              </div>
            )}

            {/* ---------------- 步骤 2 ---------------- */}
            {step === 2 && (
              <div className="space-y-5">
                <Field
                  label="公司名称"
                  required
                  htmlFor="su-company"
                  error={attempted2 || companyName ? companyError : ""}
                  hint="之后可以在「公司管理」里改名"
                >
                  <input
                    id="su-company"
                    className="input"
                    value={companyName}
                    onChange={(e) => setCompanyName(e.target.value)}
                    placeholder="例如：星河文化传媒"
                    maxLength={60}
                    autoFocus
                    disabled={submitting}
                  />
                </Field>

                <Field
                  label="一句话简介"
                  htmlFor="su-desc"
                  error={attempted2 && descError ? descError : ""}
                  hint="可选。写清业务，员工接任务时会更懂你的语境。"
                >
                  <textarea
                    id="su-desc"
                    className="input"
                    rows={3}
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="例如：做小红书与抖音内容代运营，主要客户是本地餐饮品牌。"
                    maxLength={200}
                    disabled={submitting}
                  />
                </Field>

                <div>
                  <div className="flex items-baseline flex-wrap gap-x-2 gap-y-1 mb-2">
                    <span className="text-xs" style={{ color: "var(--muted)" }}>
                      行业
                    </span>
                    <span className="text-[10.5px]" style={{ color: "#6d6d7c" }}>
                      决定推荐哪种部门蓝本，不限制后续能力
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {INDUSTRIES.map((ind) => (
                      <Chip
                        key={ind.key}
                        active={industry === ind.key}
                        onClick={() => chooseIndustry(ind.key)}
                      >
                        {ind.label}
                      </Chip>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* ---------------- 步骤 3 ---------------- */}
            {step === 3 && (
              <div className="space-y-4">
                <div>
                  <SectionLabel idx="A" title="组织起点" note="二选一" />
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                    <OptionCard
                      active={orgMode === "preset"}
                      onClick={() => setOrgMode("preset")}
                      title="按行业推荐"
                      desc={`用「${industryLabel(industry)}」的预设生成部门与 AI 员工`}
                      meta={presetIndustry ? `匹配到的预设：${presetIndustry}` : undefined}
                    />
                    <OptionCard
                      active={orgMode === "empty"}
                      onClick={() => setOrgMode("empty")}
                      title="空公司（0 部门 / 0 员工）"
                      desc="只建公司骨架，进入系统后再搭团队"
                    />
                  </div>
                </div>

                {orgMode === "preset" && (
                  <div>
                    <SectionLabel idx="B" title="编制规模" note="决定从预设里取几个部门" />
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
                      {SCALES.map((s) => (
                        <OptionCard
                          key={s.key}
                          active={scale === s.key}
                          onClick={() => setScale(s.key)}
                          title={s.name}
                          desc={s.desc}
                        />
                      ))}
                    </div>
                  </div>
                )}

                {orgMode === "preset" && (
                  <div>
                    <SectionLabel idx="C" title="行业预设" note="按行业自动匹配，可手动更换" />
                    <select
                      className="input"
                      value={presetId}
                      onChange={(e) => setPresetManual(e.target.value)}
                      disabled={submitting || !presets.length}
                      aria-label="组织预设"
                    >
                      {!presets.length && (
                        <option value={presetId}>
                          {listLoading ? "正在读取预设列表…" : `自动兜底预设 ${FALLBACK_PRESET_ID}（列表不可用）`}
                        </option>
                      )}
                      {presets.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.industry}（{p.department_count} 部门 / {p.agent_count} 员工）
                        </option>
                      ))}
                    </select>
                    {presetsError && (
                      <p className="mt-1.5 text-[10.5px] leading-relaxed" style={{ color: "var(--warning)" }}>
                        {presetsError}
                      </p>
                    )}
                  </div>
                )}

                <div>
                  <SectionLabel
                    idx={orgMode === "preset" ? "D" : "B"}
                    title="组织预览"
                    note="实时计算，不需要点生成"
                  />
                  <div
                    className="rounded-[14px] overflow-hidden"
                    style={{ border: "1px solid var(--border)", background: "rgba(0,0,0,0.26)" }}
                  >
                    <div
                      className="flex items-center gap-2.5 px-3.5 py-2.5"
                      style={{
                        borderBottom: "1px solid var(--border)",
                        background: "linear-gradient(90deg, rgba(99,102,241,0.13), rgba(99,102,241,0.02))",
                      }}
                    >
                      <span
                        className="w-7 h-7 rounded-[9px] flex items-center justify-center text-[11px] font-bold shrink-0"
                        style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)", color: "#fff" }}
                        aria-hidden
                      >
                        我
                      </span>
                      <span className="min-w-0">
                        <span className="block text-[12.5px] font-medium truncate">
                          {username.trim() || "你"} · 老板
                        </span>
                        <span className="block text-[10.5px]" style={{ color: "var(--muted)" }}>
                          定目标 · 验收 · 复盘
                        </span>
                      </span>
                      {orgCounts && (
                        <span
                          className="ml-auto shrink-0 text-[10px] rounded-full px-2 py-0.5"
                          style={{
                            color: "#e8e8ff",
                            background: "rgba(99,102,241,0.3)",
                            border: "1px solid rgba(99,102,241,0.5)",
                          }}
                        >
                          共 {orgCounts.agents + 1} 人
                        </span>
                      )}
                    </div>

                    <div
                      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3.5 py-2 text-[11px]"
                      style={{ color: "var(--muted)", borderBottom: "1px solid var(--border)" }}
                    >
                      {orgCounts ? (
                        <>
                          <span>
                            将创建{" "}
                            <b className="text-[15px] mx-0.5" style={{ color: "var(--foreground)" }}>
                              {orgCounts.departments}
                            </b>
                            个部门 /{" "}
                            <b className="text-[15px] mx-0.5" style={{ color: "var(--foreground)" }}>
                              {orgCounts.agents}
                            </b>
                            位 AI
                          </span>
                          {presetIndustry && orgMode === "preset" && (
                            <span className="truncate">预设：{presetIndustry}</span>
                          )}
                        </>
                      ) : (
                        <span>预览数据暂不可用（安装仍会继续）</span>
                      )}
                    </div>

                    <div className="px-3.5 py-3">
                      {!orgCounts ? (
                        <p className="text-[11.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                          {previewFallback}
                        </p>
                      ) : orgCounts.names.length ? (
                        <div className="flex flex-wrap gap-1.5">
                          {orgCounts.names.map((name) => (
                            <span
                              key={name}
                              className="rounded-md px-2 py-1 text-[11px]"
                              style={{ background: "rgba(255,255,255,0.05)", color: "#c6c6d2" }}
                            >
                              {name}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <p className="text-[11.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                          {orgMode === "empty"
                            ? "空公司：只建公司骨架，不生成部门与员工。进入系统后可在「组织架构」一键引入行业预设，或手动创建第一个员工。"
                            : scale === "solo"
                              ? "个体户：不创建部门。进入系统后仍可随时加部门、加员工。"
                              : "这个预设里没有可引入的部门，安装会照常完成；进入系统后可手动创建组织。"}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )}
            {/* ---------------- 步骤 4 ---------------- */}
            {step === 4 && (
              <div className="space-y-4">
                {!installed ? (
                  <p className="text-[12px] leading-relaxed" style={{ color: "var(--muted)" }}>
                    安装流程尚未完成，请先回到第 1 步创建账号与公司。
                  </p>
                ) : modelOutcome?.status === "bound" ? (
                  <div
                    className="rounded-[13px] px-4 py-4 text-[12.5px] leading-relaxed"
                    style={{
                      border: "1px solid rgba(52,211,153,0.4)",
                      background: "rgba(52,211,153,0.08)",
                    }}
                    role="status"
                  >
                    <div className="font-semibold" style={{ color: "var(--success)" }}>
                      ✅ 大模型已接入
                    </div>
                    <p className="mt-1.5" style={{ color: "#c9c9d6" }}>
                      {modelOutcome.modelName}（{modelOutcome.modelIdOnProvider}）
                      · 已绑定 {modelOutcome.bound}/{modelOutcome.agentCount} 位 AI 员工
                    </p>
                    <p className="mt-1 text-[11.5px]" style={{ color: "var(--muted)" }}>
                      {modelOutcome.testOk
                        ? "连通测试通过，员工现在就能接任务。"
                        : `连通测试未通过${modelOutcome.testError ? `：${modelOutcome.testError}` : ""}。模型与绑定已保存，可进入系统后在「模型配置」里调整。`}
                    </p>
                    <p className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
                      点右下角「完成，进入工作台」即可开始。
                    </p>
                  </div>
                ) : (
                  <>
                    <div
                      className="rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
                      style={{
                        border: "1px solid rgba(99,102,241,0.28)",
                        background: "rgba(99,102,241,0.08)",
                        color: "#c9c9d6",
                      }}
                    >
                      <span className="font-semibold" style={{ color: "var(--foreground)" }}>
                        公司「{installed.companyName}」已创建成功
                      </span>
                      ，最后一步是给它接上「大脑」。没有模型，员工接任务只会回复「尚未绑定任何模型」。
                      <br />
                      Key 只写入本机 <code>apps/api/.env</code>（权限 600），
                      <b>不上传、不入库、不回显</b>；保存后立即生效，无需重启。
                    </div>

                    <div>
                      <div className="flex items-baseline flex-wrap gap-x-2 gap-y-1 mb-2">
                        <span className="text-xs" style={{ color: "var(--muted)" }}>
                          服务商
                        </span>
                        <span className="text-[10.5px]" style={{ color: "#6d6d7c" }}>
                          填好地址和 Key 后点「读取模型」，从列表里选
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {MODEL_PRESETS.map((p, i) => (
                          <Chip
                            key={p.label}
                            active={modelPresetIdx === i}
                            onClick={() => {
                              if (modelBusy) return;
                              setModelPresetIdx(i);
                              setModelForm({ ...p });
                              setModelError("");
                              setModelFetchError("");
                              setModelOptions([]);
                              setNameAuto(true);
                              // 自定义接口多半不支持列模型，直接给手填入口
                              setManualModel(p.custom);
                            }}
                          >
                            {p.label}
                          </Chip>
                        ))}
                      </div>
                      {modelForm.key_hint && (
                        <p className="text-[11px] mt-2 leading-relaxed" style={{ color: "var(--muted)" }}>
                          🔑 Key 从哪来：{modelForm.key_hint}
                        </p>
                      )}
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-4">
                      <Field
                        label="接口地址"
                        required
                        htmlFor="su-model-base"
                        hint={modelForm.custom ? "填到 /v1 为止，例如 http://127.0.0.1:11434/v1" : undefined}
                      >
                        <input
                          id="su-model-base"
                          className="input"
                          value={modelForm.base_url}
                          onChange={(e) => {
                            setModelForm({ ...modelForm, base_url: e.target.value });
                            // 换了地址，之前读到的模型列表就不再可信
                            setModelOptions([]);
                            setModelFetchError("");
                          }}
                          placeholder="https://.../v1"
                          disabled={modelBusy}
                        />
                      </Field>
                      <Field
                        label="API Key"
                        required={!modelForm.custom}
                        htmlFor="su-model-key"
                        hint={
                          keyInput.trim()
                            ? `保存为本机变量 ${modelForm.api_key_env}`
                            : `本机无鉴权服务可留空；填写后保存为 ${modelForm.api_key_env}`
                        }
                      >
                        <div className="relative">
                          <input
                            id="su-model-key"
                            type={showKey ? "text" : "password"}
                            className="input pr-14"
                            value={keyInput}
                            onChange={(e) => setKeyInput(e.target.value)}
                            placeholder="粘贴服务商控制台里的 Key"
                            autoComplete="off"
                            spellCheck={false}
                            disabled={modelBusy}
                          />
                          <span className="absolute right-2 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }}>
                            <button
                              type="button"
                              onClick={() => setShowKey((s) => !s)}
                              className="text-[11px] px-1.5 py-0.5 rounded"
                              style={{ color: "inherit" }}
                              tabIndex={-1}
                            >
                              {showKey ? "隐藏" : "显示"}
                            </button>
                          </span>
                        </div>
                      </Field>

                      <Field label="模型" required htmlFor="su-model-id">
                        <div className="flex gap-2">
                          {modelOptions.length > 0 && !manualModel ? (
                            <select
                              id="su-model-id"
                              className="input flex-1 min-w-0"
                              value={modelForm.model_id}
                              onChange={(e) => {
                                const id = e.target.value;
                                setModelForm((f) => ({
                                  ...f,
                                  model_id: id,
                                  name: nameAuto ? id : f.name,
                                }));
                              }}
                              disabled={modelBusy}
                            >
                              <option value="" disabled>
                                选择一个模型
                              </option>
                              {modelOptions.map((id) => (
                                <option key={id} value={id}>
                                  {id}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <input
                              id="su-model-id"
                              className="input flex-1 min-w-0"
                              value={modelForm.model_id}
                              onChange={(e) => setModelForm({ ...modelForm, model_id: e.target.value })}
                              placeholder={manualModel ? "手动填写模型 ID" : "点右侧「读取模型」自动获取"}
                              disabled={modelBusy || !manualModel}
                            />
                          )}
                          <button
                            type="button"
                            onClick={() => void fetchModelOptions()}
                            disabled={modelBusy || modelFetchBusy || !modelForm.base_url.trim()}
                            className="btn-ghost shrink-0 px-3 disabled:opacity-40 disabled:cursor-not-allowed"
                            style={{ color: "inherit" }}
                          >
                            {modelFetchBusy ? "读取中…" : modelOptions.length ? "重新读取" : "读取模型"}
                          </button>
                        </div>
                        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px]">
                          {modelOptions.length > 0 && !manualModel ? (
                            <span style={{ color: "var(--success)" }}>
                              ✓ 已读取 {modelOptions.length} 个模型
                            </span>
                          ) : (
                            <span style={{ color: "var(--muted)" }}>
                              {manualModel ? "手动填写模式" : "先填接口地址，再点「读取模型」"}
                            </span>
                          )}
                          <button
                            type="button"
                            onClick={() => setManualModel((s) => !s)}
                            className="underline underline-offset-2"
                            style={{ color: "var(--muted)", cursor: "pointer" }}
                          >
                            {manualModel ? "改为从列表选择" : "服务商不支持读取？手动填写"}
                          </button>
                        </div>
                        {modelFetchError && (
                          <p className="text-[10.5px] mt-1 leading-relaxed" style={{ color: "var(--warning)" }}>
                            {modelFetchError}
                          </p>
                        )}
                      </Field>
                      <Field label="显示名称" htmlFor="su-model-name" hint="只是给你自己看的备注，留空则用模型 ID">
                        <input
                          id="su-model-name"
                          className="input"
                          value={modelForm.name}
                          onChange={(e) => {
                            setNameAuto(false);
                            setModelForm({ ...modelForm, name: e.target.value });
                          }}
                          placeholder={modelForm.model_id || "例如 公司主力模型"}
                          disabled={modelBusy}
                        />
                      </Field>
                    </div>

                    {modelStage > 0 && (
                      <ol className="space-y-1.5" aria-live="polite">
                        {[
                          { key: "model", label: "登记模型到本机" },
                          { key: "key", label: keyInput.trim() ? "保存密钥到 .env（600 权限）" : "本机服务：无需密钥" },
                          { key: "test", label: "测试与模型的连通性" },
                          { key: "bind", label: `绑定给 ${installedAgents} 位 AI 员工` },
                        ].map((s, i) => {
                          const state =
                            i + 1 < modelStage ? "done" : i + 1 === modelStage ? "active" : "todo";
                          return (
                            <li
                              key={s.key}
                              className="flex items-center gap-2.5 rounded-[10px] px-3 py-2 text-[11.5px]"
                              style={{
                                border: `1px solid ${state === "active" ? "rgba(99,102,241,0.45)" : "var(--border)"}`,
                                background: state === "active" ? "rgba(99,102,241,0.08)" : "rgba(0,0,0,0.22)",
                                opacity: state === "todo" ? 0.45 : 1,
                              }}
                            >
                              <span className="w-[15px] h-[15px] shrink-0 flex items-center justify-center" aria-hidden>
                                {state === "done" ? (
                                  <span style={{ color: "var(--success)" }}>✓</span>
                                ) : state === "active" ? (
                                  <span
                                    className="w-3 h-3 rounded-full border-2 animate-spin"
                                    style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
                                  />
                                ) : (
                                  <span className="w-[5px] h-[5px] rounded-full" style={{ background: "var(--muted)" }} />
                                )}
                              </span>
                              <span style={{ color: state === "active" ? "var(--foreground)" : undefined }}>
                                {s.label}
                              </span>
                            </li>
                          );
                        })}
                      </ol>
                    )}

                    {modelError && (
                      <div
                        className="rounded-[10px] px-3 py-2.5 text-[11.5px] leading-relaxed"
                        style={{
                          background: "rgba(248,113,113,0.12)",
                          border: "1px solid rgba(248,113,113,0.4)",
                          color: "#fca5a5",
                        }}
                        role="alert"
                      >
                        {modelError}
                        {modelCreatedId && (
                          <span style={{ color: "var(--muted)" }}>
                            {" "}（模型配置已登记，重试不会重复创建）
                          </span>
                        )}
                      </div>
                    )}

                    <button
                      type="button"
                      onClick={() => void handleConnectModel()}
                      disabled={modelBusy}
                      className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                      style={{ color: "inherit" }}
                    >
                      {modelBusy
                        ? "正在接入…"
                        : modelCreatedId
                          ? "重试接入 →"
                          : "接入并绑定全部员工 →"}
                    </button>
                  </>
                )}
              </div>
            )}
          </div>

          <div
            className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-4 py-4 sm:px-8 sm:py-5"
            style={{ borderTop: "1px solid var(--border)" }}
          >
            <div className="hidden sm:block text-[11.5px]" style={{ color: "var(--muted)" }}>
              {footHint}
            </div>
            <div className="flex flex-col sm:flex-row gap-2.5 w-full sm:w-auto">
              {step > 1 && step < 4 && (
                <span style={{ color: "var(--muted)" }}>
                  <button
                    type="button"
                    onClick={() => setStep((s) => (s > 1 ? ((s - 1) as Step) : s))}
                    disabled={submitting}
                    className="btn-ghost w-full sm:w-auto"
                    style={{ color: "inherit" }}
                  >
                    ← 上一步
                  </button>
                </span>
              )}

              {step < 3 ? (
                <button
                  type="button"
                  onClick={() => {
                    if (step === 1) {
                      setAttempted1(true);
                      if (step1Valid) setStep(2);
                      return;
                    }
                    setAttempted2(true);
                    if (!companyError) setStep(3);
                  }}
                  disabled={step === 1 ? !step1Valid : !!companyError}
                  className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "inherit" }}
                >
                  下一步 →
                </button>
              ) : step === 3 ? (
                <button
                  type="button"
                  onClick={() => void handleInstall()}
                  disabled={submitting}
                  className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "inherit" }}
                >
                  {submitting ? "正在安装…" : "创建并进入系统 →"}
                </button>
              ) : (
                <>
                  {modelOutcome?.status !== "bound" && (
                    <span style={{ color: "var(--muted)" }}>
                      <button
                        type="button"
                        onClick={() => {
                          const skipped: ModelOutcome = { status: "skipped" };
                          setModelOutcome(skipped);
                          finishSetup(skipped);
                        }}
                        disabled={modelBusy}
                        className="btn-ghost w-full sm:w-auto"
                        style={{ color: "inherit" }}
                      >
                        跳过，先进入 →
                      </button>
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => finishSetup()}
                    disabled={modelBusy || modelOutcome?.status !== "bound"}
                    className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{ color: "inherit" }}
                  >
                    完成，进入工作台 →
                  </button>
                </>
              )}
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

/* ---------------- 局部展示组件 ---------------- */

const CREATING_TIPS = [
  "AI 员工会继承所属部门的职责范围，岗位越清晰，干活越准。",
  "公司建好后，随时可以在「组织架构」里增删部门与岗位。",
  "不满意就推倒重建：清空这次安装即可回到第 1 步重来。",
  "所有数据都保存在你自己的机器上（SQLite），不会上传。",
];

/** 创建中的整屏等待态：把两次真实请求拆成可见的几步，逐段打勾 */
function Creating({
  stages,
  index,
  companyName,
  elapsed,
}: {
  stages: { key: string; title: string; hint: string }[];
  index: number;
  companyName: string;
  elapsed: number;
}) {
  const [tipIdx, setTipIdx] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTipIdx((i) => (i + 1) % CREATING_TIPS.length), 3000);
    return () => window.clearInterval(t);
  }, []);

  const pct = Math.min(100, Math.round(((index + 0.4) / stages.length) * 100));

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-[480px] animate-fade-up">
        <div className="flex flex-col items-center text-center">
          <div className="relative w-[88px] h-[88px]" aria-hidden>
            <div
              className="absolute inset-0 rounded-full animate-spin"
              style={{
                background:
                  "conic-gradient(from 0deg, rgba(99,102,241,0) 0deg, #6366f1 120deg, #22d3ee 210deg, rgba(34,211,238,0) 330deg)",
                animationDuration: "2.2s",
              }}
            />
            <div
              className="absolute inset-[4px] rounded-full flex items-center justify-center overflow-hidden"
              style={{ background: "var(--background)", border: "1px solid var(--border)" }}
            >
              <div
                className="absolute inset-[8px] rounded-full pulse-dot"
                style={{
                  background:
                    "radial-gradient(circle at 32% 28%, rgba(99,102,241,0.55), rgba(34,211,238,0.12) 55%, transparent 75%)",
                }}
              />
              <span className="relative text-[14px] font-bold text-gradient tracking-[1px]">AI</span>
            </div>
          </div>

          <h1 className="mt-6 text-lg sm:text-xl font-semibold">
            正在为你搭建「{companyName}」
          </h1>
          <p className="mt-2 text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
            账号、公司、部门与 AI 员工正在依次就位，通常十来秒就能完成。
          </p>
        </div>

        <div className="mt-6" aria-hidden>
          <div className="h-[4px] rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
            <div
              className="h-full rounded-full transition-all duration-700"
              style={{ width: `${pct}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }}
            />
          </div>
          <div className="mt-1.5 flex justify-between text-[10.5px]" style={{ color: "var(--muted)" }}>
            <span>
              第 {Math.min(index + 1, stages.length)} / {stages.length} 步
            </span>
            <span>已用 {elapsed} 秒</span>
          </div>
        </div>

        <ol className="mt-4 space-y-2" aria-live="polite">
          {stages.map((s, i) => {
            const state = i < index ? "done" : i === index ? "active" : "todo";
            return (
              <li
                key={s.key}
                className="flex gap-3 rounded-[11px] px-3.5 py-2.5 transition-colors"
                style={{
                  border: `1px solid ${state === "active" ? "rgba(99,102,241,0.45)" : "var(--border)"}`,
                  background: state === "active" ? "rgba(99,102,241,0.08)" : "rgba(0,0,0,0.22)",
                  opacity: state === "todo" ? 0.5 : 1,
                }}
              >
                <span
                  className="mt-[2px] w-[16px] h-[16px] shrink-0 rounded-full flex items-center justify-center text-[9px] font-bold"
                  aria-hidden
                >
                  {state === "done" ? (
                    <span
                      className="w-full h-full rounded-full flex items-center justify-center"
                      style={{ background: "rgba(52,211,153,0.16)", color: "var(--success)" }}
                    >
                      ✓
                    </span>
                  ) : state === "active" ? (
                    <span
                      className="w-3 h-3 rounded-full border-2 animate-spin"
                      style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
                    />
                  ) : (
                    <span className="w-[6px] h-[6px] rounded-full" style={{ background: "var(--muted)" }} />
                  )}
                </span>
                <span className="min-w-0">
                  <span
                    className="block text-[12.5px] font-medium"
                    style={{
                      color:
                        state === "active" ? "var(--foreground)" : state === "done" ? "#c9c9d6" : "var(--muted)",
                    }}
                  >
                    {s.title}
                  </span>
                  {state !== "todo" && (
                    <span
                      className="block text-[11px] mt-0.5 leading-relaxed"
                      style={{ color: state === "active" ? "var(--muted)" : "#6f7280" }}
                    >
                      {s.hint}
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ol>

        <p
          className="mt-5 text-center text-[11.5px] leading-relaxed transition-opacity"
          style={{ color: "var(--muted)" }}
          aria-live="polite"
        >
          {CREATING_TIPS[tipIdx]}
        </p>
        <p className="mt-1.5 text-center text-[10.5px]" style={{ color: "#6f7280" }}>
          请不要关闭或刷新这个页面
        </p>
      </div>
    </div>
  );
}

function Rail({ step }: { step: Step }) {
  return (
    <aside
      className="hidden md:flex flex-col gap-6 p-7 min-w-0"
      style={{
        borderRight: "1px solid var(--border)",
        background:
          "linear-gradient(160deg, rgba(99,102,241,0.14), rgba(34,211,238,0.045) 60%, transparent)",
      }}
    >
      <div className="flex items-center gap-2.5">
        <span
          className="w-9 h-9 rounded-[11px] flex items-center justify-center text-[13px] font-bold shrink-0"
          style={{
            background: "linear-gradient(135deg,#6366f1,#8b5cf6)",
            color: "#fff",
            boxShadow: "0 6px 20px -6px rgba(99,102,241,0.8)",
          }}
          aria-hidden
        >
          AI
        </span>
        <span>
          <span className="block text-[15px] font-semibold leading-tight">AI-EOS</span>
          <span className="block text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
            首次使用 · 初始化
          </span>
        </span>
      </div>

      <ol className="space-y-0.5">
        {STEPS.map((s) => {
          const current = s.n === step;
          const finished = s.n < step;
          return (
            <li
              key={s.n}
              className="flex gap-3 rounded-[12px] px-2.5 py-3"
              style={{ background: current ? "rgba(99,102,241,0.10)" : "transparent" }}
            >
              <span
                className="shrink-0 w-[26px] h-[26px] rounded-full flex items-center justify-center text-[11px] font-bold"
                style={
                  finished
                    ? {
                        background: "rgba(52,211,153,0.14)",
                        border: "1px solid rgba(52,211,153,0.5)",
                        color: "var(--success)",
                      }
                    : current
                      ? {
                          background: "linear-gradient(135deg,#6366f1,#8b5cf6)",
                          color: "#fff",
                          boxShadow: "0 3px 14px -3px rgba(99,102,241,0.9)",
                        }
                      : {
                          border: "1px solid var(--border)",
                          background: "rgba(255,255,255,0.02)",
                          color: "var(--muted)",
                        }
                }
                aria-hidden
              >
                {finished ? "✓" : s.n}
              </span>
              <span className="min-w-0">
                <span
                  className="block text-[13px] font-semibold"
                  style={{ color: current ? "var(--foreground)" : finished ? "#b9b9c6" : "var(--muted)" }}
                >
                  {s.title}
                </span>
                <span className="block text-[11px] mt-0.5" style={{ color: "#5c5c68" }}>
                  {s.sub}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      <div className="mt-auto text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
        <div
          className="inline-flex items-center rounded-full px-2.5 py-1 mb-2.5 text-[10px]"
          style={{
            border: "1px solid rgba(251,191,36,0.35)",
            background: "rgba(251,191,36,0.08)",
            color: "var(--warning)",
          }}
        >
          约 1 分钟
        </div>
        <p>完成后系统即进入正常状态：其他人通过登录 / 注册进入，不会再看到这个引导。</p>
      </div>
    </aside>
  );
}

function SectionLabel({ idx, title, note }: { idx: string; title: string; note?: string }) {
  return (
    <div className="flex items-baseline flex-wrap gap-x-2 gap-y-1 mb-2">
      <span
        className="text-[9.5px] font-bold tracking-[1px] rounded-[5px] px-1.5"
        style={{
          color: "var(--accent-2)",
          border: "1px solid rgba(34,211,238,0.28)",
          background: "rgba(34,211,238,0.07)",
        }}
      >
        {idx}
      </span>
      <span className="text-[12px] font-semibold">{title}</span>
      {note && (
        <span className="text-[10.5px] hidden sm:inline" style={{ color: "var(--muted)" }}>
          {note}
        </span>
      )}
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  // 外层 span 负责文字颜色，按钮自身固定 color: inherit（避免 UA 样式把文字压成黑色）
  return (
    <span className="inline-flex" style={{ color: active ? "#fff" : "var(--muted)" }}>
      <button
        type="button"
        onClick={onClick}
        aria-pressed={active}
        className="whitespace-nowrap rounded-full px-3 py-1.5 text-[12px] transition-all"
        style={{
          color: "inherit",
          border: `1px solid ${active ? "transparent" : "var(--border)"}`,
          background: active
            ? "linear-gradient(135deg, rgba(99,102,241,0.92), rgba(139,92,246,0.92))"
            : "rgba(255,255,255,0.02)",
          fontWeight: active ? 600 : 400,
          cursor: "pointer",
        }}
      >
        {children}
      </button>
    </span>
  );
}

function OptionCard({
  active,
  onClick,
  title,
  desc,
  meta,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  desc: string;
  meta?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className="relative w-full text-left rounded-[12px] px-3.5 py-3 transition-all"
      style={{
        color: "inherit",
        border: `1px solid ${active ? "rgba(99,102,241,0.85)" : "var(--border)"}`,
        background: active
          ? "linear-gradient(180deg, rgba(99,102,241,0.16), rgba(99,102,241,0.04))"
          : "rgba(255,255,255,0.022)",
        cursor: "pointer",
      }}
    >
      <span className="block text-[12.5px] font-semibold pr-5" style={{ color: active ? "var(--foreground)" : "#c9c9d6" }}>
        {title}
      </span>
      <span className="block text-[10.5px] leading-relaxed mt-1.5" style={{ color: "var(--muted)" }}>
        {desc}
      </span>
      {meta && (
        <span className="block text-[10px] mt-1 truncate" style={{ color: "#7d7d8d" }}>
          {meta}
        </span>
      )}
      {active && (
        <span
          className="absolute top-2.5 right-2.5 w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold"
          style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)", color: "#fff" }}
          aria-hidden
        >
          ✓
        </span>
      )}
    </button>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3 py-1">
      <span className="shrink-0" style={{ color: "var(--muted)" }}>
        {label}
      </span>
      <span
        className="font-medium text-right break-all"
        style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}
      >
        {value}
      </span>
    </div>
  );
}
