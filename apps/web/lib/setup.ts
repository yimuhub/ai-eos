// 首次安装引导（/setup）的纯逻辑：类型、行业 ↔ 组织预设的模糊匹配、规模 → 部门子集、错误文案。
//
// 为什么单独一个文件：这些规则都需要被测试（vitest 只跑 lib/**），
// 而且页面里混着它们会让 JSX 难以阅读。这里不碰 React、不发请求。

import { ApiError, describeError } from "./api";

/* ---------------- 接口类型（与 P5 契约第 5 节 / org_presets.py 保持一致） ---------------- */

export type SetupStatus = { needs_setup: boolean };

export type SetupUser = {
  id: string;
  username: string;
  email: string;
  display_name?: string | null;
  is_superadmin?: boolean | null;
};

export type SetupCompany = { id: string; name: string };

/** POST /setup 的 201 响应 */
export type SetupResult = {
  access_token: string;
  refresh_token?: string | null;
  token_type?: string;
  user: SetupUser;
  company: SetupCompany;
};

/** GET /org-presets 的列表项（只给摘要） */
export type PresetSummary = {
  id: string;
  industry: string;
  emoji?: string;
  kind?: string;
  department_count: number;
  agent_count: number;
};

export type PresetAgent = { name: string; role?: string; expert_slug?: string };

export type PresetDepartment = { name: string; desc?: string; agents?: PresetAgent[] };

/** GET /org-presets/{pid} 的详情 */
export type PresetDetail = {
  id: string;
  industry: string;
  emoji?: string;
  departments: PresetDepartment[];
};

/** POST /org-presets/{pid}/apply 的结果 */
export type ApplyResult = {
  preset: string;
  industry?: string;
  created_departments: number;
  created_agents: number;
  skipped_departments?: number;
  skipped_agents?: number;
  dry_run?: boolean;
  note?: string;
};

/* ---------------- 行业（12 个 chip） ---------------- */

export type Industry = {
  key: string;
  label: string;
  /** 用来匹配 GET /org-presets 返回里的 industry 字段（大小写不敏感、包含即命中） */
  keywords: string[];
};

/** 顺序即界面顺序。关键词只用于**推荐**，用户随时可在第 3 步手动换预设。 */
export const INDUSTRIES: Industry[] = [
  { key: "internet", label: "互联网/SaaS", keywords: ["互联网", "科技", "saas", "软件"] },
  { key: "content", label: "内容自媒体", keywords: ["内容", "自媒体", "媒体"] },
  { key: "ecommerce", label: "电商", keywords: ["电商", "零售"] },
  { key: "retail_chain", label: "连锁零售·超市", keywords: ["零售", "超市", "连锁"] },
  { key: "manufacturing", label: "制造业", keywords: ["制造"] },
  { key: "construction", label: "建筑工程", keywords: ["建筑", "工程", "地产"] },
  { key: "logistics", label: "物流供应链", keywords: ["物流", "供应链"] },
  { key: "hospitality", label: "餐饮酒店", keywords: ["餐饮", "酒店"] },
  { key: "services", label: "专业服务", keywords: ["专业服务", "咨询", "服务"] },
  { key: "education", label: "教育培训", keywords: ["教育", "培训"] },
  { key: "healthcare", label: "医疗健康", keywords: ["医疗", "健康"] },
  { key: "agriculture", label: "农业食品", keywords: ["农业", "食品"] },
];

export function industryLabel(key: string): string {
  return INDUSTRIES.find((i) => i.key === key)?.label ?? "互联网/SaaS";
}

/* ---------------- 规模 → 部门子集 ---------------- */

export type OrgScale = "solo" | "studio" | "smb" | "group";

export type ScaleOption = {
  key: OrgScale;
  name: string;
  desc: string;
  /** 取预设的前 N 个部门；0 = 不建组织，null = 全部 */
  deptLimit: number | null;
};

export const SCALES: ScaleOption[] = [
  { key: "solo", name: "个体户", desc: "不建部门，0 组织", deptLimit: 0 },
  { key: "studio", name: "工作室", desc: "取前 2 个部门", deptLimit: 2 },
  { key: "smb", name: "中小公司", desc: "取前 4 个部门", deptLimit: 4 },
  { key: "group", name: "集团大厂", desc: "全部部门", deptLimit: null },
];

/** 预设 id 的兜底值：一个关键词都没匹配上时用它（契约钦定的兜底） */
export const FALLBACK_PRESET_ID = "tech";

