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
//   ② 你的公司：名称 / 简介 / 行业（12 个 chip，只影响组织架构推荐）——这一步结束时真正安装
//   ③ 接入大模型：登记模型 + 填 Key + 测试连通（可跳过）
//   ④ 资料与组织：按行业预设 / 空公司搭骨架，或让 AI 读知识库资料生成；生成后把 ③ 的模型绑给全部员工
//
// 为什么模型排在组织前面：老顺序是"先搭人、后接大脑"，用户在第 3 步对着还不认识的员工选骨架，
// 到第 4 步才知道他们长什么样。现在先接模型、最后建人——绑模型这一步顺势挪到第 4 步
// （bind-agents 依赖员工已经存在，顺序反了就只能绑出 0 个人）。
//
// 第 3、4 步发生在安装成功之后（此时已有 token）：
// 没有模型，员工接任务只会回「尚未绑定任何模型」——这是过去新装系统的默认结局。
// Key 只写进本机 apps/api/.env（后端原子写入、600 权限、立即生效），
// 不入库、不上传、不回显。
//
// ## 关键约束
// - 预设 id 不写死：GET /org-presets 列表 + 行业字段模糊匹配，匹配不到才用 `tech`；
//   部门名一律取自 GET /org-presets/{pid}，规模只决定"取前几个"。
// - 生成组织失败**不阻塞收尾**：公司已经建好了，组织架构漏了也能进系统后在「组织架构」里补。
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
  getToken,
  setActiveCompanyId,
  setRefreshToken,
  setToken,
  type RequestOptions,
} from "@/lib/api";
import { V, runRules } from "@/lib/validate";
import Field from "@/components/Field";
import DemoMode from "@/components/DemoMode";
import OrgSynthesisDrawer from "@/components/OrgSynthesisDrawer";
import {
  FALLBACK_PRESET_ID,
  INDUSTRIES,
  SCALES,
  clearPendingInstall,
  clearSetupDraft,
  departmentSubset,
  industryLabel,
  loadPendingInstall,
  loadSetupDraft,
  matchPresetId,
  modelBindingSummary,
  passwordStrength,
  presetErrorMessage,
  presetPool,
  previewCounts,
  savePendingInstall,
  saveSetupDraft,
  setupErrorMessage,
  setupStatusErrorMessage,
  type ApplyResult,
  type OrgScale,
  type PresetDetail,
  type PresetSummary,
  type SetupDraft,
  type SetupInstalledDraft,
  type SetupOrgOutcome,
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

/** 组织架构结果：类型定义在 lib/setup.ts（草稿/安装标记要复用它） */
type OrgOutcome = SetupOrgOutcome;

/** 第 3 步的结果：接入成功（含测试是否通过）或用户主动跳过 */
type ModelOutcome =
  | { status: "bound"; modelId: string; modelName: string; modelIdOnProvider: string;
      bound: number; alreadyBound: number; agentCount: number; testOk: boolean; testError?: string }
  | { status: "skipped" };

type DoneState = {
  username: string;
  email: string;
  companyName: string;
  org: OrgOutcome;
  model: ModelOutcome | null;
};

/** 安装成功的上下文：就是「待续装」标记的内容（第 3 步接模型、第 4 步建组织都靠它） */
type Installed = SetupInstalledDraft;

const STEPS: { n: Step; title: string; sub: string }[] = [
  { n: 1, title: "管理员账号", sub: "唯一的管理者" },
  { n: 2, title: "你的公司", sub: "名称与业务方向" },
  { n: 3, title: "接入大模型", sub: "让员工能思考" },
  { n: 4, title: "资料与组织", sub: "预设 · AI 生成" },
];

/**
 * 写进「待续装」标记里的来源标记：区分预设生成与 AI 生成。
 * 刷新后靠它判断"AI 那一路已经落过地"，避免收尾时又叠一套预设部门。
 */
const AI_ORG_MARK = "AI 按资料生成";

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
    desc: "公司是员工、项目、任务、成果、知识库的归属单元。填完这一步就会真正开始创建（账号 + 公司），之后还能创建更多公司并在工作台随时切换。",
  },
  3: {
    title: "给 AI 员工接上大脑",
    desc: "员工的思考来自大模型服务。选一个服务商、粘贴 API Key，系统会自动登记模型、保存密钥并测试连通；员工还没建，绑定放到第 4 步组织生成之后一起做。",
  },
  4: {
    title: "搭建你的组织",
    desc: "按行业预设配齐部门与 AI 员工，也可以 0 部门空公司进入、之后再搭。资料多一点的老板可以让 AI 读完知识库再设计——岗位更贴合你实际在做的业务。",
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
        // 系统已初始化：通常是用户装完刷新了页面。
        // 若「已安装、待续装」标记与 token 都还在，说明第 3/4 步还没收尾——
        // 此时踢回 /login 会丢掉上下文（用户还得自己再找一遍入口），
        // 所以恢复安装上下文，原地停在没做完的那一步。
        const pending = loadPendingInstall();
        if (pending && getToken()) {
          setInstalled(pending);
          if (pending.model) {
            // 模型已经登记过：恢复成"已接入"，续到第 4 步建组织，不会再创建第二条配置
            const m = pending.model;
            setModelCreatedId(m.modelId);
            setModelOutcome({
              status: "bound",
              modelId: m.modelId,
              modelName: m.modelName,
              modelIdOnProvider: m.modelIdOnProvider,
              bound: 0,
              alreadyBound: 0,
              agentCount: 0,
              testOk: m.testOk,
              testError: m.testError,
            });
            setStep(4);
          } else {
            setStep(3);
          }
          if (pending.org.status === "created" && pending.org.presetIndustry === AI_ORG_MARK) {
            // AI 那一路已经落过地：停在第 4 步只做收尾，再点一次不会又叠一套预设部门
            setAiOrgResult({
              centers: 0,
              departments: pending.org.departments,
              agents: pending.org.agents,
            });
            setStep(4);
          }
          setGate("ready");
          return;
        }
        clearPendingInstall();
        clearSetupDraft();
        router.replace("/login");
        return;
      }
      // 仍然是未初始化状态：残留的旧标记只会让后续判断混乱，清掉
      clearPendingInstall();
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
  // 完成页的结论要以服务端的模型体检为准：向导里那次测试可能已经过期，
  // 用户也可能在别处修好了 Key——不能拿旧结果一直挂着"未通过"。
  const [serverModelReady, setServerModelReady] = useState<boolean | null>(null);

  useEffect(() => {
    if (!done || done.model?.status !== "bound") return;
    let running = true;
    (async () => {
      try {
        const d = await api.get<{ ready?: boolean }>("/models/diagnostics", { silent: true });
        if (running && typeof d?.ready === "boolean") setServerModelReady(d.ready);
      } catch {
        // 体检接口不可用就沿用向导结果，不额外打扰用户
      }
    })();
    return () => { running = false; };
  }, [done]);

  // 推倒重建：完成页上的二次确认
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [rebuildText, setRebuildText] = useState("");
  const [rebuildBusy, setRebuildBusy] = useState(false);
  const [rebuildError, setRebuildError] = useState("");
  const [resetNotice, setResetNotice] = useState("");

  /* ---------------- 第 3 步：接入大模型（发生在安装成功之后） ---------------- */
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
  // 0=未开始，1..3=正在做第几件事（登记→存密钥→测连通），4=全部完成
  const [modelStage, setModelStage] = useState(0);
  const [modelError, setModelError] = useState("");
  const [modelOutcome, setModelOutcome] = useState<ModelOutcome | null>(null);
  // 已经登记过的模型 id：失败重试时不再重复创建，避免一次失败留下两条配置
  const [modelCreatedId, setModelCreatedId] = useState<string | null>(null);
  // 没接模型就想进第 4 步：先问一句，别让人带着"员工干不了活"的系统离开向导
  const [skipModelAsking, setSkipModelAsking] = useState(false);

  /* ---------------- 第 4 步：资料与组织（生成骨架 + 绑定模型） ---------------- */
  const [orgBusy, setOrgBusy] = useState(false);
  const [orgError, setOrgError] = useState("");
  const [orgStage, setOrgStage] = useState(0); // 1=生成部门与员工，2=绑定模型
  // 「AI 读资料生成组织」抽屉（复用 components/OrgSynthesisDrawer.tsx）
  const [aiOrgOpen, setAiOrgOpen] = useState(false);
  // 抽屉里已经落地的组织：主按钮这时不再跑预设，只做"绑模型 + 收尾"
  const [aiOrgResult, setAiOrgResult] = useState<{ centers: number; departments: number; agents: number } | null>(null);

  /* ---------------- 可续装：草稿恢复 / 保存（只存非敏感字段） ---------------- */
  // 演示模式弹层（复用 components/DemoMode.tsx，不另写一套演示逻辑）
  const [demoOpen, setDemoOpen] = useState(false);
  // 「去修复 Key」：失败态点一下要回到表单并聚焦 Key 输入
  const keyInputRef = useRef<HTMLInputElement>(null);
  const [repairKeyTick, setRepairKeyTick] = useState(0);
  // 草稿恢复完成前不要回写，否则会用初始空值把已保存的草稿覆盖掉
  const draftHydrated = useRef(false);

  useEffect(() => {
    const d = loadSetupDraft();
    if (d) {
      setUsername(d.username);
      setEmail(d.email);
      setCompanyName(d.companyName);
      setDescription(d.description);
      setIndustry(d.industry);
      setOrgMode(d.orgMode);
      setScale(d.scale);
      setPresetManual(d.presetManual);
      // 第 3、4 步不能只凭草稿：必须同时有「待续装」标记与登录态，由 checkStatus 决定
      if (d.step <= 2) setStep(d.step);
      if (d.model) {
        const m = d.model;
        const idx = MODEL_PRESETS.findIndex(
          (p) => p.provider_type === m.providerType && p.base_url === m.baseUrl,
        );
        if (idx >= 0) setModelPresetIdx(idx);
        else setManualModel(true); // 恢复出来的地址不属于任何预置服务商 → 允许手填/改地址
        setModelForm((f) => ({
          ...f,
          provider_type: m.providerType || f.provider_type,
          name: m.name,
          base_url: m.baseUrl,
          model_id: m.modelId,
        }));
        setNameAuto(false);
      }
    }
    draftHydrated.current = true;
  }, []);

  const draftSnapshot = useMemo<SetupDraft>(
    () => ({
      step,
      username,
      email,
      companyName,
      description,
      industry,
      orgMode,
      scale,
      presetManual,
      model: {
        providerType: modelForm.provider_type,
        name: modelForm.name,
        baseUrl: modelForm.base_url,
        modelId: modelForm.model_id,
      },
    }),
    [
      step,
      username,
      email,
      companyName,
      description,
      industry,
      orgMode,
      scale,
      presetManual,
      modelForm.provider_type,
      modelForm.name,
      modelForm.base_url,
      modelForm.model_id,
    ],
  );

  // 轻量 debounce：连续敲键盘不会每个按键都写一次 sessionStorage。
  // 状态探测没通过（gate !== "ready"）就不写，避免"即将被踢回登录页"时留下残稿。
  useEffect(() => {
    if (!draftHydrated.current || gate !== "ready") return;
    const t = window.setTimeout(() => saveSetupDraft(draftSnapshot), 400);
    return () => window.clearTimeout(t);
  }, [draftSnapshot, gate]);

  // 失败态「去修复 Key」：表单重新挂载后滚动 + 聚焦 Key 输入框
  useEffect(() => {
    if (!repairKeyTick) return;
    const el = keyInputRef.current;
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.focus();
  }, [repairKeyTick]);

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
   * 第 3 步：登记模型 → 保存密钥 → 测试连通。
   *
   * 这一步**不绑员工**：员工要到第 4 步才存在（bind-agents 找不到人只会绑出 0）。
   * 绑定由第 4 步生成组织后统一做一次，运行时只认 agent_model_configs，
   * 缺了它员工仍会报「尚未绑定任何模型」。连通测试失败不阻塞——Key 可能有效
   * 但该模型暂时不可用，先记下、进系统后换模型即可；结果里如实标出测试失败。
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
      const outcome: ModelOutcome = {
        status: "bound",
        modelId: mid,
        modelName: name,
        modelIdOnProvider: modelId,
        bound: 0,
        alreadyBound: 0,
        agentCount: 0,
        testOk,
        testError: testError || undefined,
      };
      setModelOutcome(outcome);
      // 立刻写回「待续装」标记：刷新后停在第 4 步，而不是把同一个模型再登记一遍
      updateInstalled({ model: {
        modelId: mid,
        modelName: name,
        modelIdOnProvider: modelId,
        testOk,
        testError: testError || undefined,
      } });
      setKeyInput("");
    } catch (e) {
      setModelError(describeError(e, "接入大模型失败"));
    } finally {
      setModelBusy(false);
    }
  }

  /**
   * 更新安装上下文并同步「待续装」标记。
   *
   * 返回值是给调用方当场用的：setState 是异步的，紧接着的 finishSetup
   * 若继续读 `installed` 会拿到旧值（组织架构/模型就白写了）。
   */
  function updateInstalled(patch: Partial<Installed>): Installed | null {
    if (!installed) return null;
    const next: Installed = { ...installed, ...patch };
    setInstalled(next);
    savePendingInstall(next);
    return next;
  }

  function finishSetup(model: ModelOutcome | null = modelOutcome, info: Installed | null = installed) {
    if (!info) return;
    // 收尾后不再"待接模型"：草稿与标记都清掉，刷新/再打开只会走正常登录
    clearSetupDraft();
    clearPendingInstall();
    setDone({
      username: info.username,
      email: info.email,
      companyName: info.companyName,
      org: info.org,
      model,
    });
  }

  /** 失败态「去修复 Key」：收起结果面板，回到模型表单并聚焦 Key 输入 */
  function goFixKey() {
    setModelOutcome(null);
    setModelError("");
    setModelStage(0); // 进度清单归零，重新开始才能看到真实的每一步
    setRepairKeyTick((n) => n + 1);
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
      listLoaded.current = false; // 下次进入第 4 步再试一次
    } finally {
      if (alive.current) setListLoading(false);
    }
  }, []);

  useEffect(() => {
    if (step !== 4 || listLoaded.current) return;
    void loadPresets();
  }, [step, loadPresets]);

  const autoPresetId = useMemo(
    () => (presets.length ? matchPresetId(presets, industry) : FALLBACK_PRESET_ID),
    [presets, industry],
  );
  const presetId = presetManual ?? autoPresetId;

  useEffect(() => {
    if (step !== 4 || orgMode !== "preset" || !presetId) return;
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
   * 后端把"账号 + 公司 + owner 成员关系"放在同一个事务里，所以这里只有这两段
   * 是真的（POST /setup 一次写完），最后一段是给"马上带你进第 3 步"一个落脚点。
   * 部门与 AI 员工已经挪到第 4 步，不再混在安装过程里。
   */
  const creatingStages = useMemo(
    () => [
      { key: "account", title: "创建管理员账号", hint: "写入超级管理员，建立登录身份" },
      { key: "company", title: "建立公司档案", hint: `为「${companyName.trim() || "你的公司"}」准备数据空间` },
      { key: "finish", title: "准备就绪", hint: "接着接大脑（第 3 步）、搭组织（第 4 步）" },
    ],
    [companyName],
  );

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
   * 第 4 步已经会自己拉一次预设列表，但用户可能在列表还没读回来时就点了完成，
   * 这时 presetId 只是兜底值；这里再按行业重新匹配一次——
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
          : await api.get<PresetDetail>(`/org-presets/${encodeURIComponent(pid)}`, PRESET_OPTS);
      const names = departmentSubset(d, scale);
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

    // 账号与公司已经落库：立刻写「已安装、待续装」标记。
    // 第 3 步登记模型、第 4 步生成组织都要靠这个上下文；中途刷新也能停在原步骤，
    // 而不是被 needs_setup=false 踢回登录页。组织架构此时还不存在，先记"未生成"。
    const installedInfo: Installed = {
      companyId: res.company.id,
      companyName: res.company?.name || companyName.trim(),
      username: res.user?.username || username.trim(),
      email: res.user?.email || email.trim(),
      org: { status: "skipped", departments: 0, agents: 0, note: "组织架构尚未生成" },
      model: null,
    };

    // 账号与公司 + owner 成员在同一个事务里落库，界面上分两段呈现，
    // 让"建了什么"可见；真实失败只可能发生在 POST /setup 那一步（上面已处理）。
    advanceTo("company");
    await sleep(700);
    if (!alive.current) return;
    advanceTo("finish");
    await sleep(600);
    if (!alive.current) return;

    setInstalled(installedInfo);
    savePendingInstall(installedInfo);
    setModelStage(0);
    setModelError("");
    setModelOutcome(null);
    setSkipModelAsking(false);
    setSubmitting(false);
    // 不直接收尾：先落到第 3 步接模型。没有模型的系统进去后每个员工都"无法执行任务"。
    setStep(3);
  }

  /**
   * 把第 3 步接好的模型绑给刚生成的员工。
   *
   * 顺序不能反：运行时只认 agent_model_configs，而 bind-agents 是按公司找人的，
   * 员工还不存在时绑出 0 个，进系统后每个任务都会报「尚未绑定任何模型」。
   */
  async function bindModelToAgents(model: Extract<ModelOutcome, { status: "bound" }>) {
    if (!installed) return model;
    const b = await api.post<{ bound: number; skipped: number; agent_count: number }>(
      `/models/${model.modelId}/bind-agents`,
      { company_id: installed.companyId },
      { silent: true, timeoutMs: 60_000 },
    );
    return {
      ...model,
      bound: b?.bound ?? 0,
      alreadyBound: b?.skipped ?? 0,
      agentCount: b?.agent_count ?? 0,
    };
  }

  /**
   * 第 4 步主按钮：生成组织 →（有模型就）绑员工 → 收尾。
   *
   * 组织失败不阻塞收尾：公司已建好，带着 0 部门进系统比卡在向导里强，
   * 完成页会如实写明并指路「组织架构」页。绑定失败同理。
   */
  async function handleApplyOrg() {
    if (!installed || orgBusy || submitting) return;
    setOrgBusy(true);
    setOrgError("");
    setOrgStage(1);

    let org: OrgOutcome;
    try {
      org = aiOrgResult
        ? { status: "created", departments: aiOrgResult.departments, agents: aiOrgResult.agents, presetIndustry: AI_ORG_MARK }
        : await createOrg(installed.companyId);
    } catch (e) {
      org = { status: "failed", departments: 0, agents: 0, reason: describeError(e, "组织架构生成失败") };
    }
    if (!alive.current) return;

    let model = modelOutcome;
    if (model?.status === "bound" && org.status === "created" && org.agents > 0) {
      setOrgStage(2);
      const bound = await bindModelToAgents(model).catch((e) => {
        setOrgError(
          `部门与员工已生成，但模型自动绑定失败：${describeError(e, "接口没有返回原因")}。进系统后在「模型配置」里点「绑定给全部员工」即可。`,
        );
        return null;
      });
      if (bound && alive.current) {
        model = bound;
        setModelOutcome(bound);
      }
    }
    if (!alive.current) return;

    await sleep(300);
    if (!alive.current) return;
    setOrgStage(0);
    setOrgBusy(false);
    finishSetup(model, updateInstalled({ org }));
  }

  /** 第 4 步的退路：不建组织也让人走得掉，避免向导变成死胡同 */
  function handleSkipOrg() {
    if (!installed || orgBusy) return;
    const org: OrgOutcome = {
      status: "skipped",
      departments: 0,
      agents: 0,
      note: "跳过组织架构，进系统后可在「组织架构」里补",
    };
    finishSetup(modelOutcome, updateInstalled({ org }));
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
    clearSetupDraft();
    clearPendingInstall();
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
    setSkipModelAsking(false);
    setOrgBusy(false);
    setOrgError("");
    setOrgStage(0);
    setAiOrgOpen(false);
    setAiOrgResult(null);
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
    const modelBound = m?.status === "bound";
    // 服务端体检优先；拿不到体检结果时才退回向导里那次测试的结论
    const modelOk = serverModelReady ?? (m?.status === "bound" ? m.testOk : false);
    const modelText =
      modelBound
        ? `${m!.modelName}（${m!.modelIdOnProvider}）· ${modelBindingSummary(m!)}${
            modelOk ? "" : " · 连通测试未通过"
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

          {modelBound && !modelOk && (
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
              {serverModelReady === null && m?.testError && <p className="mt-1">原因：{m.testError}</p>}
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
  // 中心是部门的上级，不是部门：把两者相加会说成「8 个部门」，数字对不上组织树。
  const aiOrgLabel = aiOrgResult
    ? `${aiOrgResult.centers > 0 ? `${aiOrgResult.centers} 个中心 · ` : ""}` +
      `${aiOrgResult.departments} 个部门 / ${aiOrgResult.agents} 位 AI`
    : "";

  const footHint =
    step === 1
      ? "这个账号将自动成为「超级管理员」。"
      : step === 2
        ? "行业只影响推荐的组织架构；这一步点完就开始真正创建账号与公司。"
        : step === 3
          ? modelOutcome?.status === "bound"
            ? modelOutcome.testOk
              ? "模型已连通；第 4 步生成组织后会自动绑定给全部 AI 员工"
              : "模型已保存但未连通：建议先修好 Key，否则员工接任务会失败"
            : "接不上也没关系，可以先往下走，进系统后在「模型配置」里随时补。"
          : aiOrgResult
            ? `AI 已生成 ${aiOrgLabel}，点「完成」即可`
            : orgMode === "empty" || scale === "solo"
              ? "不生成部门与员工，进去后随时能在「组织架构」里补。"
              : !orgCounts
                ? "正在统计推荐的部门与员工…"
                : modelOutcome?.status === "bound"
                  ? `将创建 ${orgCounts.departments} 个部门 / ${orgCounts.agents} 位 AI，并把模型绑给它们`
                  : `将创建 ${orgCounts.departments} 个部门 / ${orgCounts.agents} 位 AI`;

  // 第 4 步这颗按钮到底会不会真的建部门与员工：
  // 空公司 / 个体户只是收尾，AI 已经生成过也不需要再生成——按钮文案不能还写「生成组织」。
  const willGenerateOrg = !aiOrgResult && orgMode === "preset" && scale !== "solo";

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

                {/* 免 Key 演示入口：新向导此前完全没有，小白在没有 Key 时看不到任何东西 */}
                <div
                  className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2.5 rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
                  style={{
                    border: "1px solid rgba(251,191,36,0.35)",
                    background: "rgba(251,191,36,0.07)",
                  }}
                >
                  <span style={{ color: "#e8d9a8" }}>
                    <span className="font-semibold" style={{ color: "var(--warning)" }}>
                      还没有 API Key？
                    </span>
                    可以先看一遍免 Key 演示（约 30 秒），了解员工怎么接任务。
                    <span style={{ color: "var(--muted)" }}>演示数据，不代表真实能力。</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setDemoOpen(true)}
                    className="btn-ghost shrink-0"
                    style={{ color: "inherit" }}
                  >
                    🎭 先体验演示（免 Key）→
                  </button>
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

            {/* ---------------- 步骤 4：资料与组织 ---------------- */}
            {step === 4 && (
              <div className="space-y-4">
                {!installed ? (
                  <p className="text-[12px] leading-relaxed" style={{ color: "var(--muted)" }}>
                    账号与公司还没创建成功，请回到第 1 步把前两步走完。
                  </p>
                ) : aiOrgResult ? (
                  <div
                    className="rounded-[13px] px-4 py-4 text-[12.5px] leading-relaxed"
                    style={{
                      border: "1px solid rgba(52,211,153,0.4)",
                      background: "rgba(52,211,153,0.08)",
                    }}
                    role="status"
                  >
                    <div className="font-semibold" style={{ color: "var(--success)" }}>
                      ✦ AI 已按你的资料生成并落地了组织
                    </div>
                    <p className="mt-1.5" style={{ color: "#c9c9d6" }}>
                      {aiOrgLabel} 员工已经建好。
                    </p>
                    <p className="mt-1 text-[11.5px]" style={{ color: "var(--muted)" }}>
                      点右下角「完成，进入工作台」收尾{modelOutcome?.status === "bound" ? "，系统会把刚接入的模型绑给这些员工" : ""}。
                      想再调整部门与岗位，进系统后打开「组织架构」随时可以重新生成。
                    </p>
                  </div>
                ) : (
                  <>
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

                {orgError && (
                  <div
                    className="rounded-[10px] px-3 py-2.5 text-[11.5px] leading-relaxed"
                    style={{
                      background: "rgba(248,113,113,0.12)",
                      border: "1px solid rgba(248,113,113,0.4)",
                      color: "#fca5a5",
                    }}
                    role="alert"
                  >
                    {orgError}
                  </div>
                )}

                {/* E：可选的第二条路——让 AI 读知识库资料自己设计组织 */}
                <div
                  className="rounded-[13px] px-4 py-3.5"
                  style={{
                    border: "1px solid rgba(139,92,246,0.35)",
                    background: "linear-gradient(120deg, rgba(139,92,246,0.10), rgba(34,211,238,0.05))",
                  }}
                >
                  <SectionLabel idx="E" title="让 AI 读你的资料设计组织" note="可选 · 比预设更贴合实际业务" />
                  <p className="text-[11.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                    把公司介绍、业务手册、SOP、正在做的项目放进知识库，AI 读完会自己判断该设哪些部门与岗位；
                    每个岗位都写明理由和依据的资料，落地前编制可调、岗位可去掉，落地后还能一键撤销。
                  </p>
                  <div className="mt-2.5">
                    <button
                      type="button"
                      onClick={() => setAiOrgOpen(true)}
                      disabled={orgBusy}
                      className="btn-ghost !py-1.5 text-xs disabled:opacity-40 disabled:cursor-not-allowed"
                      style={{ color: "inherit" }}
                    >
                      ✦ 用 AI 生成组织 →
                    </button>
                  </div>
                  <p className="mt-1.5 text-[10.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                    {modelOutcome?.status === "bound"
                      ? "AI 分析用第 3 步接入的模型；生成完成后会自动把模型绑给新岗位。"
                      : "这条路要先有可用模型（AI 分析也用它）；没接成也没关系，回到第 3 步修 Key，或先用预设完成。"}
                  </p>
                </div>
                  </>
                )}
              </div>
            )}
            {/* ---------------- 步骤 3：接入大模型 ---------------- */}
            {step === 3 && (
              <div className="space-y-4">
                {!installed ? (
                  <p className="text-[12px] leading-relaxed" style={{ color: "var(--muted)" }}>
                    安装流程尚未完成，请先回到第 1 步创建账号与公司。
                  </p>
                ) : modelOutcome?.status === "bound" && modelOutcome.testOk ? (
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
                      {modelOutcome.agentCount > 0
                        ? ` · ${modelBindingSummary(modelOutcome)}`
                        : " · 等第 4 步生成员工后一起绑定"}
                    </p>
                    <p className="mt-1 text-[11.5px]" style={{ color: "var(--muted)" }}>
                      连通测试通过，大脑已经就位。
                    </p>
                    <p className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
                      点右下角「下一步：搭建组织 →」开始建部门与 AI 员工。
                    </p>
                  </div>
                ) : modelOutcome?.status === "bound" ? (
                  /* 已保存但**没连通**：不能用绿色成功态，否则用户带着坏 Key 进系统，
                     第一个任务就失败，还以为是产品坏了。 */
                  <div
                    className="rounded-[13px] px-4 py-4 text-[12.5px] leading-relaxed"
                    style={{
                      border: "1px solid rgba(251,191,36,0.45)",
                      background: "rgba(251,191,36,0.09)",
                    }}
                    role="alert"
                  >
                    <div className="font-semibold" style={{ color: "var(--warning)" }}>
                      ⚠ 模型已保存，但未连通
                    </div>
                    <p className="mt-1.5" style={{ color: "#e8d9a8" }}>
                      {modelOutcome.modelName}（{modelOutcome.modelIdOnProvider}）
                      {modelOutcome.agentCount > 0
                        ? ` · ${modelBindingSummary(modelOutcome)}`
                        : " · 等第 4 步生成员工后一起绑定"}
                    </p>
                    <p className="mt-1.5" style={{ color: "#e8d9a8" }}>
                      连通测试没有通过，员工现在接任务会失败。常见原因：Key 复制不完整或已失效、
                      模型 ID 与服务商不符、需要代理网络、服务商额度用尽。
                    </p>
                    <details className="mt-2">
                      <summary className="cursor-pointer text-[11.5px] underline underline-offset-2" style={{ color: "#fcd34d" }}>
                        查看完整错误信息
                      </summary>
                      <p
                        className="mt-1.5 rounded-[9px] px-2.5 py-2 text-[11px] whitespace-pre-wrap break-all"
                        style={{
                          background: "rgba(0,0,0,0.28)",
                          border: "1px solid rgba(251,191,36,0.25)",
                          color: "#e8d9a8",
                          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                        }}
                      >
                        {modelOutcome.testError || "后端没有返回具体原因（可能是 Key 无效或网络不通）。"}
                      </p>
                    </details>
                    <div className="mt-3 flex flex-col sm:flex-row sm:items-center gap-2">
                      <button
                        type="button"
                        onClick={goFixKey}
                        disabled={modelBusy}
                        className="btn-primary w-full sm:w-auto px-4 py-2 disabled:opacity-40 disabled:cursor-not-allowed"
                        style={{ color: "inherit" }}
                      >
                        去修复 Key →
                      </button>
                      <button
                        type="button"
                        onClick={() => setStep(4)}
                        disabled={modelBusy}
                        className="btn-ghost w-full sm:w-auto"
                        style={{ color: "inherit" }}
                      >
                        先继续搭组织，进系统后再修
                      </button>
                    </div>
                    <p className="mt-1.5 text-[11px]" style={{ color: "#fca5a5" }}>
                      未连通，进入后任务会失败。
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
                      ，这一步先给它接上「大脑」；部门与 AI 员工放到第 4 步，生成后系统会自动把模型绑给它们。
                      没有模型，员工接任务只会回复「尚未绑定任何模型」。
                      <br />
                      Key 只写入本机 <code>apps/api/.env</code>（权限 600），
                      <b>不上传、不入库、不回显</b>；保存后立即生效，无需重启。
                      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                        <button
                          type="button"
                          onClick={() => setDemoOpen(true)}
                          className="btn-ghost !px-2.5 !py-1 text-[11px]"
                          style={{ color: "inherit" }}
                        >
                          🎭 没有 Key？先体验免 Key 演示（约 30 秒）
                        </button>
                        <span className="text-[10.5px]" style={{ color: "var(--muted)" }}>
                          演示数据，不代表真实能力
                        </span>
                      </div>
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
                            ref={keyInputRef}
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

                    <p className="text-[10.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
                      AI 员工在第 4 步才生成，绑定动作那时自动做完——这里不需要额外操作。
                    </p>

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
                          : "接入模型 →"}
                    </button>

                    {skipModelAsking && (
                      <div
                        className="rounded-[11px] px-3.5 py-3 text-[11.5px] leading-relaxed"
                        style={{
                          border: "1px solid rgba(251,191,36,0.4)",
                          background: "rgba(251,191,36,0.08)",
                        }}
                        role="alert"
                      >
                        <div className="font-medium" style={{ color: "var(--warning)" }}>
                          这一步还没接入模型
                        </div>
                        <p className="mt-1" style={{ color: "var(--muted)" }}>
                          跳过之后，第 4 步生成的 AI 员工接任务会回复「尚未绑定任何模型」；
                          进系统后在「模型配置」里也能补上。
                        </p>
                        <div className="mt-2.5 flex flex-wrap gap-2">
                          <button
                            type="button"
                            onClick={() => {
                              setModelOutcome({ status: "skipped" });
                              setSkipModelAsking(false);
                              setStep(4);
                            }}
                            className="btn-primary px-4 py-2"
                            style={{ color: "inherit" }}
                          >
                            确定跳过，去搭组织 →
                          </button>
                          <button
                            type="button"
                            onClick={() => setSkipModelAsking(false)}
                            className="btn-ghost"
                            style={{ color: "inherit" }}
                          >
                            回去接模型
                          </button>
                        </div>
                      </div>
                    )}
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
              {/* 安装发生在第 2 步结束时：那之后账号与公司已落库，回不去第 1、2 步了，
                  所以只有第 4 步允许退回第 3 步（改模型），第 3 步不再给"上一步"。 */}
              {(step === 2 || (step === 4 && !!installed)) && (
                <span style={{ color: "var(--muted)" }}>
                  <button
                    type="button"
                    onClick={() => setStep((s) => (s > 1 ? ((s - 1) as Step) : s))}
                    disabled={submitting || orgBusy}
                    className="btn-ghost w-full sm:w-auto"
                    style={{ color: "inherit" }}
                  >
                    ← 上一步
                  </button>
                </span>
              )}

              {step === 1 && (
                <button
                  type="button"
                  onClick={() => {
                    setAttempted1(true);
                    if (step1Valid) {
                      setStep(2);
                      return;
                    }
                    // 按钮可点，但校验不通过时给出**全部**原因，并滚到第一个错误字段：
                    // 之前的 disabled 按钮只会让用户看到"点不动"，不知道缺什么。
                    setTouched({ username: true, email: true, password: true, confirm: true });
                    const first = (["username", "email", "password", "confirm"] as FieldKey[]).find(
                      (k) => errors[k],
                    );
                    const el = first ? document.getElementById(`su-${first}`) : null;
                    el?.scrollIntoView({ block: "center", behavior: "smooth" });
                    (el as HTMLInputElement | null)?.focus();
                  }}
                  disabled={submitting}
                  className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "inherit" }}
                >
                  下一步 →
                </button>
              )}

              {step === 2 && (
                <button
                  type="button"
                  onClick={() => {
                    setAttempted2(true);
                    if (!companyError) void handleInstall();
                  }}
                  disabled={submitting || !!companyError}
                  className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "inherit" }}
                >
                  {submitting ? "正在创建…" : "创建账号与公司 →"}
                </button>
              )}

              {step === 3 && (
                <button
                  type="button"
                  onClick={() => {
                    if (modelOutcome?.status === "bound") {
                      setStep(4);
                      return;
                    }
                    setSkipModelAsking(true);
                  }}
                  disabled={modelBusy || !installed}
                  className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "inherit" }}
                >
                  下一步：搭建组织 →
                </button>
              )}

              {step === 4 && (
                <>
                  {willGenerateOrg && (
                    <span style={{ color: "var(--muted)" }}>
                      <button
                        type="button"
                        onClick={handleSkipOrg}
                        disabled={orgBusy}
                        className="btn-ghost w-full sm:w-auto"
                        style={{ color: "inherit" }}
                      >
                        不建组织，直接进入
                      </button>
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => void handleApplyOrg()}
                    disabled={orgBusy || !installed}
                    className="btn-primary w-full sm:w-auto px-5 py-2.5 disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{ color: "inherit" }}
                  >
                    {orgBusy
                      ? orgStage === 2
                        ? "正在绑定模型…"
                        : "正在生成组织…"
                      : willGenerateOrg
                        ? "生成组织并完成 →"
                        : "完成，进入工作台 →"}
                  </button>
                </>
              )}
            </div>
          </div>
        </main>
      </div>

      {/* 免 Key 演示：直接复用 components/DemoMode.tsx（与旧向导同一套逻辑） */}
      {demoOpen && <DemoMode onClose={() => setDemoOpen(false)} />}

      {/* AI 读资料生成组织：与「组织架构」页同一个抽屉，落地后由本页面负责绑模型并收尾 */}
      {aiOrgOpen && (
        <OrgSynthesisDrawer
          open={aiOrgOpen}
          companyId={installed?.companyId ?? null}
          onClose={() => setAiOrgOpen(false)}
          onApplied={(r) => {
            if (!r) {
              // 撤销落地：组织回到"还没生成"，收尾时按第 4 步的选择重新走一遍
              setAiOrgResult(null);
              updateInstalled({
                org: { status: "skipped", departments: 0, agents: 0, note: "已撤销 AI 生成的组织" },
              });
              return;
            }
            const result = { centers: r.created_centers, departments: r.created_departments, agents: r.created_agents };
            setAiOrgResult(result);
            // 立刻写进「待续装」标记：落地之后刷新页面不会又去叠加一套预设部门
            updateInstalled({
              org: {
                status: "created",
                departments: result.departments,
                agents: result.agents,
                presetIndustry: AI_ORG_MARK,
              },
            });
          }}
        />
      )}
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
