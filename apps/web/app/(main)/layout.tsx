"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "@/components/Layout";
import ToastHost from "@/components/Toast";
import ConfirmHost from "@/components/ConfirmDialog";
import CommandPalette from "@/components/CommandPalette";
import GlobalShortcuts from "@/components/GlobalShortcuts";
import { api, ApiError, getToken } from "@/lib/api";

/**
 * 登录态守卫 + 全局外壳。
 *
 * 改造前的问题：`/auth/me` 失败时只是 `.catch(() => {})`，
 * 而 `ok` 永远为 false → 用户看到一片纯黑空白页，没有任何解释，
 * 只能反复刷新。现在区分三种状态：校验中 / 校验失败（可重试）/ 通过。
 */
export default function MainLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [state, setState] = useState<"checking" | "ready" | "failed">("checking");
  const [error, setError] = useState("");

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

  return (
    <>
      <ToastHost />
      {/* 统一二次确认：全站危险操作都走它，替代原生 confirm */}
      <ConfirmHost />
      {/* ⌘K 全局跳转：34 个页面规模下，"找得到"比"列得全"更重要 */}
      <CommandPalette />
      {/* 全局快捷键（n / / j k / 空格 / ?）：与命令面板互不干扰 */}
      <GlobalShortcuts />
      <Layout>{children}</Layout>
    </>
  );
}
