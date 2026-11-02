"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import CeoPet from "@/components/CeoPet";
import { ApiError, api } from "@/lib/api";
import { comboLabel, readCeoShortcut } from "@/lib/ceoShortcut";
import { desktopBridge } from "@/lib/desktopBridge";

/**
 * 桌面常驻小人的全部交互：
 * - 拖动：只上报「开始/结束」，窗口跟随由主进程轮询光标完成；
 * - 点击（没拖动）：确认已任命 CEO 就唤起主窗口的对话抽屉，否则把主窗口叫出来；
 * - 未读：和网页抽屉共用 `aieos_ceo_seen:<id>`，抽屉里读过会通过 storage 事件同步消失。
 */

type Companion = {
  has_ceo: boolean;
  ceo_agent_id: string | null;
  ceo_agent_name: string | null;
  message_count: number;
};

function seenKey(id: string | null): string {
  return id ? `aieos_ceo_seen:${id}` : "";
}

function readSeen(id: string | null): number {
  const key = seenKey(id);
  if (!key) return 0;
  try {
    return Number(localStorage.getItem(key) || 0);
  } catch {
    return 0;
  }
}

export default function PetClient({ desktop }: { desktop: boolean }) {
  const [hasCeo, setHasCeo] = useState(false);
  const [name, setName] = useState("CEO");
  const [hint, setHint] = useState("正在连接 CEO…");
  const [offline, setOffline] = useState(false);
  const [unread, setUnread] = useState(false);
  const [hop, setHop] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [label, setLabel] = useState("");

  const dataRef = useRef<{ id: string | null; count: number }>({ id: null, count: 0 });
  const dragRef = useRef<{
    startX: number;
    startY: number;
    pointerId: number;
    moving: boolean;
  } | null>(null);
  const movedRef = useRef(false);

  const refresh = useCallback(async () => {
    setLabel(comboLabel(readCeoShortcut()));
    try {
      // 小人窗口不是登录页：401 只把自己收起来，不能整窗跳去 /login
      const d = await api.get<Companion>("/ceo/companion", {
        noAuthRedirect: true,
        silent: true,
      });
      const has = !!d.has_ceo;
      setHasCeo(has);
      setName(d.ceo_agent_name || "CEO");
      setOffline(false);
      const bridge = desktopBridge();
      void bridge?.petState?.({ hasCeo: has });

      if (!has) {
        dataRef.current = { id: null, count: 0 };
        setUnread(false);
        setHint("点我回主界面任命 CEO");
        return;
      }

      const id = d.ceo_agent_id || null;
      const count = Number(d.message_count) || 0;
      dataRef.current = { id, count };
      setUnread(count > readSeen(id));
      setHint("点我和 CEO 聊聊");
    } catch (e) {
      if (e instanceof ApiError && e.isAuth) {
        // 登录态确实失效：交给主窗口去处理登录，小人先收起来
        setHasCeo(false);
        setUnread(false);
        setHint("登录已过期 · 点我回主界面");
        void desktopBridge()?.petState?.({ hasCeo: false });
        return;
      }
      // 网络/服务暂时不可达：保留小人，让老板还能一键回到主界面看错误
      setOffline(true);
      setUnread(false);
      setHint("服务未连接 · 点我回主界面");
    }
  }, []);

  useEffect(() => {
    if (desktop) {
      document.documentElement.style.background = "transparent";
      document.body.style.background = "transparent";
    }
    void refresh();
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, 60_000);
    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    // 抽屉里读消息会写 seen；同一来源的另一个窗口靠 storage 事件收到
    const onStorage = (e: StorageEvent) => {
      const d = dataRef.current;
      if (!d.id || e.key !== seenKey(d.id)) return;
      setUnread(d.count > Number(e.newValue || 0));
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("storage", onStorage);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("storage", onStorage);
    };
  }, [desktop, refresh]);

  useEffect(() => {
    // 快捷键可能在主窗口里被改过；回到小人窗口时同步显示
    const sync = () => setLabel(comboLabel(readCeoShortcut()));
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  function activate() {
    setHop(true);
    window.setTimeout(() => setHop(false), 420);
    const bridge = desktopBridge();
    if (!bridge) {
      window.location.href = "/";
      return;
    }
    if (hasCeo) void bridge.openCeoChat?.();
    else void bridge.showMain?.();
  }

  function onPointerDown(e: ReactPointerEvent<HTMLButtonElement>) {
    if (e.button !== 0) return;
    dragRef.current = {
      startX: e.screenX,
      startY: e.screenY,
      pointerId: e.pointerId,
      moving: false,
    };
    movedRef.current = false;
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: ReactPointerEvent<HTMLButtonElement>) {
    const d = dragRef.current;
    if (!d || d.pointerId !== e.pointerId) return;
    if (!d.moving) {
      // 屏幕坐标判定：窗口开始跟随后，指针在窗口内的坐标几乎不变
      if (Math.hypot(e.screenX - d.startX, e.screenY - d.startY) < 4) return;
      d.moving = true;
      movedRef.current = true;
      setDragging(true);
      void desktopBridge()?.petDragStart?.();
    }
  }

  function finishPointer(e: ReactPointerEvent<HTMLButtonElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    if (!d || d.pointerId !== e.pointerId) return;
    if (d.moving) {
      void desktopBridge()?.petDragEnd?.();
      return;
    }
    activate();
  }

  function cancelPointer() {
    dragRef.current = null;
    setDragging(false);
    if (movedRef.current) void desktopBridge()?.petDragEnd?.();
    movedRef.current = false;
  }

  useEffect(() => {
    const onBlur = () => {
      if (dragRef.current?.moving) {
        dragRef.current = null;
        setDragging(false);
        void desktopBridge()?.petDragEnd?.();
      }
    };
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, []);

  const bubble = (
    <span
      className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1.5 w-max max-w-[168px] -translate-x-1/2 rounded-lg px-2.5 py-1.5 text-center text-[10px] leading-snug opacity-0 transition-opacity duration-150 group-hover:opacity-100"
      style={{
        background: "rgba(15,15,22,0.94)",
        border: "1px solid var(--border)",
        color: "#d7dbe8",
        boxShadow: "0 6px 18px rgba(0,0,0,0.35)",
      }}
    >
      {offline ? "服务未连接 · 点我回主界面" : hint}
      {label && (
        <span className="mt-0.5 block text-[9px] opacity-60">{label} 随时唤起</span>
      )}
    </span>
  );

  const petButton = (
    <button
      type="button"
      aria-label={`${name}：${offline ? "服务未连接，点我回主界面" : hint}${label ? `（${label}）` : ""}`}
      className="ceo-pet-root group relative block rounded-2xl p-2 outline-none transition-colors hover:bg-white/[0.04] focus-visible:ring-2 focus-visible:ring-indigo-400/60"
      style={{ cursor: dragging ? "grabbing" : "grab", touchAction: "none" }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishPointer}
      onPointerCancel={cancelPointer}
      // 键盘（Enter/空格）触发的是 detail=0 的 click，没有指针事件；
      // 鼠标点击统一由 pointerup 处理，避免一次点击打开两次。
      onClick={(e) => {
        if (e.detail === 0) activate();
      }}
      onContextMenu={(e) => e.preventDefault()}
      onDragStart={(e) => e.preventDefault()}
    >
      {bubble}
      {unread && (
        <span
          className="absolute right-2 top-2 z-10 h-3 w-3 rounded-full border-2"
          style={{ background: "#fbbf24", borderColor: "#0b0b10" }}
          aria-label="有未读对话"
        />
      )}
      <span className={hop ? "ceo-pet-hop block" : "ceo-pet-bob block"}>
        <CeoPet size={desktop ? 96 : 120} />
      </span>
    </button>
  );

  if (!desktop) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4">
        {petButton}
        <div className="space-y-1 text-center">
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            这是桌面客户端的常驻 CEO 小人。
          </p>
          <p className="text-[11px]" style={{ color: "var(--muted)" }}>
            安装客户端后，小人会留在桌面右下角，点击即可对话。
          </p>
        </div>
      </main>
    );
  }

  return (
    <div className="flex h-screen w-screen select-none flex-col items-center justify-end bg-transparent pb-1.5">
      {petButton}
      <span
        className="mt-0.5 text-[10px] font-medium tracking-[0.2em]"
        style={{ color: "#9aa3b8", textShadow: "0 1px 4px rgba(0,0,0,0.65)" }}
      >
        {offline ? "CEO · 离线" : "CEO"}
      </span>
    </div>
  );
}
