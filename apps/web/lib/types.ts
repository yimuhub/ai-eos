/**
 * 领域实体类型：让 `api.get<T>()` 的泛型真正用起来。
 *
 * ## 为什么要有这个文件
 *
 * 后端有 300+ 个接口，前端此前几乎全部用 `any` 接返回值。后果是：
 * 接口改了字段名、改了结构，TypeScript 一声不吭，问题只在运行时暴露——
 * 而这次审计已经证明"静默失效"是这个项目最主要的故障形态。
 *
 * 这里只声明**已经在用的核心实体**，不追求一次覆盖全部。
 * 新增字段时优先补到这里，而不是就地再写一个 any。
 */

export type ID = string;

export interface Company {
  id: ID;
  name: string;
  description?: string | null;
  logo?: string | null;
  created_at?: string;
}

export interface Department {
  id: ID;
  company_id: ID;
  name: string;
  description?: string | null;
  parent_department_id?: ID | null;
  manager_agent_id?: ID | null;
  sort_order?: number;
  pinned?: boolean;
}

export interface ModelConfig {
  id: ID;
  name: string;
  provider_type: string;
  base_url: string | null;
  model_id: string;
  config?: Record<string, unknown> | null;
  /** 归属公司；为空表示"未归属/共享"（升级前的遗留配置）。 */
  company_id?: ID | null;
  /** 未归属的配置只能认领、不能直接删除。 */
  unowned?: boolean;
}

/** 员工的能力包绑定与执行底座（存在 `agents.memory_config` 里） */
export interface AgentMemoryConfig {
  /** 勾选的能力包 key，如 computer_run_shell */
  capabilities?: string[];
  /** 执行底座：native_llm / hermes / codex */
  backend?: string;
  /** 人格与职责设定。部分页面把它存在 memory_config 里而非独立列 */
  system_prompt?: string;
}

/** 员工与模型的绑定关系（`GET /agents/{id}/models`） */
export interface AgentModelBinding {
  /** 绑定用途，目前主要有 primary */
  role: string;
  model_id: string;
}

export interface Agent {
  id: ID;
  company_id: ID;
  /** 后端恒返回该字段（可能为 null），所以是必填而非可选 */
  department_id: ID | null;
  name: string;
  role?: string | null;
  description?: string | null;
  system_prompt?: string | null;
  avatar?: string | null;
  /** IDLE / ACTIVE / RUNNING / PAUSED / DISABLED / ERROR */
  status: string;
  manager_agent_id: ID | null;
  memory_config?: AgentMemoryConfig | null;
  model_configs?: AgentModelBinding[];
}

export type TaskStatus =
  | "TODO" | "PLANNING" | "RUNNING" | "WAITING"
  | "COMPLETED" | "FAILED" | "PAUSED" | "CANCELLED";

export interface TaskExecution {
  id: ID;
  agent_id: ID;
  status: "running" | "completed" | "failed" | "paused" | "cancelled";
  output?: string | null;
  /** 失败原因。用户排查"为什么失败"主要靠这个字段。 */
  error?: string | null;
}

export interface Task {
  id: ID;
  company_id: ID;
  title: string;
  description?: string | null;
  status: TaskStatus;
  priority?: string;
  assigned_agent_id?: ID | null;
  parent_task_id?: ID | null;
  result?: string | null;
  executions?: TaskExecution[];
  /** Worker A 在后端任务模型新增：0.0-1.0 执行进度；旧版本后端不返回 */
  progress?: number | null;
  /** Worker A 在后端任务模型新增：当前阶段文案；可空 */
  current_stage?: string | null;
}

export interface Project {
  id: ID;
  company_id: ID;
  name: string;
  description?: string | null;
  status?: string;
  priority?: string;
  manager_agent_id?: ID | null;
}

export interface Asset {
  id: ID;
  company_id: ID;
  name: string;
  type: string;
  /** 字节数。后端可能不返回，故可选 */
  size?: number;
  path?: string;
  task_id?: ID | null;
  agent_id?: ID | null;
}

/** `GET /metrics/overview` —— 质量度量。 */
export interface MetricsOverview {
  company: { id: ID; name: string };
  window_days: number;
  tasks: {
    total: number;
    completed: number;
    failed: number;
    running: number;
    cancelled: number;
    todo: number;
    /** 样本为空时是 null（"没有数据"），不是 0（"全部失败"）。 */
    success_rate: number | null;
  };
  executions: {
    total: number;
    completed: number;
    failed: number;
    success_rate: number | null;
    avg_duration_seconds: number | null;
  };
  model_calls: {
    total: number;
    success: number;
    failed: number;
    success_rate: number | null;
    avg_latency_ms: number | null;
  };
  failure_reasons: { reason: string; count: number }[];
  failure_by_agent: { agent_id: ID; agent_name: string; failed: number }[];
  throughput_by_day: { date: string; completed: number; failed: number }[];
  /** 采样上限保护：truncated 为真时数字是"抽样结论"，界面上要说清楚 */
  sampled?: {
    tasks: number; executions: number; model_calls: number;
    limit: number; truncated: boolean;
  };
}

/** `GET /access/matrix` —— 角色 × 能力矩阵。 */
export interface RoleCapabilities {
  role: "owner" | "admin" | "member" | "viewer";
  capabilities: string[];
}

/* ---------- 模型配置中心的接口形状 ---------- */

/** `GET /models/convert-config` —— 文档转换专用模型 */
export interface ConvertConfig {
  convert_model_id: string | null;
}

/** `POST /models/providers/list-models` 的返回：成功给列表，失败给原因 */
export type ProviderModelsResult =
  | { status: "ok"; models: string[] }
  | { status: "error"; error: string };

/** `POST /models/providers/batch-import` 的返回 */
export interface BatchImportResult {
  imported_count: number;
  skipped_existing?: string[];
  /** 部分导入失败时后端会回传逐条原因 */
  errors?: string[];
}

/** `POST /models/{id}/test` 的返回 */
export type ModelTestResult =
  | { status: "ok"; reply: string }
  | { status: "error"; error: string };

/** `GET /agents/capability-packs` —— 能力包与执行底座目录 */
export interface CapabilityPack {
  key: string;
  label: string;
  description?: string;
  /** 风险等级：高危能力（如本机命令执行）需要在 UI 上明确标出 */
  risk?: "low" | "medium" | "high" | string;
}

export interface AgentBackend {
  key: string;
  label: string;
  description?: string;
}

export interface CapabilityCatalog {
  packs: CapabilityPack[];
  backends: AgentBackend[];
}

/**
 * 把比率渲染成可读文本。
 *
 * 这个函数存在的唯一理由是：**区分"没有数据"和"0%"**。
 * 一个刚建好、还没跑过任务的公司，看板上不该显示"成功率 0%"——
 * 那会把"没数据"读成"全失败"。
 */
export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return "<1 秒";
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m} 分 ${s} 秒`;
}
