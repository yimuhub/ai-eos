"use client";

// 经典模式外壳：v5.12 及之前的那套「左侧固定菜单 + 整页导航」。
//
// 与极客模式的区别只有交互形态：功能页是整页，不做右栏标签。
// 导航数据仍来自 `lib/nav.ts`（侧边栏与 ⌘K 共用同一份定义）。

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { clearToken } from "@/lib/api";
import { notify } from "@/components/Toast";
import { NAV_GROUPS, isNavActive } from "@/lib/nav";
import { openCommandPalette, pushRecentPage } from "@/components/CommandPalette";
import { openModeChooser } from "@/components/ModeChooser";
import { registerAppNavigator } from "@/lib/apps";

const COLLAPSED_KEY = "aieos_nav_collapsed";

function groupHasActive(pathname: string, items: { href: string }[]): boolean {
  return items.some((it) => isNavActive(pathname, it.href));
}

export default function ClassicLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  // 折叠状态：
  //   null = 从未手动折过 → 只展开当前所在分组（避免一屏铺满 30+ 项）
  //   对象 = 用户已表达过偏好 → 按他的来，但当前分组始终展开
  const [collapsed, setCollapsed] = useState<Record<string, boolean> | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(COLLAPSED_KEY);
      if (raw) setCollapsed(JSON.parse(raw));
    } catch {
      /* 存储损坏时按"仅展开当前分组"处理即可，不值得打断用户 */
    }
  }, []);

  useEffect(() => {
    if (pathname) pushRecentPage(pathname);
  }, [pathname]);

  // 经典模式下没有右栏标签容器：`openApp()` 走普通路由跳转
  useEffect(() => registerAppNavigator((href) => router.push(href)), [router]);

  const toggleGroup = useCallback((title: string) => {
    setCollapsed((prev) => {
      const base = prev ?? Object.fromEntries(NAV_GROUPS.map((g) => [g.title, !groupHasActive(pathname, g.items)]));
      const next = { ...base, [title]: !base[title] };
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* 隐私模式：折叠仍生效，只是不持久 */
      }
      return next;
    });
  }, [pathname]);

  const setAll = useCallback((value: boolean) => {
    const next = Object.fromEntries(NAV_GROUPS.map((g) => [g.title, value]));
    setCollapsed(next);
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
    } catch {
      /* 同上 */
    }
  }, []);

  async function logout() {
    try {
      await clearToken();
    } catch (e) {
      notify("warning", `退出时清理本地凭据失败：${(e as Error).message || e}`);
    }
    router.push("/login");
  }

  const allCollapsed = collapsed !== null && NAV_GROUPS.every((g) => collapsed[g.title]);

  return (
    <div className="flex h-full text-gray-100">
      {/* ---------- 固定左侧菜单 ---------- */}
      <aside
        className="w-60 h-full flex flex-col shrink-0 border-r"
        style={{ borderColor: "var(--border)", background: "rgba(10,10,16,0.85)", backdropFilter: "blur(8px)" }}
      >
        <div className="px-5 pt-5 pb-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center text-white font-bold shrink-0"
              style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)" }}>
              ⚡
            </div>
            <div className="min-w-0">
              <h1 className="text-base font-bold tracking-wide">AI-EOS</h1>
              <p className="text-[10px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>人工智能企业操作系统</p>
            </div>
          </div>
        </div>

        {/* 30+ 页面的系统里，"找到"比"列全"更重要 */}
        <div className="px-3 pb-2">
          <button
            type="button"
            onClick={openCommandPalette}
            aria-label="搜索页面或操作"
            className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs transition-colors hover:bg-white/[0.05]"
            style={{ border: "1px solid var(--border)", color: "var(--muted)" }}
          >
            <span aria-hidden>🔍</span>
            <span className="flex-1 text-left">搜索页面 / 操作</span>
            <kbd className="text-[9px] px-1 py-0.5 rounded" style={{ border: "1px solid var(--border)" }}>⌘K</kbd>
          </button>
        </div>

        <div className="px-5 pb-1 flex items-center justify-between">
          <span className="text-[9px] tracking-widest uppercase" style={{ color: "#55555f" }}>导航</span>
          <button
            type="button"
            onClick={() => setAll(!allCollapsed)}
            className="text-[9px] transition-colors hover:text-gray-300"
            style={{ color: "#55555f" }}
          >
            {allCollapsed ? "全部展开" : "全部收起"}
          </button>
        </div>

        <nav className="flex-1 px-3 pb-3 space-y-3.5 overflow-y-auto ws-scroll">
          {NAV_GROUPS.map((group) => {
            const active = groupHasActive(pathname, group.items);
            const isCollapsed = (collapsed === null ? !active : !!collapsed[group.title]) && !active;
            return (
              <div key={group.title}>
                <button
                  type="button"
                  onClick={() => toggleGroup(group.title)}
                  aria-expanded={!isCollapsed}
                  title={group.hint}
                  className="w-full flex items-center justify-between px-2 mb-1.5"
                >
                  <span className="text-[10px] font-semibold tracking-widest uppercase"
                    style={{ color: active ? "#8b8bff" : "#55555f" }}>
                    {group.title}
                  </span>
                  <span className="text-[9px] transition-transform duration-150"
                    style={{ color: "#55555f", transform: isCollapsed ? "rotate(-90deg)" : "none" }}>
                    ▾
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="space-y-0.5">
                    {group.items.map((item) => {
                      const on = isNavActive(pathname, item.href);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          title={item.desc}
                          aria-current={on ? "page" : undefined}
                          className={`relative flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-sm transition-colors duration-150 ${
                            on ? "text-white font-medium" : "hover:text-gray-200 hover:bg-white/[0.04]"
                          }`}
                          style={on ? { background: "linear-gradient(90deg, rgba(99,102,241,0.18), rgba(99,102,241,0.05))" } : { color: "var(--muted)" }}
                        >
                          {on && (
                            <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-full"
                              style={{ background: "linear-gradient(180deg,#6366f1,#22d3ee)" }} />
                          )}
                          <span className={`text-sm shrink-0 ${on ? "" : "opacity-70"}`}>{item.icon}</span>
                          <span className="truncate">{item.label}</span>
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        <div className="px-3 pb-3 pt-2 space-y-2" style={{ borderTop: "1px solid var(--border)" }}>
          <button
            type="button"
            onClick={openModeChooser}
            title="切换界面模式"
            className="w-full flex items-center justify-between px-3 py-2 text-xs rounded-lg transition-colors hover:bg-white/[0.05]"
            style={{ color: "var(--muted)", border: "1px solid var(--border)" }}
          >
            <span>⇄ 界面模式</span>
            <span className="text-[10px]">经典</span>
          </button>
          <button onClick={logout} className="w-full px-3 py-2 text-xs rounded-lg transition-colors hover:text-red-400"
            style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
            退出登录
          </button>
        </div>
      </aside>

      {/* ---------- 主内容：整页，自己滚动 ---------- */}
      <main className="flex-1 min-w-0 overflow-auto">{children}</main>
    </div>
  );
}
