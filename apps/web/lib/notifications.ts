// 站内通知（C4）前端数据层。
//
// 只做三件事：拉列表 / 拉未读数 / 标记已读。全部复用 `lib/api.ts`，
// 这样 token 刷新、超时、错误规范化、公司上下文都跟其它页面完全一致。
//
// 轮询刻意放在这里而不是组件里：间隔、页面隐藏时暂停、接口路径
// 都只有这一份定义，避免顶栏和页头各写一套导致双倍请求。

import { api, getActiveCompanyId, type RequestOptions } from "@/lib/api";

export type NotificationType =
  | "task.completed"
  | "task.failed"
  | "approval.pending"
  | "meeting.minutes"
  | string;

export type AppNotification = {
  id: string;
  company_id: string;
  type: NotificationType;
  title: string;
  body: string | null;
  link: string | null;
  is_read: boolean;
  created_at: string | null;
};

/** 轮询间隔：30s。再密只会增加空请求，通知本身不是秒级业务。 */
export const NOTIFICATION_POLL_INTERVAL_MS = 30_000;

function scopeId(companyId?: string | null): string | null {
  return companyId || getActiveCompanyId();
}

export async function fetchUnreadCount(
  companyId?: string | null,
  config?: RequestOptions,
): Promise<number> {
  const cid = scopeId(companyId);
  if (!cid) return 0;
  const data = await api.get<{ count: number }>(
    `/notifications/unread-count?company_id=${encodeURIComponent(cid)}`,
    // 轮询失败不弹全局错误：后端短暂不可用不该每 30 秒吵一次
    { silent: true, ...config },
  );
  return Number(data?.count || 0) || 0;
}

export async function fetchNotifications(
  companyId?: string | null,
  opts: { unreadOnly?: boolean; limit?: number } & RequestOptions = {},
): Promise<AppNotification[]> {
  const cid = scopeId(companyId);
  if (!cid) return [];
  const { unreadOnly = false, limit = 20, ...config } = opts;
  const params = new URLSearchParams({
    company_id: cid,
    limit: String(Math.min(Math.max(limit, 1), 50)),
  });
  if (unreadOnly) params.set("unread_only", "true");
  const rows = await api.get<AppNotification[]>(
    `/notifications?${params.toString()}`,
    { silent: true, ...config },
  );
  return Array.isArray(rows) ? rows : [];
}

export async function markRead(id: string, config?: RequestOptions): Promise<void> {
  await api.post(`/notifications/${encodeURIComponent(id)}/read`, undefined, config);
}

export async function markAllRead(
  companyId?: string | null,
  config?: RequestOptions,
): Promise<number> {
  const cid = scopeId(companyId);
  if (!cid) return 0;
  const data = await api.post<{ updated: number }>(
    `/notifications/read-all?company_id=${encodeURIComponent(cid)}`,
    undefined,
    config,
  );
  return Number(data?.updated || 0) || 0;
}

/**
 * 启动轮询，返回停止函数。
 *
 * - 页面不可见（切标签/最小化）时**不轮询**，回来时立即补一次；
 * - `document` 不存在（SSR）时直接返回空停止函数，调用方无需判断环境。
 */
export function startNotificationPolling(
  onTick: () => void,
  intervalMs: number = NOTIFICATION_POLL_INTERVAL_MS,
): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return () => {};
  }
  // 硬下限：任何调用方都别把间隔调到 30s 以下
  const every = Math.max(intervalMs, NOTIFICATION_POLL_INTERVAL_MS);
  let timer: ReturnType<typeof setInterval> | null = null;

  const start = () => {
    if (timer === null) timer = setInterval(onTick, every);
  };
  const stop = () => {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") {
      onTick();
      start();
    } else {
      stop();
    }
  };

  document.addEventListener("visibilitychange", onVisibility);
  if (document.visibilityState === "visible") start();

  return () => {
    stop();
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
