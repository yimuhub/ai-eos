"use client";
import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import ToastHost from "@/components/Toast";
import ConfirmHost from "@/components/ConfirmDialog";
import CommandPalette from "@/components/CommandPalette";
import GlobalShortcuts from "@/components/GlobalShortcuts";
import UpdateBanner from "@/components/UpdateBanner";
import { api, ApiError, getToken } from "@/lib/api";
import { isEmbedded } from "@/lib/apps";

/**
 * 登录态守卫 + 全局外壳。
 *
 * 单界面约定：`/workspace` 是唯一的界面外壳，其余页面只在它的右栏 iframe 里出现。
 * 直接访问 `/tasks` 之类的地址会被带回 `/workspace?app=/tasks`，
 * 这样书签、刷新、外部粘贴链接都落在同一个界面里。
 *
 * 内嵌页面（iframe，`window.self !== window.top`）反过来要剥掉外壳：
 * 页面的 Toast / 确认框 / ⌘K 都挂在自己的文档里，否则弹出层会消失。
 */
const DESKTOP_TITLEBAR = 34;

export default function MainLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [state, setState] = useState<"checking" | "ready" | "failed">("checking");
  const [error, setError] = useState("");
  // 桌面壳改成 hiddenInset 后，顶部 34px 归红绿灯与窗口拖拽；普通页面也要让出这段
  const [desktop, setDesktop] = useState(false);
  const [embed, setEmbed] = useState(false);

  useEffect(() => {
    const d = (window as any).aieosDesktop;
    // 只有 mac 改成了 hiddenInset；Windows 保留系统标题栏，不能再多让 34px
    setDesktop(!!d?.isDesktop && d?.platform === "darwin");
    setEmbed(isEmbedded());
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

  // 单界面：除工作台外的页面一律带回工作台，作为右栏标签打开。
  // iframe 里必须放行，否则标签内会无限自我重定向。
  useEffect(() => {
    if (state !== "ready" || embed || isWorkspace) return;
    const target = `${pathname}${window.location.search}${window.location.hash}`;
    router.replace(`/workspace?app=${encodeURIComponent(target)}`);
  }, [state, embed, isWorkspace, pathname, router]);

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

  const hosts = (
    <>
      <ToastHost />
      {/* 统一二次确认：全站危险操作都走它，替代原生 confirm */}
      <ConfirmHost />
      {/* ⌘K 全局跳转：34 个页面规模下，"找得到"比"列得全"更重要 */}
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

  if (!isWorkspace) {
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
      {hosts}
      {/* 自动升级横幅：只在桌面壳里工作，浏览器访问零渲染 */}
      <UpdateBanner />
      {desktop && <div className="app-drag fixed inset-x-0 top-0 z-40 h-[34px]" aria-hidden />}
      <div className="h-screen overflow-hidden" style={{ paddingTop: desktop ? DESKTOP_TITLEBAR : 0 }}>
        {children}
      </div>
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
