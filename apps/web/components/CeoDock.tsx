"use client";

/**
 * 常驻的 CEO：右下角商务小人 + 对话抽屉。
 *
 * 产品约定（与后端 /ceo/companion、/ceo/threads 一一对应）：
 * 1. 只要公司指定了 CEO，小人就出现在任何页面；没指定 CEO 时完全不渲染。
 * 2. 点击小人打开抽屉，随时提问/下指令，不必先跳到 /ceo。
 * 3. 对话永久保存，按线程归档；滚动摘要把老对话压成长期记忆，省 token。
 * 4. 默认 ⌘⇧K / Ctrl+Shift+K 唤起，可在抽屉里改成别的组合。
 * 5. 小人可拖动，位置记在本机；不动它时轻微呼吸、眨眼、悬停挥手。
 *
 * 复用 `/agents/{aid}/chat/stream` 发消息（payload 带 thread_id），
 * 与员工私聊、工作台共用同一条流式链路，不另造一套对话协议。
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import Link from "next/link";
import { api, describeError, isCancelled, streamPost } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import {
  CEO_SHORTCUT_STORE,
  canonicalCombo,
  comboLabel,
  defaultCeoShortcut,
  eventCombo,
  isMacPlatform,
  readCeoShortcut,
} from "@/lib/ceoShortcut";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";
import CeoPet from "@/components/CeoPet";

type Companion = {
  has_ceo: boolean;
  company_id: string;
  ceo_agent_id: string | null;
  ceo_agent_name: string | null;
  ceo_role: string | null;
  thread_id: string | null;
  thread_title: string | null;
  summary: string | null;
  message_count: number;
  last_message_at: string | null;
  today_tokens: number;
  today_cost: number;
  budget_paused: boolean;
  quick_prompts: string[];
};

type Thread = {
  id: string;
  title: string;
  summary: string | null;
  message_count: number;
  last_message_at: string | null;
  created_at: string | null;
};

type Msg = {
  id?: string;
  role: string;
  content: string;
  thread_id?: string | null;
  created_at?: string;
  streaming?: boolean;
};

type Pos = { right: number; bottom: number };

const POS_STORE = "aieos_ceo_pet_pos";
const MODIFIER_KEYS = ["Meta", "Control", "Alt", "Shift"];

function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

function fmtCost(n: number): string {
  if (!n) return "¥0";
  return n < 0.01 ? `¥${n.toFixed(4)}` : `¥${n.toFixed(2)}`;
}

export default function CeoDock() {
  const cid = useActiveCompanyId();
  const companion = useResource<Companion>(
    cid ? `ceo:companion:${cid}` : null,
    () => api.get<Companion>("/ceo/companion"),
    { staleMs: 30_000 },
  );
  const data = companion.data;
  const refreshCompanion = companion.refresh;
  const hasCeo = !!data?.has_ceo;

  const [open, setOpen] = useState(false);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [summary, setSummary] = useState("");
  const [msgsLoading, setMsgsLoading] = useState(false);
  const [threadsBusy, setThreadsBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");
  const [input, setInput] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [shortcut, setShortcut] = useState("");
  const [isMac, setIsMac] = useState(false);
  const [unread, setUnread] = useState(false);
  const [hop, setHop] = useState(false);

  const [pos, setPos] = useState<Pos>({ right: 20, bottom: 24 });
  const [dragging, setDragging] = useState(false);

  const threadIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const recordingRef = useRef(false);
  const suppressClickRef = useRef(false);
  const dragRef = useRef<{
    startX: number;
    startY: number;
    right: number;
    bottom: number;
    moved: boolean;
    pointerId: number;
  } | null>(null);
  const petWrapRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const threadsGen = useRef(0);
  const msgsGen = useRef(0);

  recordingRef.current = recording;

  const setThread = useCallback((id: string | null) => {
    threadIdRef.current = id;
    setThreadId(id);
  }, []);

  const loadThreads = useCallback(async (silent = false) => {
    const gen = ++threadsGen.current;
    if (!silent) setThreadsBusy(true);
    try {
      const d = await api.get<{ threads: Thread[] }>("/ceo/threads");
      if (gen !== threadsGen.current) return;
      const list = Array.isArray(d.threads) ? d.threads : [];
      setThreads(list);
      const current = threadIdRef.current;
      if (!current || !list.some((t) => t.id === current)) {
        setThread(list[0]?.id ?? null);
      }
    } catch (e) {
      if (gen === threadsGen.current && !silent) {
        setErr(describeError(e, "读取对话列表失败"));
      }
    } finally {
      if (gen === threadsGen.current) setThreadsBusy(false);
    }
  }, [setThread]);

  const loadMessages = useCallback(async (tid: string, silent = false) => {
    const gen = ++msgsGen.current;
    if (!silent) {
      setMsgsLoading(true);
      setErr("");
    }
    try {
      const d = await api.get<{ messages: Msg[]; summary?: string | null }>(
        `/ceo/threads/${tid}/messages?limit=200`,
      );
      if (gen !== msgsGen.current) return;
      setMsgs(Array.isArray(d.messages) ? d.messages.map((m) => ({ ...m })) : []);
      setSummary(d.summary || "");
    } catch (e) {
      if (gen === msgsGen.current && !silent) {
        setErr(describeError(e, "读取对话失败"));
      }
    } finally {
      if (gen === msgsGen.current) setMsgsLoading(false);
    }
  }, []);

  /* ---------- 公司 / CEO 切换 ---------- */

  useEffect(() => {
    setIsMac(isMacPlatform());
    setShortcut(readCeoShortcut());
    try {
      const raw = localStorage.getItem(POS_STORE);
      if (raw) {
        const p = JSON.parse(raw);
        if (Number.isFinite(p?.right) && Number.isFinite(p?.bottom)) {
          setPos({ right: p.right, bottom: p.bottom });
        }
      }
    } catch {
      /* 位置坏了就用默认值 */
    }
  }, []);

  useEffect(() => {
    if (!data) return;
    if (!data.has_ceo) {
      setThread(null);
      setThreads([]);
      setMsgs([]);
      setSummary("");
      setOpen(false);
      return;
    }
    if (!threadIdRef.current && data.thread_id) setThread(data.thread_id);
    // companion 只描述默认线程；选中别的线程时不能被它的摘要覆盖
    if (threadIdRef.current === data.thread_id) {
      setSummary(data.summary || "");
    }
  }, [data, setThread]);

  useEffect(() => {
    const onChange = () => {
      setThread(null);
      setThreads([]);
      setMsgs([]);
      setSummary("");
      refreshCompanion();
    };
    window.addEventListener("aieos:ceo-changed", onChange);
    return () => window.removeEventListener("aieos:ceo-changed", onChange);
  }, [refreshCompanion, setThread]);

  /* ---------- 打开抽屉时取最新数据 ---------- */

  useEffect(() => {
    if (!open || !hasCeo) return;
    refreshCompanion();
    void loadThreads();
    const timer = window.setTimeout(() => inputRef.current?.focus(), 60);
    return () => window.clearTimeout(timer);
  }, [open, hasCeo, refreshCompanion, loadThreads]);

  useEffect(() => {
    // 流式进行中不重拉历史：否则服务端只落了老板那句，
    // 会把正在逐字填充的回复气泡覆盖掉。
    if (!open || !hasCeo || !threadId || busy) return;
    void loadMessages(threadId);
  }, [open, hasCeo, threadId, busy, loadMessages]);

  useEffect(() => {
    const el = listRef.current;
    if (el && open) el.scrollTop = el.scrollHeight;
  }, [msgs, busy, open]);

  useEffect(() => () => abortRef.current?.abort(), []);

  /* ---------- 未读提示（只在本机记已读） ---------- */

  useEffect(() => {
    if (!data?.ceo_agent_id) {
      setUnread(false);
      return;
    }
    const key = `aieos_ceo_seen:${data.ceo_agent_id}`;
    let seen = 0;
    try {
      seen = Number(localStorage.getItem(key) || 0);
    } catch {
      /* 隐私模式下读不到，按已读处理 */
    }
    if (open) {
      try {
        localStorage.setItem(key, String(data.message_count));
      } catch {
        /* 忽略 */
      }
      setUnread(false);
    } else {
      setUnread(data.message_count > seen);
    }
  }, [data?.ceo_agent_id, data?.message_count, open]);

  /* ---------- 快捷键 ---------- */

  useEffect(() => {
    if (!shortcut) return;
    const want = canonicalCombo(shortcut);
    const onKey = (e: KeyboardEvent) => {
      if (recordingRef.current || !hasCeo) return;
      if (canonicalCombo(eventCombo(e)) !== want) return;
      e.preventDefault();
      e.stopPropagation();
      setOpen((v) => !v);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [shortcut, hasCeo]);

  // 极客模式：焦点在 iframe 里时按键由内层转发成这个消息
  useEffect(() => {
    const onToggle = () => {
      if (hasCeo) setOpen((v) => !v);
    };
    window.addEventListener("aieos:ceo-toggle", onToggle);
    return () => window.removeEventListener("aieos:ceo-toggle", onToggle);
  }, [hasCeo]);

  useEffect(() => {
    if (!recording) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") {
        setRecording(false);
        return;
      }
      if (MODIFIER_KEYS.includes(e.key)) return; // 等真正的按键
      const mods: string[] = [];
      if (e.metaKey) mods.push("meta");
      if (e.ctrlKey) mods.push("ctrl");
      if (e.altKey) mods.push("alt");
      if (e.shiftKey) mods.push("shift");
      if (!mods.some((m) => m !== "shift")) {
        notify("error", "请至少包含 ⌘ / Ctrl / Alt 中的一个修饰键");
        return;
      }
      const next = canonicalCombo([...mods, e.key.toLowerCase()].join("+"));
      try {
        localStorage.setItem(CEO_SHORTCUT_STORE, next);
      } catch {
        /* 存不了也不影响本次会话 */
      }
      setShortcut(next);
      setRecording(false);
      notify("success", `CEO 唤起快捷键已改为 ${comboLabel(next, isMac)}`);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording, isMac]);

  /* ---------- 拖动 ---------- */

  const clampPos = useCallback((p: Pos): Pos => {
    const el = petWrapRef.current;
    const w = el?.offsetWidth || 96;
    const h = el?.offsetHeight || 132;
    const maxRight = Math.max(8, window.innerWidth - w - 8);
    const maxBottom = Math.max(8, window.innerHeight - h - 8);
    return {
      right: Math.min(Math.max(8, p.right), maxRight),
      bottom: Math.min(Math.max(8, p.bottom), maxBottom),
    };
  }, []);

  useEffect(() => {
    const onResize = () => setPos((p) => clampPos(p));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clampPos]);

  function onPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      right: pos.right,
      bottom: pos.bottom,
      moved: false,
      pointerId: e.pointerId,
    };
    setDragging(true);
    (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d || d.pointerId !== e.pointerId) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    d.moved = true;
    setPos(clampPos({ right: d.right - dx, bottom: d.bottom - dy }));
  }

  function openPet() {
    setHop(true);
    window.setTimeout(() => {
      setHop(false);
      setOpen(true);
    }, 160);
  }

  function onPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    if (!d || d.pointerId !== e.pointerId) return;
    if (!d.moved) {
      // 指针捕获会把 click 重定向到外层 div，按钮的 onClick 收不到；
      // 所以「没拖动」就当作点击，在这里直接展开。
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      openPet();
      return;
    }
    suppressClickRef.current = true;
    setPos((p) => {
      try {
        localStorage.setItem(POS_STORE, JSON.stringify(p));
      } catch {
        /* 忽略 */
      }
      return p;
    });
    window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
  }

  /* ---------- 对话动作 ---------- */

  const patchLast = (patch: (m: Msg) => Msg) => {
    setMsgs((prev) => {
      if (!prev.length) return prev;
      const next = prev.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  };

  async function send(raw?: string) {
    const text = (raw ?? input).trim();
    const agentId = data?.ceo_agent_id;
    if (!text || busy || !agentId) return;
    if (data?.budget_paused) {
      notify("error", "成本预算已暂停，请先到「Token 统计」调高额度");
      return;
    }

    setInput("");
    setErr("");
    setStatus("");
    setBusy(true);
    setMsgs((prev) => [
      ...prev,
      { role: "boss", content: text },
      { role: "agent", content: "", streaming: true },
    ]);

    const ac = new AbortController();
    abortRef.current = ac;
    let acc = "";
    let started = false;

    try {
      await streamPost(`/agents/${agentId}/chat/stream`, {
        message: text,
        thread_id: threadIdRef.current,
      }, {
        signal: ac.signal,
        onEvent: (ev) => {
          if (ev.type === "delta") {
            started = true;
            acc += String(ev.text || "");
            patchLast((m) => ({ ...m, content: acc, streaming: true }));
          } else if (ev.type === "status") {
            setStatus(String(ev.text || ""));
          } else if (ev.type === "done") {
            if (ev.thread_id && !threadIdRef.current) {
              setThread(String(ev.thread_id));
            }
            acc = String(ev.reply ?? acc);
            patchLast((m) => ({ ...m, content: acc || "（没有收到内容）", streaming: false }));
            setStatus("");
          } else if (ev.type === "error") {
            // 已吐出的字保留，错误另起一行，避免半截回复被整段覆盖
            patchLast((m) => ({
              ...m,
              content: m.content
                ? `${m.content}\n\n❌ ${ev.message}`
                : `❌ ${ev.message || "CEO 回复失败"}`,
              streaming: false,
            }));
            setStatus("");
          }
        },
      });
    } catch (e) {
      if (isCancelled(e) || ac.signal.aborted) {
        patchLast((m) => ({
          ...m,
          content: m.content ? `${m.content}\n\n（已停止，本轮回复未保存）` : "（已停止）",
          streaming: false,
        }));
      } else {
        patchLast((m) => ({
          ...m,
          content: started ? m.content : `❌ ${describeError(e, "发送失败")}`,
          streaming: false,
        }));
      }
    } finally {
      if (abortRef.current === ac) abortRef.current = null;
      setBusy(false);
      setStatus("");
      refreshCompanion();
      const tid = threadIdRef.current;
      if (tid) void loadMessages(tid, true);
      void loadThreads(true);
    }
  }

  async function newThread() {
    if (!data?.company_id || threadsBusy) return;
    setThreadsBusy(true);
    setErr("");
    try {
      const t = await api.post<Thread>("/ceo/threads", { company_id: data.company_id });
      setThreads((prev) => [t, ...prev]);
      setThread(t.id);
      setMsgs([]);
      setSummary("");
      window.setTimeout(() => inputRef.current?.focus(), 30);
    } catch (e) {
      setErr(describeError(e, "新建对话失败"));
    } finally {
      setThreadsBusy(false);
    }
  }

  async function removeThread() {
    const tid = threadIdRef.current;
    if (!tid || threadsBusy) return;
    const ok = await confirmAction({
      title: "删除这段对话？",
      description:
        "会永久删除本段对话的原文与摘要。CEO 的人格、技能和其他对话不受影响。",
      confirmText: "删除对话",
      danger: true,
    });
    if (!ok) return;
    setThreadsBusy(true);
    setErr("");
    try {
      await api.del(`/ceo/threads/${tid}`);
      setThread(null);
      setMsgs([]);
      setSummary("");
      notify("success", "对话已删除");
      await loadThreads();
    } catch (e) {
      setErr(describeError(e, "删除对话失败"));
    } finally {
      setThreadsBusy(false);
    }
  }

  const close = useCallback(() => {
    setOpen(false);
    setSettingsOpen(false);
    setRecording(false);
  }, []);

  // Esc 关闭抽屉；正在录制快捷键时由录制逻辑自己处理。
  // 必须在捕获阶段独占：工作台也用 Esc 关右栏应用，不拦住会「一按关两样」。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || recordingRef.current) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      close();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, close]);

  if (!data?.has_ceo) return null;

  const label = comboLabel(shortcut, isMac);
  const last = msgs[msgs.length - 1];
  const showThinking = busy && !last?.streaming;

  return (
    <>
      {/* ---------- 常驻小人 ---------- */}
      {!open && (
        <div
          ref={petWrapRef}
          className="fixed z-[60] select-none group"
          style={{
            right: pos.right,
            bottom: pos.bottom,
            touchAction: "none",
            cursor: dragging ? "grabbing" : "grab",
          }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <button
            type="button"
            aria-label={`${data.ceo_agent_name || "CEO"}，点击对话${label ? `（${label}）` : ""}`}
            className="ceo-pet-root relative block rounded-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400/60"
            onClick={() => {
              if (suppressClickRef.current) {
                suppressClickRef.current = false;
                return;
              }
              if (hop) return;
              // 先让小人跳一下，再展开抽屉——否则它会在动画开始前就被藏起来
              openPet();
            }}
          >
            <span
              className="pointer-events-none absolute -top-8 right-0 whitespace-nowrap rounded-lg px-2 py-1 text-[10px] opacity-0 transition-opacity duration-150 group-hover:opacity-100"
              style={{
                background: "rgba(15,15,22,0.92)",
                border: "1px solid var(--border)",
                color: "var(--muted)",
              }}
            >
              {busy ? "CEO 正在处理…" : "点我和 CEO 聊聊"}
            </span>

            {busy && (
              <span
                className="absolute -top-2 left-0 flex items-end gap-0.5 rounded-full px-2 py-1"
                style={{ background: "rgba(15,15,22,0.92)", border: "1px solid var(--border)" }}
                aria-hidden
              >
                <i className="ceo-pet-think-dot block h-1 w-1 rounded-full" style={{ background: "#a5b4fc" }} />
                <i className="ceo-pet-think-dot block h-1 w-1 rounded-full" style={{ background: "#a5b4fc" }} />
                <i className="ceo-pet-think-dot block h-1 w-1 rounded-full" style={{ background: "#a5b4fc" }} />
              </span>
            )}

            {unread && (
              <span
                className="absolute right-0 top-0 h-3 w-3 rounded-full border-2"
                style={{ background: "#fbbf24", borderColor: "#0b0b10" }}
                aria-label="有未读对话"
              />
            )}

            <span className={`block ${hop ? "ceo-pet-hop" : "ceo-pet-bob"}`}>
              <CeoPet size={84} />
            </span>
          </button>
          <span
            className="mt-0.5 block text-center text-[10px] font-medium tracking-[0.18em]"
            style={{ color: "var(--muted)" }}
          >
            CEO
          </span>
        </div>
      )}

      {/* ---------- 对话抽屉 ---------- */}
      {open && (
        <div
          role="dialog"
          aria-label="和 CEO 的对话"
          className="fixed z-[9500] flex flex-col overflow-hidden rounded-2xl border shadow-2xl animate-fade-up"
          style={{
            right: 12,
            bottom: 12,
            width: "min(400px, calc(100vw - 24px))",
            height: "min(680px, calc(100vh - 24px))",
            background: "linear-gradient(180deg, #15151f, #0f0f16)",
            borderColor: "var(--border)",
          }}
        >
          <header
            className="flex items-center gap-2.5 border-b px-3.5 py-3"
            style={{ borderColor: "var(--border)" }}
          >
            <div
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-[10px] font-bold tracking-wide text-white"
              style={{ background: "linear-gradient(135deg,#1e293b,#3b4b69)" }}
              aria-hidden
            >
              CEO
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold">
                {data.ceo_agent_name || "CEO"}
              </div>
              <div className="truncate text-[10px]" style={{ color: "var(--muted)" }}>
                公司 CEO · 今日 {fmtTokens(data.today_tokens)} tokens · {fmtCost(data.today_cost)}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setSettingsOpen((v) => !v)}
              className={`flex h-7 w-7 items-center justify-center rounded-lg text-xs transition-colors ${
                settingsOpen ? "bg-indigo-500/20 text-indigo-300" : "hover:bg-white/[0.06]"
              }`}
              style={settingsOpen ? undefined : { color: "var(--muted)" }}
              title="对话设置"
              aria-label="对话设置"
              aria-expanded={settingsOpen}
            >
              ⚙
            </button>
            <button
              type="button"
              onClick={close}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-xs hover:bg-white/[0.06]"
              style={{ color: "var(--muted)" }}
              aria-label="关闭 CEO 对话"
            >
              ✕
            </button>
          </header>

          {settingsOpen && (
            <div
              className="space-y-2 border-b px-3.5 py-2.5 text-[11px]"
              style={{ borderColor: "var(--border)", background: "rgba(99,102,241,0.05)" }}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex-1">唤起快捷键</span>
                <kbd
                  className="rounded-md px-2 py-0.5 text-[10px]"
                  style={{ border: "1px solid var(--border)", background: "rgba(255,255,255,0.04)" }}
                >
                  {recording ? "请按组合键…" : label || "未设置"}
                </kbd>
                {recording ? (
                  <button
                    type="button"
                    className="text-indigo-300"
                    onClick={() => setRecording(false)}
                  >
                    取消
                  </button>
                ) : (
                  <button
                    type="button"
                    className="text-indigo-300"
                    onClick={() => setRecording(true)}
                  >
                    修改
                  </button>
                )}
                {!recording && shortcut !== defaultCeoShortcut() && (
                  <button
                    type="button"
                    className="text-[var(--muted)]"
                    onClick={() => {
                      const next = defaultCeoShortcut();
                      try {
                        localStorage.setItem(CEO_SHORTCUT_STORE, next);
                      } catch {
                        /* 忽略 */
                      }
                      setShortcut(next);
                    }}
                  >
                    恢复默认
                  </button>
                )}
              </div>
              <p style={{ color: "var(--muted)" }}>
                快捷键需含 ⌘ / Ctrl / Alt。对话原文永久保存，老段落会被压缩成摘要以节省 token。
              </p>
            </div>
          )}

          {/* 线程切换 */}
          <div
            className="flex items-center gap-1.5 border-b px-3 py-2"
            style={{ borderColor: "var(--border)" }}
          >
            <select
              className="input min-w-0 flex-1 !py-1 text-[11px]"
              value={threadId || ""}
              disabled={busy}
              onChange={(e) => {
                setMsgs([]);
                setSummary("");
                setThread(e.target.value || null);
              }}
              aria-label="选择对话"
            >
              {!threads.length && <option value="">和 CEO 的对话</option>}
              {threads.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title}
                  {t.message_count ? ` · ${t.message_count} 条` : ""}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={newThread}
              disabled={threadsBusy || busy}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-sm hover:bg-white/[0.06] disabled:opacity-40"
              style={{ color: "var(--muted)" }}
              title="新对话"
              aria-label="新建对话"
            >
              ＋
            </button>
            <button
              type="button"
              onClick={removeThread}
              disabled={!threadId || threadsBusy || busy}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-xs hover:bg-white/[0.06] disabled:opacity-40"
              style={{ color: "var(--muted)" }}
              title="删除当前对话"
              aria-label="删除当前对话"
            >
              🗑
            </button>
          </div>

          {data.budget_paused && (
            <div
              className="px-3.5 py-2 text-[11px]"
              style={{ background: "rgba(251,191,36,0.1)", color: "#fcd34d" }}
            >
              成本预算已暂停，CEO 暂时无法调用模型。
              <Link
                href="/token-stats"
                onClick={close}
                className="ml-1 underline underline-offset-2"
              >
                去调额
              </Link>
            </div>
          )}

          {summary && (
            <details
              className="mx-3 mt-2 rounded-lg text-[11px]"
              style={{ background: "rgba(99,102,241,0.06)", border: "1px solid rgba(99,102,241,0.18)" }}
            >
              <summary className="cursor-pointer px-2.5 py-1.5" style={{ color: "#a5b4fc" }}>
                记忆摘要 · 老对话已压缩（省 token）
              </summary>
              <p className="max-h-32 overflow-y-auto whitespace-pre-wrap px-2.5 pb-2 leading-relaxed" style={{ color: "var(--muted)" }}>
                {summary}
              </p>
            </details>
          )}

          {/* 消息区 */}
          <div ref={listRef} className="ws-scroll flex-1 space-y-2.5 overflow-y-auto px-3 py-3">
            {msgsLoading && !msgs.length && (
              <p className="py-8 text-center text-xs" style={{ color: "var(--muted)" }}>
                正在读取对话…
              </p>
            )}

            {!msgsLoading && !msgs.length && (
              <div className="space-y-3 py-4">
                <p className="text-center text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
                  老板，我在。今天想先聊哪件事？
                </p>
                <div className="flex flex-wrap justify-center gap-1.5">
                  {(data.quick_prompts || []).map((p) => (
                    <button
                      key={p}
                      type="button"
                      disabled={busy || data.budget_paused}
                      onClick={() => void send(p)}
                      className="rounded-full px-2.5 py-1 text-[11px] transition-colors hover:border-indigo-400/60 disabled:opacity-40"
                      style={{ border: "1px solid var(--border)", color: "var(--muted)" }}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {msgs.map((m, i) => {
              const boss = m.role === "boss";
              return (
                <div key={m.id || i} className={`flex ${boss ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[86%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-xs leading-relaxed ${
                      boss ? "rounded-br-sm bg-indigo-600 text-white" : "rounded-bl-sm"
                    }`}
                    style={boss ? undefined : { background: "var(--surface-2)", color: "var(--foreground)" }}
                  >
                    {m.content || (m.streaming ? "…" : "（空）")}
                    {m.streaming && <span className="ws-caret" aria-hidden />}
                  </div>
                </div>
              );
            })}

            {showThinking && (
              <div className="flex justify-start">
                <div
                  className="rounded-2xl rounded-bl-sm px-3 py-2 text-xs pulse-dot"
                  style={{ background: "var(--surface-2)" }}
                >
                  CEO 正在思考…
                </div>
              </div>
            )}

            {err && (
              <div className="rounded-lg px-2.5 py-2 text-[11px]" style={{ background: "rgba(248,113,113,0.12)", color: "#fca5a5" }}>
                {err}
              </div>
            )}
          </div>

          {status && (
            <p className="px-3.5 pb-1 text-[10px] pulse-dot" style={{ color: "var(--muted)" }}>
              {status}
            </p>
          )}

          {/* 输入区 */}
          <div className="border-t p-2.5" style={{ borderColor: "var(--border)" }}>
            <div className="flex items-center gap-1.5">
              <input
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void send();
                  }
                }}
                placeholder={data.budget_paused ? "预算已暂停" : "问点什么，或直接下指令…"}
                disabled={data.budget_paused}
                className="input min-w-0 flex-1 !py-2 text-xs disabled:opacity-50"
                aria-label="给 CEO 发消息"
              />
              {busy ? (
                <button
                  type="button"
                  onClick={() => abortRef.current?.abort()}
                  className="shrink-0 rounded-lg px-3 py-2 text-xs font-medium"
                  style={{
                    background: "rgba(248,113,113,0.14)",
                    color: "#fca5a5",
                    border: "1px solid rgba(248,113,113,0.35)",
                  }}
                >
                  停止
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void send()}
                  disabled={!input.trim() || data.budget_paused}
                  className="btn-primary shrink-0 !px-3.5 !py-2 text-xs disabled:opacity-40"
                >
                  发送
                </button>
              )}
            </div>
            <p className="mt-1 text-[10px]" style={{ color: "var(--muted)" }}>
              {label ? `${label} 随时唤起 · ` : ""}对话永久保存
            </p>
          </div>
        </div>
      )}
    </>
  );
}
