"use client";

/**
 * C6 移动端只读工作台 /m
 *
 * 设计约束（刻意为之）：
 * - 放在 (main) 之外：手机端不加载桌面侧栏/命令面板，首屏更轻；
 * - 只读会话：作战室详情只展示，不提供发言/开工/停止等写操作；
 * - 所有请求带 `noAuthRedirect`：未登录时就地展示「请先在电脑上登录」，
 *   而不是被 api 层整页跳去 /login（手机上多半没有可用的登录态）；
 * - 20 秒静默刷新当前标签，页面隐藏即暂停，卸载清理 timer。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  describeError,
  getActiveCompanyId,
  isCancelled,
  setActiveCompanyId,
} from "@/lib/api";
import {
  approvalActionZh,
  progressPct,
  sessionStatusMeta,
  sortByUpdatedDesc,
  taskStatusZh,
  timeLabel,
  validateDeclineComment,
  type Tone,
} from "@/lib/mobile";
import { ApprovalFilePreview } from "@/components/FileChangePreview";

type Tab = "accept" | "approve" | "sessions";

type Company = { id: string; name: string };

type AwaitingItem = {
  id: string;
  title: string;
  status: string;
  agent_name?: string | null;
  updated_at?: string | null;
  artifact_id?: string | null;
  artifact_status?: string | null;
};

type Approval = {
  id: string;
  action_type: string;
  status?: string;
  reason?: string | null;
  payload?: Record<string, unknown> | null;
};

type WsSession = {
  id: string;
  goal: string;
  status: string;
  leader_name?: string | null;
  tasks?: { id: string; title: string; status: string; agent_name?: string }[];
};

type Snapshot = {
  session?: WsSession;
  team?: {
    members?: { name: string; progress_pct?: number; task_count?: number }[];
  };
  chat?: {
    messages?: {
      id: string;
      role?: string;
      agent_name?: string | null;
      text?: string;
      created_at?: string;
    }[];
  };
};

type InstallPrompt = Event & { prompt: () => Promise<void> };

type PushStatus = {
  configured: boolean;
  available?: boolean;
  public_key?: string | null;
  reason?: string | null;
};

/** base64url（VAPID 公钥）→ Uint8Array（applicationServerKey 要求） */
function urlBase64ToUint8Array(base64Url: string) {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

type PushUi =
  | "checking"
  | "unsupported"
  | "unconfigured"
  | "off"
  | "on"
  | "denied"
  | "error";

/** 浏览器是否具备 Web Push 所需的三个 API。 */
function pushSupported(): boolean {
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

const TONE_CLS: Record<Tone, string> = {
  run: "text-cyan-300 border-cyan-400/30 bg-cyan-400/10",
  ok: "text-emerald-300 border-emerald-400/30 bg-emerald-400/10",
  warn: "text-amber-300 border-amber-400/30 bg-amber-400/10",
  bad: "text-red-300 border-red-400/30 bg-red-400/10",
  idle: "text-[var(--muted)] border-[var(--border)] bg-white/5",
};

const EMPTY_TEXT: Record<Tab, string> = {
  accept: "现在没有待验收的交付物。员工交活后会出现在这里。",
  approve: "没有待审批的请求。Agent 遇到需要你点头的动作时会出现在这里。",
  sessions: "还没有作战会话。在电脑端下达作战目标后，这里可以只读查看进展。",
};

export default function MobilePage() {
  const [auth, setAuth] = useState<"checking" | "ok" | "needed">("checking");
  const [company, setCompany] = useState<Company | null>(null);
  const [bootError, setBootError] = useState("");
  const [tab, setTab] = useState<Tab>("accept");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [busy, setBusy] = useState("");
  const [inbox, setInbox] = useState<AwaitingItem[] | null>(null);
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [sessions, setSessions] = useState<WsSession[] | null>(null);
  const [declineFor, setDeclineFor] = useState("");
  const [declineText, setDeclineText] = useState("");
  const [declineError, setDeclineError] = useState("");
  const [openSid, setOpenSid] = useState("");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [snapLoading, setSnapLoading] = useState(false);
  const snapSeq = useRef(0);
  const [installEvt, setInstallEvt] = useState<InstallPrompt | null>(null);
  const [standalone, setStandalone] = useState(false);
  const [pushState, setPushState] = useState<PushUi>("checking");
  const [pushHint, setPushHint] = useState("");
  const [pushBusy, setPushBusy] = useState(false);
  const pushKeyRef = useRef<string | null>(null);

  const cid = company?.id || "";

  /* ---------- 启动：读公司（401 就地展示，不整页跳转） ---------- */
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await api.get<Company[]>("/companies", {
          silent: true,
          noAuthRedirect: true,
        });
        if (!alive) return;
        const arr = Array.isArray(list) ? list : [];
        if (arr.length === 0) {
          setAuth("ok");
          return;
        }
        const cached = getActiveCompanyId();
        const hit = arr.find((c) => c.id === cached) || arr[0];
        if (hit.id !== cached) setActiveCompanyId(hit.id);
        setCompany({ id: hit.id, name: hit.name || "我的公司" });
        setAuth("ok");
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 401) {
          setAuth("needed");
        } else {
          setAuth("ok");
          setBootError(describeError(e, "读取公司信息失败"));
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /* ---------- PWA：安装提示 + Service Worker ---------- */
  useEffect(() => {
    setStandalone(window.matchMedia("(display-mode: standalone)").matches);
    const onPrompt = (e: Event) => {
      e.preventDefault();
      setInstallEvt(e as InstallPrompt);
    };
    window.addEventListener("beforeinstallprompt", onPrompt);
    return () => window.removeEventListener("beforeinstallprompt", onPrompt);
  }, []);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const host = window.location.hostname;
    const allowed =
      process.env.NODE_ENV === "production" ||
      host === "localhost" ||
      host === "127.0.0.1";
    if (!allowed) return;
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* 注册失败不影响使用 */
    });
  }, []);

  const install = async () => {
    if (!installEvt) return;
    // BeforeInstallPromptEvent.prompt 需要绑定 this；也避开原生弹窗护栏的误报
    const promptInstall = installEvt.prompt.bind(installEvt);
    try {
      await promptInstall();
    } catch {
      /* 用户取消 */
    }
    setInstallEvt(null);
  };

  /* ---------- Web Push：状态探测 / 开启 / 关闭 ---------- */
  const readyRegistration = async (): Promise<ServiceWorkerRegistration> => {
    const reg =
      (await navigator.serviceWorker.getRegistration("/sw.js")) ||
      (await navigator.serviceWorker.register("/sw.js"));
    if (reg.active) return reg;
    // ready 在注册失败/被禁用时永不 resolve，用超时兜底，避免按钮永远转圈
    await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("service worker 未就绪")), 5000),
      ),
    ]);
    return reg;
  };

  const loadPushStatus = useCallback(async () => {
    if (!pushSupported()) {
      setPushState("unsupported");
      setPushHint(
        window.isSecureContext
          ? "当前浏览器不支持通知推送，页面内的待办列表仍会正常刷新。"
          : "通知需要 HTTPS 或 localhost 环境；当前页面无法申请权限。",
      );
      return;
    }
    setPushState("checking");
    setPushHint("");
    try {
      const st = await api.get<PushStatus>("/notifications/push/public-key", {
        silent: true,
        noAuthRedirect: true,
      });
      if (!st?.configured || !st.public_key) {
        pushKeyRef.current = null;
        setPushState("unconfigured");
        setPushHint("服务端还没有配置推送密钥，暂时无法开启手机通知。");
        return;
      }
      pushKeyRef.current = st.public_key;
      const reg = await navigator.serviceWorker.getRegistration("/sw.js");
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) {
        setPushState("on");
        setPushHint("通知已开启，任务完成和待审批会推送到这台设备。");
      } else if (Notification.permission === "denied") {
        setPushState("denied");
        setPushHint("通知已被浏览器阻止，请在浏览器的网站设置里允许通知。");
      } else {
        setPushState("off");
        setPushHint("开启后，任务完成、失败和待审批会推送到手机。");
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth("needed");
        return;
      }
      setPushState("error");
      setPushHint(describeError(e, "读取通知状态失败，请稍后重试"));
    }
  }, []);

  useEffect(() => {
    if (auth !== "ok" || !cid) return;
    void loadPushStatus();
  }, [auth, cid, loadPushStatus]);

  const enablePush = async () => {
    if (!cid || pushBusy) return;
    if (!pushSupported()) {
      setPushState("unsupported");
      return;
    }
    setPushBusy(true);
    setPushHint("");
    try {
      let key = pushKeyRef.current;
      if (!key) {
        const st = await api.get<PushStatus>("/notifications/push/public-key", {
          silent: true,
          noAuthRedirect: true,
        });
        if (!st?.configured || !st.public_key) {
          setPushState("unconfigured");
          setPushHint("服务端还没有配置推送密钥，暂时无法开启手机通知。");
          return;
        }
        key = st.public_key;
        pushKeyRef.current = key;
      }
      if (Notification.permission === "denied") {
        setPushState("denied");
        setPushHint("通知已被浏览器阻止，请在浏览器的网站设置里允许通知。");
        return;
      }
      const permission =
        Notification.permission === "granted"
          ? "granted"
          : await Notification.requestPermission();
      if (permission !== "granted") {
        setPushState("off");
        setPushHint("你暂时拒绝了通知授权，需要时可以再点开启。");
        return;
      }

      const reg = await readyRegistration();
      const sub =
        (await reg.pushManager.getSubscription()) ||
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(key),
        }));
      const json = sub.toJSON();
      const endpoint = json.endpoint || sub.endpoint;
      const p256dh = json.keys?.p256dh;
      const auth = json.keys?.auth;
      if (!endpoint || !p256dh || !auth) {
        throw new Error("浏览器没有返回完整的订阅信息");
      }
      await api.post(
        "/notifications/push/subscribe",
        { company_id: cid, endpoint, keys: { p256dh, auth } },
        { silent: true, noAuthRedirect: true },
      );
      setPushState("on");
      setPushHint("通知已开启，任务完成和待审批会推送到这台设备。");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth("needed");
        return;
      }
      setPushState("error");
      setPushHint(describeError(e, "开启通知失败，请稍后重试"));
    } finally {
      setPushBusy(false);
    }
  };

  const disablePush = async () => {
    if (!cid || pushBusy) return;
    setPushBusy(true);
    setPushHint("");
    try {
      const reg = await navigator.serviceWorker.getRegistration("/sw.js");
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) {
        const query =
          `company_id=${encodeURIComponent(cid)}` +
          `&endpoint=${encodeURIComponent(sub.endpoint)}`;
        await api.del(`/notifications/push/unsubscribe?${query}`, {
          silent: true,
          noAuthRedirect: true,
        });
        await sub.unsubscribe();
      }
      setPushState("off");
      setPushHint("已关闭通知；页面内的待办列表仍会正常刷新。");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth("needed");
        return;
      }
      setPushState("error");
      setPushHint(describeError(e, "关闭通知失败，请稍后重试"));
    } finally {
      setPushBusy(false);
    }
  };

  /* ---------- 当前标签的数据拉取 ---------- */
  const load = useCallback(
    async (silent: boolean, signal?: AbortSignal): Promise<boolean> => {
      if (!cid) return false;
      if (!silent) {
        setLoading(true);
        setError("");
      }
      try {
        if (tab === "accept") {
          const d = await api.get<{ awaiting_acceptance?: AwaitingItem[] }>(
            `/tasks/inbox?company_id=${cid}`,
            { signal, silent: true, noAuthRedirect: true },
          );
          setInbox(sortByUpdatedDesc(d?.awaiting_acceptance || []));
        } else if (tab === "approve") {
          const d = await api.get<Approval[]>(
            `/intervention/requests?company_id=${cid}&status=PENDING`,
            { signal, silent: true, noAuthRedirect: true },
          );
          setApprovals(Array.isArray(d) ? d : []);
        } else {
          const d = await api.get<{ sessions?: WsSession[] }>(
            `/war-room/sessions?company_id=${cid}`,
            { signal, silent: true, noAuthRedirect: true },
          );
          setSessions(Array.isArray(d?.sessions) ? d!.sessions! : []);
        }
        setError("");
        return true;
      } catch (e) {
        if (isCancelled(e)) return false;
        if (e instanceof ApiError && e.status === 401) {
          setAuth("needed");
          return false;
        }
        setError(describeError(e, "加载失败，请点右上角刷新重试"));
        return false;
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [cid, tab],
  );

  /* ---------- 20s 静默刷新；隐藏暂停；卸载清理 ---------- */
  useEffect(() => {
    if (auth !== "ok" || !cid) return;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    const start = () => {
      if (!timer) timer = setInterval(() => void load(true, ctrl.signal), 20_000);
    };
    void load(false, ctrl.signal);
    if (document.visibilityState !== "hidden") start();
    const onVis = () => {
      if (document.visibilityState === "hidden") {
        stop();
      } else {
        void load(true, ctrl.signal);
        start();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
      ctrl.abort();
    };
  }, [auth, cid, tab, load]);

  // 切标签时收起详情与退回输入，避免串台
  useEffect(() => {
    setOpenSid("");
    setSnap(null);
    setDeclineFor("");
    setDeclineText("");
    setDeclineError("");
  }, [tab]);

  // 成功提示 4 秒自动消失
  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(""), 4000);
    return () => clearTimeout(t);
  }, [flash]);

  /* ---------- 操作：验收 / 退回 / 审批 ---------- */
  const act = async (
    key: string,
    fn: () => Promise<unknown>,
    okMsg: string,
  ): Promise<boolean> => {
    setBusy(key);
    setFlash("");
    try {
      await fn();
      setFlash(okMsg);
      setError("");
      await load(true);
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth("needed");
        return false;
      }
      setError(describeError(e, "操作失败，请重试"));
      return false;
    } finally {
      setBusy("");
    }
  };

  const accept = (artifactId: string) =>
    act(
      `accept:${artifactId}`,
      () =>
        api.post(`/artifacts/${artifactId}/accept`, undefined, {
          silent: true,
          noAuthRedirect: true,
        }),
      "已验收通过，任务结案",
    );

  const decline = async (artifactId: string) => {
    const check = validateDeclineComment(declineText);
    if (!check.ok) {
      setDeclineError(check.error);
      return;
    }
    setDeclineError("");
    const ok = await act(
      `decline:${artifactId}`,
      () =>
        api.post(
          `/artifacts/${artifactId}/decline`,
          { comment: check.comment },
          { silent: true, noAuthRedirect: true },
        ),
      "已退回，等待员工返工",
    );
    if (ok) {
      setDeclineFor("");
      setDeclineText("");
    }
  };

  const decide = (id: string, decision: "approve" | "reject") =>
    act(
      `decide:${id}`,
      () =>
        api.post(
          `/intervention/requests/${id}/decide`,
          { decision },
          { silent: true, noAuthRedirect: true },
        ),
      decision === "approve" ? "已批准，Agent 将继续执行" : "已驳回该请求",
    );

  /* ---------- 会话只读详情 ---------- */
  const toggleSession = useCallback(
    async (sid: string) => {
      if (openSid === sid) {
        setOpenSid("");
        setSnap(null);
        return;
      }
      const seq = ++snapSeq.current;
      setOpenSid(sid);
      setSnap(null);
      setSnapLoading(true);
      try {
        const d = await api.get<Snapshot>(
          `/war-room/sessions/${sid}/snapshot?company_id=${cid}`,
          { silent: true, noAuthRedirect: true },
        );
        if (seq === snapSeq.current) setSnap(d);
      } catch (e) {
        if (isCancelled(e)) return;
        if (e instanceof ApiError && e.status === 401) {
          setAuth("needed");
          return;
        }
        if (seq === snapSeq.current) setError(describeError(e, "读取会话详情失败"));
      } finally {
        if (seq === snapSeq.current) setSnapLoading(false);
      }
    },
    [cid, openSid],
  );

  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "accept", label: "待验收", count: inbox?.length ?? 0 },
    { key: "approve", label: "待审批", count: approvals?.length ?? 0 },
    { key: "sessions", label: "会话", count: sessions?.length ?? 0 },
  ];

  const list = tab === "accept" ? inbox : tab === "approve" ? approvals : sessions;

  return (
    <div className="mx-auto min-h-screen w-full max-w-md border-x border-[var(--border)] bg-[var(--background)]">
      <header className="sticky top-0 z-20 border-b border-[var(--border)] bg-[rgba(9,9,15,0.86)] backdrop-blur">
        <div className="flex items-center gap-3 px-4 pb-3 pt-[calc(0.75rem+env(safe-area-inset-top))]">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--muted)]">
              AI-EOS 移动端
            </div>
            <h1 className="truncate text-base font-semibold">
              <span className="text-gradient">
                {company?.name ||
                  (auth === "checking" ? "正在读取公司…" : "AI-EOS")}
              </span>
            </h1>
          </div>
          <button
            type="button"
            className="btn-ghost shrink-0 text-xs"
            disabled={loading || auth !== "ok"}
            onClick={() => void load(false)}
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </header>

      <main className="px-3 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-3">
        {!standalone && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-[11px] leading-relaxed text-[var(--muted)]">
            <span className="flex-1">
              {installEvt
                ? "把它装到桌面，像 App 一样打开。"
                : "安装提示：在浏览器菜单中选择「添加到主屏幕 / 安装应用」。"}
            </span>
            {installEvt && (
              <button
                type="button"
                className="btn-ghost shrink-0 text-[11px]"
                onClick={() => void install()}
              >
                安装到桌面
              </button>
            )}
          </div>
        )}

        {auth === "ok" && company && (
          <section className="mb-3 flex items-start gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium">
                手机通知
                {pushState === "on" && (
                  <span className="ml-1.5 text-[10px] text-emerald-300">
                    已开启
                  </span>
                )}
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-[var(--muted)]">
                {pushHint ||
                  (pushState === "checking"
                    ? "正在检查通知状态…"
                    : "任务完成、失败和待审批可直接推送到手机。")}
              </p>
            </div>
            {pushState === "on" ? (
              <button
                type="button"
                className="btn-ghost shrink-0 text-[11px]"
                disabled={pushBusy}
                onClick={() => void disablePush()}
              >
                {pushBusy ? "处理中…" : "关闭通知"}
              </button>
            ) : pushState === "checking" ? (
              <span className="shrink-0 text-[11px] text-[var(--muted)]">
                检查中…
              </span>
            ) : pushState === "off" || pushState === "denied" ? (
              <button
                type="button"
                className="btn-primary shrink-0 !py-1.5 text-[11px]"
                disabled={pushBusy}
                onClick={() => void enablePush()}
              >
                {pushBusy ? "开启中…" : "开启通知"}
              </button>
            ) : pushState === "error" ? (
              <button
                type="button"
                className="btn-ghost shrink-0 text-[11px]"
                disabled={pushBusy}
                onClick={() => void loadPushStatus()}
              >
                重试
              </button>
            ) : null}
          </section>
        )}

        {flash && (
          <div
            role="status"
            className="mb-3 rounded-lg border border-emerald-400/40 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-200"
          >
            {flash}
          </div>
        )}
        {error && (
          <div
            role="alert"
            className="mb-3 flex items-start gap-2 rounded-lg border border-red-400/40 bg-red-400/10 px-3 py-2 text-xs text-red-200"
          >
            <span className="flex-1 break-words">{error}</span>
            <button
              type="button"
              className="shrink-0 underline"
              onClick={() => void load(true)}
            >
              重试
            </button>
          </div>
        )}
        {bootError && (
          <div className="mb-3 rounded-lg border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-xs text-amber-200">
            {bootError}
          </div>
        )}

        {auth === "checking" && (
          <div className="mt-6 text-center text-xs text-[var(--muted)]">
            正在确认登录状态…
          </div>
        )}

        {auth === "needed" && (
          <section className="card card-static mt-6 text-center">
            <div className="text-sm font-semibold">需要在电脑上登录</div>
            <p className="mt-2 text-xs leading-relaxed text-[var(--muted)]">
              请先在电脑上登录，再用同一地址打开。手机浏览器不会自动继承电脑的登录状态。
            </p>
            <Link href="/login" className="btn-primary mt-4 inline-block text-xs">
              前往登录页
            </Link>
          </section>
        )}

        {auth === "ok" && !company && !bootError && (
          <section className="card card-static mt-6 text-center">
            <div className="text-sm font-semibold">还没有公司</div>
            <p className="mt-2 text-xs leading-relaxed text-[var(--muted)]">
              当前账号名下没有 AI 公司，请先在电脑端完成初始化，再回到这里。
            </p>
          </section>
        )}

        {auth === "ok" && company && (
          <>
            {list === null && loading && (
              <div className="space-y-3">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="h-20 animate-pulse rounded-xl border border-[var(--border)] bg-white/[0.03]"
                  />
                ))}
              </div>
            )}

            {list !== null && list.length === 0 && !loading && (
              <p className="mt-10 px-6 text-center text-xs leading-relaxed text-[var(--muted)]">
                {EMPTY_TEXT[tab]}
              </p>
            )}

            {tab === "accept" && (inbox?.length ?? 0) > 0 && (
              <div className="space-y-3">
                {inbox!.map((it) => (
                  <article key={it.id} className="card card-static !p-3">
                    <h2 className="break-words text-sm font-medium leading-snug">
                      {it.title}
                    </h2>
                    <p className="mt-1 text-[11px] text-[var(--muted)]">
                      {it.agent_name || "未指派负责人"} · {timeLabel(it.updated_at)} ·{" "}
                      {taskStatusZh(it.status)}
                    </p>
                    {it.artifact_id ? (
                      declineFor === it.artifact_id ? (
                        <div className="mt-2 space-y-2">
                          <textarea
                            value={declineText}
                            onChange={(e) => {
                              setDeclineText(e.target.value);
                              if (declineError) setDeclineError("");
                            }}
                            maxLength={2000}
                            rows={3}
                            placeholder="退回原因（1–2000 字），员工会据此返工"
                            className="input resize-none text-xs"
                          />
                          <div className="flex items-center justify-between text-[10px] text-[var(--muted)]">
                            <span>{declineText.trim().length}/2000</span>
                            {declineError && (
                              <span className="text-[var(--danger)]">{declineError}</span>
                            )}
                          </div>
                          <div className="flex gap-2">
                            <button
                              type="button"
                              className="btn-ghost flex-1 text-xs"
                              onClick={() => {
                                setDeclineFor("");
                                setDeclineText("");
                                setDeclineError("");
                              }}
                            >
                              取消
                            </button>
                            <button
                              type="button"
                              className="btn-primary flex-1 !py-1.5 text-xs"
                              disabled={!!busy}
                              onClick={() => void decline(it.artifact_id!)}
                            >
                              {busy === `decline:${it.artifact_id}` ? "提交中…" : "确认退回"}
                            </button>
                          </div>
                        </div>
                      ) : it.artifact_status && it.artifact_status !== "APPROVED" ? (
                        <p className="mt-2 text-[11px] text-[var(--muted)]">
                          成果还在审核中，审核通过后才能验收或退回。
                        </p>
                      ) : (
                        <div className="mt-3 flex gap-2">
                          <button
                            type="button"
                            className="btn-primary flex-1 !py-1.5 text-xs"
                            disabled={!!busy}
                            onClick={() => void accept(it.artifact_id!)}
                          >
                            {busy === `accept:${it.artifact_id}` ? "处理中…" : "验收通过"}
                          </button>
                          <button
                            type="button"
                            className="btn-ghost flex-1 text-xs"
                            disabled={!!busy}
                            onClick={() => {
                              setDeclineFor(it.artifact_id!);
                              setDeclineText("");
                              setDeclineError("");
                            }}
                          >
                            退回修改
                          </button>
                        </div>
                      )
                    ) : (
                      <p className="mt-2 text-[11px] text-[var(--muted)]">
                        交付物还在生成，暂时无法验收。
                      </p>
                    )}
                  </article>
                ))}
              </div>
            )}

            {tab === "approve" && (approvals?.length ?? 0) > 0 && (
              <div className="space-y-3">
                {approvals!.map((r) => (
                  <article key={r.id} className="card card-static !p-3">
                    <h2 className="text-sm font-medium">{approvalActionZh(r.action_type)}</h2>
                    <p className="mt-1 break-words text-[11px] leading-relaxed text-[var(--muted)]">
                      {r.reason || "未填写原因"}
                    </p>
                    <ApprovalFilePreview payload={r.payload} maxHeightClass="max-h-40" />
                    <div className="mt-3 flex gap-2">
                      <button
                        type="button"
                        className="btn-primary flex-1 !py-1.5 text-xs"
                        disabled={!!busy}
                        onClick={() => void decide(r.id, "approve")}
                      >
                        {busy === `decide:${r.id}` ? "处理中…" : "批准"}
                      </button>
                      <button
                        type="button"
                        className="btn-ghost flex-1 text-xs"
                        disabled={!!busy}
                        onClick={() => void decide(r.id, "reject")}
                      >
                        驳回
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}

            {tab === "sessions" && (sessions?.length ?? 0) > 0 && (
              <div className="space-y-3">
                {sessions!.map((s) => {
                  const meta = sessionStatusMeta(s.status);
                  const open = openSid === s.id;
                  const tasks = snap?.session?.tasks || [];
                  const approved = tasks.filter((t) => t.status === "approved").length;
                  const pct = progressPct(approved, tasks.length);
                  const msgs = (snap?.chat?.messages || []).slice(-20);
                  return (
                    <article
                      key={s.id}
                      className="card card-static overflow-hidden !p-0"
                    >
                      <button
                        type="button"
                        className="w-full px-3 py-3 text-left"
                        aria-expanded={open}
                        onClick={() => void toggleSession(s.id)}
                      >
                        <div className="flex items-start gap-2">
                          <span className="min-w-0 flex-1 break-words text-sm font-medium leading-snug">
                            {s.goal}
                          </span>
                          <span className={`badge shrink-0 ${TONE_CLS[meta.tone]}`}>
                            {meta.label}
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] text-[var(--muted)]">
                          负责人 {s.leader_name || "待定"} · {open ? "收起" : "查看只读详情"}
                        </p>
                      </button>
                      {open && (
                        <div className="border-t border-[var(--border)] px-3 py-3">
                          {snapLoading && (
                            <p className="text-xs text-[var(--muted)]">正在读取…</p>
                          )}
                          {!snapLoading && snap && (
                            <>
                              <div className="flex items-center justify-between text-[11px] text-[var(--muted)]">
                                <span>任务进度</span>
                                <span>
                                  {approved}/{tasks.length} · {pct}%
                                </span>
                              </div>
                              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/10">
                                <div
                                  className="h-full rounded-full bg-gradient-to-r from-indigo-400 to-cyan-300"
                                  style={{ width: `${pct}%` }}
                                />
                              </div>

                              {(snap.team?.members?.length ?? 0) > 0 && (
                                <div className="mt-3 flex flex-wrap gap-1.5">
                                  {snap.team!.members!.map((m, i) => (
                                    <span
                                      key={`${m.name}-${i}`}
                                      className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)]"
                                    >
                                      {m.name} {m.progress_pct ?? 0}%
                                    </span>
                                  ))}
                                </div>
                              )}

                              {tasks.length > 0 && (
                                <ul className="mt-3 space-y-1">
                                  {tasks.slice(0, 8).map((t) => (
                                    <li
                                      key={t.id}
                                      className="flex items-center gap-2 text-[11px]"
                                    >
                                      <span className="min-w-0 flex-1 truncate">
                                        {t.title}
                                      </span>
                                      <span className="shrink-0 text-[var(--muted)]">
                                        {t.agent_name || ""} · {taskStatusZh(t.status)}
                                      </span>
                                    </li>
                                  ))}
                                </ul>
                              )}

                              {msgs.length > 0 && (
                                <div className="mt-3 space-y-2">
                                  {msgs.map((m) => (
                                    <div
                                      key={m.id}
                                      className="rounded-lg bg-white/[0.03] px-2.5 py-2"
                                    >
                                      <div className="text-[10px] text-[var(--muted)]">
                                        {m.agent_name || (m.role === "user" ? "老板" : "系统")} ·{" "}
                                        {timeLabel(m.created_at)}
                                      </div>
                                      <div className="mt-0.5 whitespace-pre-wrap break-words text-[12px] leading-relaxed">
                                        {m.text}
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              )}

                              <p className="mt-3 border-t border-[var(--border)] pt-2 text-[10px] text-[var(--muted)]">
                                只读视图：发言、开工、停止等操作请回电脑端。
                              </p>
                            </>
                          )}
                          {!snapLoading && !snap && (
                            <p className="text-xs text-[var(--muted)]">
                              详情暂时读不到，请稍后重试。
                            </p>
                          )}
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </>
        )}
      </main>

      <nav
        aria-label="移动端标签栏"
        className="fixed inset-x-0 bottom-0 z-30 mx-auto max-w-md border-t border-[var(--border)] bg-[rgba(16,16,24,0.94)] backdrop-blur"
      >
        <div className="flex pb-[env(safe-area-inset-bottom)]">
          {tabs.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                type="button"
                aria-current={active ? "page" : undefined}
                className={`relative flex-1 py-3 text-xs font-medium transition-colors ${
                  active ? "text-[var(--accent-2)]" : "text-[var(--muted)]"
                }`}
                onClick={() => setTab(t.key)}
              >
                {active && (
                  <span className="absolute inset-x-5 top-0 h-0.5 rounded-full bg-gradient-to-r from-indigo-400 to-cyan-300" />
                )}
                {t.label}
                {t.count > 0 && (
                  <span className="ml-1 rounded-full bg-white/10 px-1.5 py-0.5 text-[10px]">
                    {t.count}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
