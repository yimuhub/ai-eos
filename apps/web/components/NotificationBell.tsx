"use client";

// 顶栏通知铃铛（C4）。
//
// ## 最小侵入
// 只有"图标 + 未读徽标 + 下拉列表"，不新增页面、不改导航结构。
// 样式沿用现有 CSS 变量（--surface / --border / --muted / --accent）。
//
// ## 为什么桌面通知只在点击后申请权限
// 浏览器对 `Notification.requestPermission()` 的最佳实践是"由用户手势触发"：
// 页面一加载就弹权限框，绝大多数人会直接拒绝，而且拒绝后无法再问。
// 所以这里只在用户主动点开铃铛时、且权限仍是 default 时申请一次；
// 不注册 service worker（站内通知不需要后台推送）。

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { describeError, getActiveCompanyId } from "@/lib/api";
import { notify as toast } from "@/components/Toast";
import {
  type AppNotification,
  fetchNotifications,
  fetchUnreadCount,
  markAllRead,
  markRead,
  startNotificationPolling,
} from "@/lib/notifications";

const TYPE_ICON: Record<string, string> = {
  "task.completed": "✅",
  "task.failed": "⛔",
  "approval.pending": "📝",
  "meeting.minutes": "📋",
};

function timeLabel(iso: string | null): string {
  if (!iso) return "";
  // 后端存的是 naive UTC，isoformat 不带时区；不补 Z 会被当成本地时间，
  // 东八区用户会看到"8 小时前"这种错误相对时间。
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function NotificationBell({
  align = "right",
  className = "",
}: {
  /** 下拉对齐方向：右侧栏用 right，左侧栏用 left（避免弹出屏幕外） */
  align?: "left" | "right";
  className?: string;
}) {
  const router = useRouter();
  const [cid, setCid] = useState<string | null>(null);
  const [count, setCount] = useState(0);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<string | null>(null);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const prevCountRef = useRef(0);
  const primedRef = useRef(false);

  // 公司切换（自定义事件 / 其它标签页写 localStorage）都要重新绑定
  useEffect(() => {
    const sync = () => setCid(getActiveCompanyId());
    sync();
    window.addEventListener("aieos-company-changed", sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener("aieos-company-changed", sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const maybeDesktopNotify = useCallback((next: number) => {
    const prev = prevCountRef.current;
    prevCountRef.current = next;
    if (!primedRef.current) {
      // 首轮只记基线：刚打开页面不该被历史未读弹一脸
      primedRef.current = true;
      return;
    }
    if (next <= prev) return;
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (window.Notification.permission !== "granted") return;
    // 页面可见时下拉/徽标已经能看见，不再叠加系统通知
    if (typeof document !== "undefined" && document.visibilityState === "visible") return;
    try {
      new window.Notification("AI-EOS 新通知", {
        body: `你有 ${next} 条未读通知`,
        tag: "aieos-notifications",
      });
    } catch {
      /* 部分桌面壳禁用系统通知：静默降级为站内徽标 */
    }
  }, []);

  const refreshCount = useCallback(async () => {
    if (!cid) return;
    try {
      const next = await fetchUnreadCount(cid);
      setCount(next);
      maybeDesktopNotify(next);
    } catch {
      /* 轮询失败保持旧值，不打扰用户 */
    }
  }, [cid, maybeDesktopNotify]);

  useEffect(() => {
    if (!cid) {
      setCount(0);
      setItems([]);
      return;
    }
    void refreshCount();
    return startNotificationPolling(() => {
      void refreshCount();
    });
  }, [cid, refreshCount]);

  const loadList = useCallback(async () => {
    if (!cid) return;
    setLoading(true);
    try {
      setItems(await fetchNotifications(cid, { limit: 20 }));
    } catch (e) {
      toast("error", describeError(e, "通知加载失败"));
    } finally {
      setLoading(false);
    }
  }, [cid]);

  // 桌面通知权限：只在"用户点击铃铛"这一手势里申请一次
  const askPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (window.Notification.permission !== "default") return;
    try {
      await window.Notification.requestPermission();
    } catch {
      /* 浏览器不支持/被策略拦截：站内徽标照常工作 */
    }
  }, []);

  const onToggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      void loadList();
      void askPermission();
    }
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const onItem = async (n: AppNotification) => {
    if (acting) return;
    try {
      if (!n.is_read) {
        setActing(n.id);
        await markRead(n.id);
        setItems((prev) => prev.map((x) => (x.id === n.id ? { ...x, is_read: true } : x)));
        setCount((c) => {
          const next = Math.max(0, c - 1);
          prevCountRef.current = next;
          return next;
        });
      }
      if (n.link) {
        setOpen(false);
        router.push(n.link);
      }
    } catch (e) {
      toast("error", describeError(e, "标记已读失败"));
    } finally {
      setActing(null);
    }
  };

  const onReadAll = async () => {
    if (!cid) return;
    try {
      await markAllRead(cid);
      setItems((prev) => prev.map((x) => ({ ...x, is_read: true })));
      setCount(0);
      prevCountRef.current = 0;
    } catch (e) {
      toast("error", describeError(e, "全部已读失败"));
    }
  };

  // 没有激活公司时静默不显示：刚登录/安装向导里还没有公司语境
  if (!cid) return null;

  const badge = count > 99 ? "99+" : String(count);
  const unread = items.filter((n) => !n.is_read).length;

  return (
    <div ref={rootRef} className={`relative shrink-0 ${className}`}>
      <button
        type="button"
        onClick={onToggle}
        aria-label={count > 0 ? `通知，${count} 条未读` : "通知"}
        aria-haspopup="menu"
        aria-expanded={open}
        className="relative w-8 h-8 rounded-lg flex items-center justify-center transition-colors hover:bg-white/[0.06]"
        style={{ color: "var(--muted)" }}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {count > 0 && (
          <span
            data-testid="notification-badge"
            className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-1 rounded-full text-[9px] font-semibold flex items-center justify-center"
            style={{ background: "#ef4444", color: "#fff", lineHeight: 1 }}
          >
            {badge}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className={`absolute top-full mt-1.5 w-[336px] rounded-xl shadow-2xl overflow-hidden ${align === "left" ? "left-0" : "right-0"}`}
          style={{ background: "var(--surface)", border: "1px solid var(--border)", zIndex: 60 }}
        >
          <div className="flex items-center gap-2 px-3.5 py-2.5"
            style={{ borderBottom: "1px solid var(--border)" }}>
            <span className="text-xs font-semibold">通知</span>
            {unread > 0 && (
              <span className="text-[10px]" style={{ color: "var(--muted)" }}>{unread} 条未读</span>
            )}
            <button
              type="button"
              onClick={onReadAll}
              disabled={count === 0}
              className="ml-auto text-[11px] px-2 py-1 rounded-md transition-colors hover:bg-white/[0.06] disabled:opacity-40"
              style={{ color: "var(--muted)" }}
            >
              全部已读
            </button>
          </div>

          <div className="max-h-[360px] overflow-y-auto ws-scroll">
            {loading && items.length === 0 ? (
              <p className="px-4 py-8 text-center text-[11px]" style={{ color: "var(--muted)" }}>
                正在加载…
              </p>
            ) : items.length === 0 ? (
              <p className="px-4 py-8 text-center text-[11px]" style={{ color: "var(--muted)" }}>
                暂无通知。任务完成、失败和待审批会出现在这里。
              </p>
            ) : (
              items.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  role="menuitem"
                  onClick={() => onItem(n)}
                  disabled={acting === n.id}
                  className="w-full text-left px-3.5 py-2.5 transition-colors hover:bg-white/[0.04] disabled:opacity-60"
                  style={{ borderBottom: "1px solid rgba(255,255,255,0.04)" }}
                >
                  <div className="flex items-start gap-2">
                    <span className="text-[13px] shrink-0 mt-0.5" aria-hidden>
                      {TYPE_ICON[n.type] || "🔔"}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <p className="text-[12px] font-medium truncate"
                          style={{ color: n.is_read ? "var(--muted)" : "var(--foreground, #e5e7eb)" }}>
                          {n.title}
                        </p>
                        {!n.is_read && (
                          <span className="w-1.5 h-1.5 rounded-full shrink-0"
                            style={{ background: "var(--accent)" }} aria-label="未读" />
                        )}
                      </div>
                      {n.body && (
                        <p className="text-[10.5px] mt-0.5 leading-snug"
                          style={{
                            color: "var(--muted)",
                            display: "-webkit-box",
                            WebkitLineClamp: 2,
                            WebkitBoxOrient: "vertical",
                            overflow: "hidden",
                          }}>
                          {n.body}
                        </p>
                      )}
                      <p className="text-[9.5px] mt-1" style={{ color: "#55555f" }}>
                        {timeLabel(n.created_at)}
                      </p>
                    </div>
                  </div>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
