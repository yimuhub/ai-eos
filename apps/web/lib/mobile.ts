/**
 * C6 移动端（/m）可测逻辑：状态中文、时间格式、会话色调、排序与退回意见校验。
 *
 * 全部是纯函数，不碰 React/DOM，因此可直接在 vitest（node 环境）里断言。
 * 页面只负责调用与渲染，避免把口径散落在 JSX 里。
 */

export type Tone = "run" | "ok" | "warn" | "bad" | "idle";

const TASK_STATUS_ZH: Record<string, string> = {
  TODO: "待办",
  PLANNING: "规划中",
  RUNNING: "执行中",
  WAITING: "等待中",
  PAUSED: "已暂停",
  REVIEW: "审核中",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  // 作战会话内的成员任务是另一套小写状态，同一函数兜住，避免详情里露英文
  todo: "待开始",
  running: "执行中",
  submitted: "已提交",
  approved: "已通过",
  failed: "失败",
  cancelled: "已取消",
};

export function taskStatusZh(status?: string | null): string {
  if (!status) return "未知";
  return TASK_STATUS_ZH[status] || status;
}

/** 与桌面端 intervention 页同一套审批动作中文名（移动端只读展示，不另造口径） */
const ACTION_ZH: Record<string, string> = {
  task_create: "创建任务",
  task_complete: "完成任务",
  meeting_start: "召开会议",
  artifact_approve: "成果验收",
  escalate: "升级请求",
  computer_command: "高危本机操作",
  external_agent: "外部 Agent 外发",
};

export function approvalActionZh(action?: string | null): string {
  if (!action) return "待审批操作";
  return ACTION_ZH[action] || action;
}

/**
 * 相对时间：1 分钟内「刚刚」，7 天内用「N 分钟/小时/天前」，
 * 更早显示「MM-DD HH:mm」。无效/空值返回「—」，不渲染 NaN。
 * `now` 可注入，便于测试；负数（时钟偏差）按「刚刚」处理。
 */
export function timeLabel(iso?: string | null, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = now - t;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 作战会话状态 → 中文 + 色调（移动端只读列表/详情共用） */
export function sessionStatusMeta(status?: string | null): { label: string; tone: Tone } {
  switch (status) {
    case "analyzing":
      return { label: "分析中", tone: "run" };
    case "announcing":
      return { label: "分工中", tone: "run" };
    case "confirmed":
      return { label: "待开工", tone: "warn" };
    case "executing":
      return { label: "执行中", tone: "run" };
    case "delivering":
      return { label: "汇总交付", tone: "warn" };
    case "review":
      return { label: "审核中", tone: "warn" };
    case "paused":
    case "stopped":
      return { label: "已暂停", tone: "idle" };
    case "done":
      return { label: "已完成", tone: "ok" };
    case "failed":
      return { label: "失败", tone: "bad" };
    default:
      return { label: status || "未知", tone: "idle" };
  }
}

/**
 * 按 updated_at 倒序，缺失/非法时间排最后；不改动入参（React state 安全）。
 * 同一时间戳保持原顺序（稳定排序）。
 */
export function sortByUpdatedDesc<T extends { updated_at?: string | null }>(
  items: readonly T[],
): T[] {
  return items
    .map((item, index) => ({ item, index, t: new Date(item.updated_at || 0).getTime() || 0 }))
    .sort((a, b) => b.t - a.t || a.index - b.index)
    .map((x) => x.item);
}

export type DeclineCheck =
  | { ok: true; comment: string }
  | { ok: false; error: string };

/** 后端约定 comment 长度 1–2000；空/超长在本地先拦，别让用户白等一次 400 */
export function validateDeclineComment(text: string | null | undefined): DeclineCheck {
  const comment = (text || "").trim();
  if (!comment) return { ok: false, error: "请填写退回意见（1–2000 字）" };
  if (comment.length > 2000) return { ok: false, error: "退回意见最多 2000 字" };
  return { ok: true, comment };
}

/** 进度百分比：0 任务时 0，结果夹在 0–100 且取整 */
export function progressPct(approved: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0;
  const pct = Math.round((approved / total) * 100);
  return Math.min(100, Math.max(0, pct));
}