/**
 * 可作为"行业蓝图"的预设。
 *
 * `bigco_*` 是"照抄某家具体公司"的形态（阿里/腾讯…），不是行业蓝图，
 * 自动匹配和手动下拉里都排除；若后端只返回了这类预设，则退回全量。
 */
export function presetPool(list: PresetSummary[]): PresetSummary[] {
  const pool = list.filter((p) => !p.id.startsWith("bigco_"));
  return pool.length ? pool : list;
}

/**
 * 按行业字段模糊匹配预设 id。
 * 关键词命中数优先，命中数相同取更短的 industry 串（"零售 / 电商" 胜过
 * "大厂仿照·阿里巴巴（电商+云AI双核）"），再相同则保持后端返回顺序。
 */
export function matchPresetId(list: PresetSummary[], industryKey: string): string {
  const pool = presetPool(list);
  const ind = INDUSTRIES.find((i) => i.key === industryKey);
  if (!ind || !pool.length) return FALLBACK_PRESET_ID;

  let bestId = "";
  let bestScore = 0;
  let bestLen = Number.MAX_SAFE_INTEGER;
  for (const p of pool) {
    const text = (p.industry || "").toLowerCase();
    const score = ind.keywords.reduce(
      (n, kw) => (text.includes(kw.toLowerCase()) ? n + 1 : n),
      0,
    );
    if (!score) continue;
    if (score > bestScore || (score === bestScore && text.length < bestLen)) {
      bestId = p.id;
      bestScore = score;
      bestLen = text.length;
    }
  }
  return bestId || FALLBACK_PRESET_ID;
}

/**
 * 规模 → 要引入的部门名（**顺序与名称都来自接口返回**，不在这里编）。
 * 个体户返回空数组（= 不调用 apply）。
 */
export function departmentSubset(detail: PresetDetail, scale: OrgScale): string[] {
  const opt = SCALES.find((s) => s.key === scale);
  if (!opt || opt.deptLimit === 0) return [];
  const names = detail.departments.map((d) => (d.name || "").trim()).filter(Boolean);
  return opt.deptLimit === null ? names : names.slice(0, opt.deptLimit);
}

/** 预览：将创建多少部门 / 多少位 AI（含部门名，供界面直接展示） */
export function previewCounts(
  detail: PresetDetail | null,
  scale: OrgScale,
): { departments: number; agents: number; names: string[] } | null {
  if (!detail) return null;
  const names = departmentSubset(detail, scale);
  const want = new Set(names);
  const agents = detail.departments
    .filter((d) => want.has((d.name || "").trim()))
    .reduce((n, d) => n + (d.agents?.length ?? 0), 0);
  return { departments: names.length, agents, names };
}

/* ---------------- 密码强度（仅用于进度条，规则仍以 validate.ts 为准） ---------------- */

export function passwordStrength(pw: string): 0 | 1 | 2 | 3 {
  if (pw.length < 8) return 0; // 不达标就不算"有强度"
  let score = 1;
  if (/[A-Za-z]/.test(pw) && /\d/.test(pw)) score += 1;
  if (pw.length >= 12 || /[^A-Za-z0-9]/.test(pw)) score += 1;
  return Math.min(score, 3) as 0 | 1 | 2 | 3;
}

/* ---------------- 安装草稿：可续装，但**绝不**持久化密钥 ---------------- */

/**
 * 草稿版本。key 与 payload 都带版本号：旧版本的残留草稿直接丢弃，
 * 不做兼容迁移（安装向导的字段改动很小，迁移的复杂度不划算）。
 */
export const SETUP_DRAFT_VERSION = 1;
export const SETUP_DRAFT_KEY = `aieos_setup_draft_v${SETUP_DRAFT_VERSION}`;

/**
 * 「已安装、待接模型」标记（localStorage，和 token 同生命周期）。
 *
 * 为什么不在草稿里：草稿在 sessionStorage（只活在这个标签页），
 * 而"账号与公司已经建好、只差模型"是**跨标签页/跨刷新**都该保留的事实。
 * 刷新后 `needs_setup=false`，若没有这个标记就只能把用户踢回登录页，
 * 第 4 步的上下文（公司、员工数）就丢了。
 */
export const SETUP_PENDING_KEY = "aieos_setup_pending_model_v1";

