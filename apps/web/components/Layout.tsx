"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, clearToken } from "@/lib/api";
import { notify } from "@/components/Toast";
import { NAV_GROUPS, isNavActive } from "@/lib/nav";
import { openCommandPalette, pushRecentPage } from "@/components/CommandPalette";

/* 导航数据来自 `lib/nav.ts`（侧边栏与命令面板共用的单一事实源）。
   这里只负责呈现，不再自己维护一份页面清单。 */

const COLLAPSED_KEY = "aieos_nav_collapsed";

function groupHasActive(pathname: string, items: { href: string }[]): boolean {
  return items.some((it) => isNavActive(pathname, it.href));
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  // 折叠状态：
  //   null = 用户从未手动折过 → 只展开当前所在分组（首次进入侧边栏不会一屏铺满 34 项）
  //   对象 = 用户已表达过偏好 → 完全按他的来（但当前分组始终展开，避免"找不到自己在哪"）
  const [collapsed, setCollapsed] = useState<Record<string, boolean> | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(COLLAPSED_KEY);
      if (raw) setCollapsed(JSON.parse(raw));
    } catch {
      /* 存储损坏时按"仅展开当前分组"处理即可，不值得打断用户 */
    }
  }, []);

  // 记录访问轨迹：⌘K 面板的「最近访问」靠它。放在这里而不是逐页埋点，
  // 是因为任何页面（含详情页）都经过这个 Layout，不会漏。
  useEffect(() => {
    if (pathname) pushRecentPage(pathname);
  }, [pathname]);

  const toggleGroup = useCallback((title: string) => {
    setCollapsed((prev) => {
      // 首次手动折叠时，以"当前实际显示状态"为基准，否则第一次点击会出现
      // "点了反而全展开"的错位。
      const base = prev ?? Object.fromEntries(NAV_GROUPS.map((g) => [g.title, !groupHasActive(pathname, g.items)]));
      const next = { ...base, [title]: !base[title] };
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* 隐私模式下 localStorage 可能不可写；折叠仍然生效，只是不持久 */
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
      // 退出时清 token 失败：仍然要离开，但得让用户知道本地凭据可能还在
      notify("warning", `退出时清理本地凭据失败：${(e as Error).message || e}`);
    }
    router.push("/login");
  }

  const allCollapsed = collapsed !== null && NAV_GROUPS.every((g) => collapsed[g.title]);

  return (
    <div className="flex h-screen text-gray-100">
      {/* ---------- 侧边栏 ---------- */}
      <aside className="w-60 flex flex-col shrink-0 border-r" style={{ borderColor: "var(--border)", background: "rgba(10,10,16,0.6)" }}>
        <div className="px-5 pt-5 pb-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center text-white font-bold"
              style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)" }}>
              ⚡
            </div>
            <div>
              <h1 className="text-base font-bold tracking-wide">AI-EOS <span className="text-[10px] ml-1 px-1.5 py-px rounded-full bg-gradient-to-r from-indigo-500 to-cyan-400 text-white">V4.2</span></h1>
              <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>人工智能企业操作系统</p>
            </div>
          </div>
        </div>

        {/* 搜索入口：34 个页面的系统里，"找到"比"列全"更重要 */}
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

        <nav className="flex-1 px-3 pb-3 space-y-3.5 overflow-y-auto">
          {NAV_GROUPS.map((group) => {
            const active = groupHasActive(pathname, group.items);
            // 当前所在分组永远展开——否则用户会"找不到自己现在在哪"
            const isCollapsed = (collapsed === null ? !active : !!collapsed[group.title]) && !active;
            return (
              <div key={group.title}>
                <button
                  type="button"
                  onClick={() => toggleGroup(group.title)}
                  aria-expanded={!isCollapsed}
                  title={group.hint}
                  className="w-full flex items-center justify-between px-2 mb-1.5 group"
                >
                  <span className="text-[10px] font-semibold tracking-widest uppercase"
                    style={{ color: active ? "#8b8bff" : "#55555f" }}>
                    {group.title}
                  </span>
                  <span className="text-[9px] transition-transform duration-150"
                    style={{ color: "#55555f",
                             transform: isCollapsed ? "rotate(-90deg)" : "none" }}>
                    ▾
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="space-y-0.5">
                    {group.items.map((item) => {
                      const active = isNavActive(pathname, item.href);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
                          title={item.desc}
                          className={`relative flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-sm transition-all duration-150 ${
                            active ? "text-white font-medium" : "hover:text-gray-200 hover:bg-white/[0.04]"
                          }`}
                          style={active ? { background: "linear-gradient(90deg, rgba(99,102,241,0.18), rgba(99,102,241,0.05))" } : { color: "var(--muted)" }}
                        >
                          {active && (
                            <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-full"
                              style={{ background: "linear-gradient(180deg,#6366f1,#22d3ee)" }} />
                          )}
                          <span className={`text-sm ${active ? "" : "opacity-70"}`}>{item.icon}</span>
                          {item.label}
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        <button onClick={logout} className="mx-3 mb-3 px-3 py-2 text-xs rounded-lg transition-colors hover:text-red-400"
          style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
          退出登录
        </button>
      </aside>

      {/* ---------- 主内容 ---------- */}
      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  );
}
