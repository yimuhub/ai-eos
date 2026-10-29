"use client";
import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import ToastHost from "@/components/Toast";
import ConfirmHost from "@/components/ConfirmDialog";
import CommandPalette from "@/components/CommandPalette";
import GlobalShortcuts from "@/components/GlobalShortcuts";
import UpdateBanner from "@/components/UpdateBanner";
import ClassicLayout from "@/components/ClassicLayout";
import ModeChooser from "@/components/ModeChooser";
import { api, ApiError, getToken } from "@/lib/api";
import { isEmbedded } from "@/lib/apps";
import { useUIMode } from "@/lib/ui-mode";

/**
 * 登录态守卫 + 双模式外壳。
 *
 *   classic：左侧固定菜单 + 整页导航，每个功能是一个完整页面（v5.12 之前的形态）；
 *   geek：`/workspace` 单外壳，功能页开在右栏标签里，对话不中断。
 *
 * 首次进入必须二选一（ModeChooser），选择存在 localStorage，之后可随时切换。
 * 内嵌页面（iframe）永远剥掉外壳，两种模式下都一样。
 */
const DESKTOP_TITLEBAR = 34;

export default function MainLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const mode = useUIMode();
  const [state, setState] = useState<"checking" | "ready" | "failed">("checking");
  const [error, setError] = useState("");
  // 桌面壳改成 hiddenInset 后，顶部 34px 归红绿灯与窗口拖拽；普通页面也要让出这段
  const [desktop, setDesktop] = useState(false);
  const [embed, setEmbed] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const d = (window as any).aieosDesktop;
    // 只有 mac 改成了 hiddenInset；Windows 保留系统标题栏，不能再多让 34px
    setDesktop(!!d?.isDesktop && d?.platform === "darwin");
    setEmbed(isEmbedded());
    setMounted(true);
  }, []);

  const verify = useCallback(async () => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setState("checking");
    try {
      await api.get("/auth/me");
      setState("ready");
    } catch (e: any) {
      // 401 已由 api 层处理（清 token 并跳登录），这里只需覆盖其它失败
      const err = e instanceof ApiError ? e : null;
      if (err?.isAuth) return;
      setError(err?.message || "无法验证登录状态");
      setState("failed");
    }
  }, [router]);

  useEffect(() => {
    verify();
  }, [verify]);

  const isWorkspace = pathname === "/workspace";
  const isHome = pathname === "/";

  // 极客模式的单界面约定：除工作台外的页面一律带回工作台，作为右栏标签打开。
  // 经典模式不做任何改写；iframe 里必须放行，否则标签内会无限自我重定向。
  useEffect(() => {
    if (state !== "ready" || !mounted || embed) return;
    if (mode !== "geek" || isWorkspace || isHome) return;
    const target = `${pathname}${window.location.search}${window.location.hash}`;
    router.replace(`/workspace?app=${encodeURIComponent(target)}`);
  }, [state, mounted, embed, mode, isWorkspace, isHome, pathname, router]);

  if (state === "checking") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-3">
        <span
          className="w-6 h-6 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
          aria-hidden
        />
        <p className="text-sm" style={{ color: "var(--muted)" }}>正在校验登录状态…</p>
      </div>
    );
  }

  if (state === "failed") {
    return (
      <div className="min-h-screen flex items-center justify-center p-6">
        <div className="card card-static max-w-md w-full text-center space-y-3">
          <div className="text-2xl">🔌</div>
          <h2 className="font-medium">连接后端服务失败</h2>
          <p className="text-sm" style={{ color: "var(--muted)" }}>{error}</p>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            请确认后端已启动（默认 http://localhost:8000），然后重试。
          </p>
          <button onClick={verify} className="btn-primary">重试连接</button>
        </div>
      </div>
    );
  }

  // 模式只能从 localStorage 读到，首帧先不渲染界面，避免闪一下再换形态
  if (!mounted) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <span
          className="w-5 h-5 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
          aria-hidden
        />
      </div>
    );
  }

  const hosts = (
    <>
      <ToastHost />
      {/* 统一二次确认：全站危险操作都走它，替代原生 confirm */}
      <ConfirmHost />
      {/* ⌘K 全局跳转：30+ 页面规模下，"找得到"比"列得全"更重要 */}
      <CommandPalette />
      {/* 全局快捷键（n / / j k / 空格 / ?）：与命令面板互不干扰 */}
      <GlobalShortcuts />
    </>
  );

  if (embed) {
    return (
      <>
        {hosts}
        <EmbedReporter />
        <EmbedShortcuts />
        {/* 内嵌页面自己滚动；固定高度避免 iframe 里出现第二层 body 滚动 */}
        <div className="h-screen overflow-y-auto">{children}</div>
      </>
    );
  }

  // 还没有选择过模式：先让用户二选一，任何页面都先不渲染
  if (mode === null) {
    return (
      <>
        {hosts}
        <div className="h-screen flex flex-col items-center justify-center gap-3"
          style={{ paddingTop: desktop ? DESKTOP_TITLEBAR : 0 }}>
          <div className="w-10 h-10 rounded-xl flex items-center justify-center text-white font-bold"
            style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)" }}>
            ⚡
          </div>
          <p className="text-xs" style={{ color: "var(--muted)" }}>先选择一种界面模式</p>
        </div>
        <ModeChooser />
      </>
    );
  }

  const chrome = (
    <>
      {hosts}
      {/* 自动升级横幅：只在桌面壳里工作，浏览器访问零渲染 */}
      <UpdateBanner />
      {desktop && <div className="app-drag fixed inset-x-0 top-0 z-40 h-[34px]" aria-hidden />}
    </>
  );

  if (mode === "classic") {
    return (
      <>
        {chrome}
        <div className="h-screen overflow-hidden" style={{ paddingTop: desktop ? DESKTOP_TITLEBAR : 0 }}>
          <ClassicLayout>{children}</ClassicLayout>
        </div>
        <ModeChooser />
      </>
    );
  }

  // "/" 自己按模式分流（经典 → /dashboard，极客 → /workspace），必须放它渲染
  if (!isWorkspace && !isHome) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-2">
        <span
          className="w-5 h-5 rounded-full border-2 animate-spin"
          style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
          aria-hidden
        />
        <p className="text-xs" style={{ color: "var(--muted)" }}>正在回到工作台…</p>
      </div>
    );
  }

  return (
    <>
      {chrome}
      <div className="h-screen overflow-hidden" style={{ paddingTop: desktop ? DESKTOP_TITLEBAR : 0 }}>
        {children}
      </div>
      <ModeChooser />
    </>
  );
}

/** 把 iframe 内的当前路径报给外壳：标签标题跟着页面里的跳转走。 */
function EmbedReporter() {
  const pathname = usePathname();
  useEffect(() => {
    window.parent.postMessage(
      { type: "aieos:embed", href: `${pathname}${window.location.search}` },
      window.location.origin,
    );
  }, [pathname]);
  return null;
}

/**
 * 键盘焦点落进 iframe 后，外壳收不到 ⌘K/⌘B/⌘J/⌘N——
 * 用户正在页面里操作时按 ⌘K，期待的是同一个命令面板，而不是"这个标签自己跳走"。
 * 所以在捕获阶段拦下来转给父页面；其余按键一律不碰，页面自己的快捷键照常。
 */
function EmbedShortcuts() {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const k = e.key.toLowerCase();
      const name = k === "k" ? "palette" : k === "b" ? "left" : k === "j" ? "right" : k === "n" ? "new-chat" : "";
      if (!name) return;
      e.preventDefault();
      e.stopPropagation();
      window.parent.postMessage({ type: "aieos:shortcut", name }, window.location.origin);
    }
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);
  return null;
}