/** 第 4 步表单里可以回填的非敏感字段（Key 永远不进来） */
export type SetupModelDraft = {
  providerType: string;
  name: string;
  baseUrl: string;
  modelId: string;
};

/** 组织架构结果摘要（第 4 步恢复时用来展示"会绑定多少员工"） */
export type SetupOrgOutcome =
  | { status: "skipped"; departments: 0; agents: 0; note: string }
  | { status: "created"; departments: number; agents: number; presetIndustry: string }
  | { status: "failed"; departments: 0; agents: 0; reason: string };

/** 安装成功的上下文（写进「待接模型」标记） */
export type SetupInstalledDraft = {
  companyId: string;
  companyName: string;
  username: string;
  email: string;
  org: SetupOrgOutcome;
};

/**
 * 草稿白名单。
 *
 * **明确没有** password / confirm / API Key：它们只存在于组件内存里。
 * 序列化时逐字段重建对象（而不是把入参展开），因此调用方就算误传
 * `{ password }` 也不会写进浏览器存储。
 */
export type SetupDraft = {
  step: 1 | 2 | 3 | 4;
  username: string;
  email: string;
  companyName: string;
  description: string;
  industry: string;
  orgMode: "preset" | "empty";
  scale: OrgScale;
  presetManual: string | null;
  model: SetupModelDraft | null;
};

const asStr = (v: unknown): string => (typeof v === "string" ? v : "");

function asCount(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function asStep(v: unknown): 1 | 2 | 3 | 4 {
  const n = Number(v);
  return n === 2 || n === 3 || n === 4 ? n : 1;
}

function asOrgMode(v: unknown): "preset" | "empty" {
  return v === "empty" ? "empty" : "preset";
}

function asScale(v: unknown): OrgScale {
  return SCALES.some((s) => s.key === v) ? (v as OrgScale) : "smb";
}

function asIndustry(v: unknown): string {
  return INDUSTRIES.some((i) => i.key === v) ? (v as string) : INDUSTRIES[0].key;
}

function asPresetId(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v : null;
}

function parseModel(v: unknown): SetupModelDraft | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const model: SetupModelDraft = {
    providerType: asStr(o.providerType),
    name: asStr(o.name),
    baseUrl: asStr(o.baseUrl),
    modelId: asStr(o.modelId),
  };
  return model.providerType || model.name || model.baseUrl || model.modelId ? model : null;
}

function parseOrg(v: unknown): SetupOrgOutcome | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (o.status === "created") {
    return {
      status: "created",
      departments: asCount(o.departments),
      agents: asCount(o.agents),
      presetIndustry: asStr(o.presetIndustry),
    };
  }
  if (o.status === "skipped") {
    return { status: "skipped", departments: 0, agents: 0, note: asStr(o.note) };
  }
  if (o.status === "failed") {
    return { status: "failed", departments: 0, agents: 0, reason: asStr(o.reason) };
  }
  return null;
}

/** 草稿 → 字符串（纯函数，便于在 node 里测）。逐字段重建 = 白名单。 */
export function serializeDraft(input: SetupDraft): string {
  const model = input.model
    ? {
        providerType: asStr(input.model.providerType),
        name: asStr(input.model.name),
        baseUrl: asStr(input.model.baseUrl),
        modelId: asStr(input.model.modelId),
      }
    : null;
  const payload = {
    version: SETUP_DRAFT_VERSION,
    step: asStep(input.step),
    username: asStr(input.username),
    email: asStr(input.email),
    companyName: asStr(input.companyName),
    description: asStr(input.description),
    industry: asIndustry(input.industry),
    orgMode: asOrgMode(input.orgMode),
    scale: asScale(input.scale),
    presetManual: asPresetId(input.presetManual),
    model: parseModel(model),
  };
  return JSON.stringify(payload);
}

/**
 * 字符串 → 草稿。任何异常（非 JSON / 版本不符 / 值不合法）都返回 null，
 * 调用方按"没有草稿"处理即可——草稿坏了绝不能把安装页搞白屏。
 */
export function parseDraft(raw: unknown): SetupDraft | null {
  if (typeof raw !== "string" || !raw) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const o = obj as Record<string, unknown>;
  if (o.version !== SETUP_DRAFT_VERSION) return null; // 版本不匹配：丢弃，不做迁移
  return {
    step: asStep(o.step),
    username: asStr(o.username),
    email: asStr(o.email),
    companyName: asStr(o.companyName),
    description: asStr(o.description),
    industry: asIndustry(o.industry),
    orgMode: asOrgMode(o.orgMode),
    scale: asScale(o.scale),
    presetManual: asPresetId(o.presetManual),
    model: parseModel(o.model),
  };
}

