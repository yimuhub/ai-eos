/** 工作台（方案 B）共用的类型与展示工具。数据全部来自现有接口，不另建模型。 */

export type WsAgent = {
  id: string;
  name: string;
  role?: string | null;
  /** 后端可能返回，用于判断员工是否在岗 */
  status?: string | null;
};

export type WsTask = {
  id: string;
  seq?: number;
  agent_id: string;
  agent_name: string;
  title: string;
  status: string;
  announce_status?: string;
  deliverable_name?: string | null;
};

export type WsSession = {
  id: string;
  project_id?: string | null;
  goal: string;
  status: string;
  leader_agent_id: string;
  leader_name: string;
  tasks: WsTask[];
  final_output_path?: string | null;
};

export type WsProject = {
  id: string;
  name: string;
  status: string;
  progress?: number;
  task_total?: number;
  task_completed?: number;
};

export type WsMeeting = {
  id: string;
  title: string;
  status: string;
  meeting_type?: string | null;
  topic?: string | null;
};

/** 工作台中间栏的四种会话形态：单人、项目群、会议、作战 */
export type WsGroupMember = { id: string; name: string; role?: string | null };

/** 自由群聊：内置分组（营销 / 产品研发）与老板自建群。 */
export type WsChatGroup = {
  id: string;
  name: string;
  kind: "preset" | "custom";
  preset_id?: string | null;
  emoji?: string | null;
  hint?: string | null;
  agent_ids: string[];
  members: WsGroupMember[];
  /** 内置分组还没集齐的员工（点一下自动生成） */
  missing?: { name: string; role?: string | null }[];
  message_count?: number;
  last_at?: string | null;
  created_at?: string | null;
};

export type WsMode = "single" | "group" | "meeting" | "war";

export type WsView = { mode: WsMode; id: string };

export type GroupMsg = {
  id?: string;
  role: string;
  agent_name?: string | null;
  text: string;
  created_at?: string | null;
};

export type MeetingMsg = {
  speaker: string;
  speaker_type: string;
  content: string;
  phase?: string;
};

export type MeetingDetail = {
  id: string;
  title: string;
  status: string;
  meeting_type?: string;
  topic?: string | null;
  minutes?: string | null;
  summary?: string | null;
  participants: { agent_id: string; name: string; spoke_count: number }[];
  messages: MeetingMsg[];
  decisions: { id: string; content: string; status: string }[];
  action_items: { id: string; content: string; assignee?: string | null; status: string }[];
};

export type WarTaskLite = {
  id: string;
  seq?: number;
  title: string;
  agent_name?: string;
  status: string;
};

export type WarDetail = {
  id: string;
  goal: string;
  status: string;
  leader_name?: string;
  project_id?: string | null;
  final_output_path?: string | null;
  tasks: WarTaskLite[];
};

export type WsArtifact = {
  id: string;
  name: string;
  type: string;
  status: string;
  version: number;
};

export type WsFile = { id?: string; name: string; size?: number };

export type WsMsg = {
  role: "boss" | "agent";
  content: string;
  created_at?: string;
  /** 正在流式接收（渲染光标、显示"正在思考"） */
  streaming?: boolean;
  /** 本轮产出的文档名（流式结束后由 done 事件带回） */
  file_name?: string;
};

export type TraceNode = {
  kind: "execution" | "boss" | "event";
  id: string;
  ts?: string | null;
  agent_id?: string;
  agent_name?: string;
  status?: string;
  input?: string;
  output?: string;
  error?: string | null;
  model_id?: string | null;
  duration_s?: number | null;
  type?: string;
  content?: string;
  event_type?: string;
  detail?: string;
  checkpoints?: { type: string; step: number; ts?: string | null }[];
  usage?: { tokens: number; cost: number; calls: number; last_model?: string | null } | null;
};

export type TraceResp = {
  task: { id: string; title: string; status: string };
  nodes: TraceNode[];
  total: { calls: number; tokens: number; cost: number; executions: number; failed: number };
};

/** 作战会话状态 → 中文与色调 */
export function sessionMeta(status: string): { label: string; tone: "run" | "warn" | "ok" | "bad" | "idle" } {
  switch (status) {
    case "analyzing": return { label: "分析中", tone: "run" };
    case "announcing": return { label: "分工中", tone: "run" };
    case "confirmed": return { label: "待开工", tone: "warn" };
    case "executing": return { label: "执行中", tone: "run" };
    case "delivering": return { label: "汇总交付", tone: "warn" };
    case "review": return { label: "审核中", tone: "warn" };
    case "done": return { label: "已完成", tone: "ok" };
    case "failed": return { label: "失败", tone: "bad" };
    case "paused": return { label: "已暂停", tone: "idle" };
    default: return { label: status || "未知", tone: "idle" };
  }
}