function session(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null; // 隐私模式可能直接抛错
  }
}

/** 保存草稿（只在 sessionStorage；写失败不影响向导） */
export function saveSetupDraft(draft: SetupDraft): void {
  try {
    session()?.setItem(SETUP_DRAFT_KEY, serializeDraft(draft));
  } catch {
    /* 配额满 / 被禁用：草稿只是锦上添花 */
  }
}

export function loadSetupDraft(): SetupDraft | null {
  try {
    return parseDraft(session()?.getItem(SETUP_DRAFT_KEY));
  } catch {
    return null;
  }
}

export function clearSetupDraft(): void {
  try {
    session()?.removeItem(SETUP_DRAFT_KEY);
  } catch {
    /* 忽略 */
  }
}

/** 写入「已安装、待接模型」标记（不含任何密钥） */
export function savePendingInstall(info: SetupInstalledDraft): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      SETUP_PENDING_KEY,
      JSON.stringify({ version: SETUP_DRAFT_VERSION, ...info }),
    );
  } catch {
    /* 忽略：最坏情况是刷新后回到登录页 */
  }
}

export function loadPendingInstall(): SetupInstalledDraft | null {
  if (typeof window === "undefined") return null;
  let obj: unknown;
  try {
    const raw = window.localStorage.getItem(SETUP_PENDING_KEY);
    if (!raw) return null;
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const o = obj as Record<string, unknown>;
  if (o.version !== SETUP_DRAFT_VERSION) return null;
  const companyId = asStr(o.companyId);
  const org = parseOrg(o.org);
  if (!companyId || !org) return null;
  return {
    companyId,
    companyName: asStr(o.companyName),
    username: asStr(o.username),
    email: asStr(o.email),
    org,
  };
}

export function clearPendingInstall(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(SETUP_PENDING_KEY);
  } catch {
    /* 忽略 */
  }
}

/* ---------------- 错误文案：说清"下一步该做什么" ---------------- */

/** 状态探测失败（GET /setup/status） */
export function setupStatusErrorMessage(e: unknown): string {
  const err = e instanceof ApiError ? e : null;
  if (err?.status === 404) {
    return "安装状态接口不存在（HTTP 404）：后端可能还是旧版本，/setup/status 尚未实现。请升级后端后重试。";
  }
  if (err?.status === 0) return err.message;
  return `无法确认系统是否已初始化：${describeError(e, "未知错误")}`;
}

/** 提交安装失败（POST /setup） */
export function setupErrorMessage(e: unknown): string {
  const err = e instanceof ApiError ? e : null;
  if (!err) return describeError(e, "安装失败，请重试");
  if (err.status === 409) return "系统已初始化，请直接登录";
  if (err.status === 404) {
    return "安装接口不存在（HTTP 404）：后端可能还是旧版本，/setup 尚未实现。请升级后端后重试。";
  }
  if (err.status === 0) return err.message;
  if (err.status === 422) return `提交的信息不合法：${err.message}`;
  if (err.status === 429) return "请求过于频繁，请稍等一分钟再试";
  if (err.status >= 500) return `后端处理安装时出错（HTTP ${err.status}）：${err.message}`;
  return `安装失败（HTTP ${err.status}）：${err.message}`;
}

/** 读预设失败：安装不会因此中断，但要告诉用户组织架构会降级 */
export function presetErrorMessage(e: unknown): string {
  const err = e instanceof ApiError ? e : null;
  if (err?.status === 401 || err?.status === 403) {
    return "读取组织预设被拒绝（HTTP 401/403）：后端可能还是旧版本（预设目录尚未公开）。安装照常进行，创建公司后会用新账号再匹配一次预设。";
  }
  if (err?.status === 404) {
    return "预设接口不可用（HTTP 404，后端版本较旧），预览数据取不到。安装照常进行，组织架构可在进入系统后手动引入。";
  }
  if (err?.status === 0) {
    return "无法连接后端读取组织预设。安装照常进行（若后端确实未启动，提交时也会失败并给出原因）。";
  }
  return `读取组织预设失败：${describeError(e, "未知错误")}。安装仍可继续，组织架构可在进入系统后手动引入。`;
}