/** 会议状态 → 中文与色调 */
export function meetingMeta(status: string): { label: string; tone: "run" | "warn" | "ok" | "bad" | "idle" } {
  switch (status) {
    case "SCHEDULED": return { label: "待开始", tone: "warn" };
    case "RUNNING": return { label: "进行中", tone: "run" };
    case "PAUSED": return { label: "已暂停", tone: "idle" };
    case "COMPLETED": return { label: "已完成", tone: "ok" };
    case "CANCELLED": return { label: "已取消", tone: "bad" };
    default: return { label: status || "未知", tone: "idle" };
  }
}

export function taskMeta(status: string): { label: string; tone: "run" | "warn" | "ok" | "bad" | "idle" } {
  switch (status) {
    case "todo": return { label: "待办", tone: "idle" };
    case "running": return { label: "进行中", tone: "run" };
    case "submitted": return { label: "待确认", tone: "warn" };
    case "done": return { label: "已完成", tone: "ok" };
    case "failed": return { label: "失败", tone: "bad" };
    case "cancelled": return { label: "已取消", tone: "idle" };
    case "paused": return { label: "已暂停", tone: "warn" };
    default: return { label: status || "未知", tone: "idle" };
  }
}

export function artifactMeta(status: string): { label: string; cls: string } {
  switch (status) {
    case "DRAFT": return { label: "草稿", cls: "text-[var(--muted)] bg-white/[0.06]" };
    case "IN_REVIEW": return { label: "审核中", cls: "text-amber-300 bg-amber-500/15" };
    case "APPROVED": return { label: "已批准", cls: "text-emerald-300 bg-emerald-500/15" };
    case "REJECTED": return { label: "已驳回", cls: "text-red-300 bg-red-500/15" };
    default: return { label: status, cls: "text-[var(--muted)] bg-white/[0.06]" };
  }
}

const TONE_CLASS: Record<string, string> = {
  run: "text-indigo-300 bg-indigo-500/15 border-indigo-500/25",
  warn: "text-amber-300 bg-amber-500/12 border-amber-500/25",
  ok: "text-emerald-300 bg-emerald-500/12 border-emerald-500/25",
  bad: "text-red-300 bg-red-500/12 border-red-500/25",
  idle: "text-[var(--muted)] bg-white/[0.05] border-white/[0.08]",
};

export function toneClass(tone: string): string {
  return TONE_CLASS[tone] || TONE_CLASS.idle;
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(t).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function clock(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/* ---------- C2 左栏按项目分组 ---------- */

export type ProjectGroup<T> = {
  /** 项目 id；null 表示「未归项目」 */
  projectId: string | null;
  name: string;
  threads: T[];
};

/**
 * 把（已过滤/已排序的）会话按 projectId 分组。
 *
 * 纯函数，便于单测：
 * - 组顺序跟随 projects 的顺序，空组不出现；「未归项目」永远排在最后；
 * - 组内保持传入顺序（调用方已处理好置顶/未读/搜索/类型筛选）；
 * - projectId 指向不存在的项目时归入「未归项目」，不凭空造一个标题。
 */
export function groupThreadsByProject<T extends { projectId?: string | null }>(
  threads: readonly T[],
  projects: readonly { id: string; name: string }[],
  ungroupedLabel = "未归项目",
): ProjectGroup<T>[] {
  const buckets = new Map<string | null, T[]>();
  const known = new Set(projects.map((p) => p.id));
  for (const t of threads) {
    const pid = t.projectId && known.has(t.projectId) ? t.projectId : null;
    const list = buckets.get(pid);
    if (list) list.push(t);
    else buckets.set(pid, [t]);
  }

  const out: ProjectGroup<T>[] = [];
  for (const p of projects) {
    const list = buckets.get(p.id);
    if (list && list.length) out.push({ projectId: p.id, name: p.name, threads: list });
  }
  const rest = buckets.get(null);
  if (rest && rest.length) out.push({ projectId: null, name: ungroupedLabel, threads: rest });
  return out;
}
