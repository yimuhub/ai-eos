"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, clearToken } from "@/lib/api";
import { notify } from "@/components/Toast";

/* 分组导航：按老板使用动线分类 —— 运营主战线 / 组织与人 / 协作生产 / 知识资产 / 成长与治理 */
const NAV_GROUPS: { title: string; items: { href: string; label: string; icon: string }[] }[] = [
  {
    title: "指挥中枢",
    items: [
      { href: "/", label: "指挥中心", icon: "◈" },
      { href: "/projects", label: "项目中心", icon: "▤" },
      { href: "/warroom", label: "作战室", icon: "🎯" },
      { href: "/tasks", label: "任务", icon: "◉" },
      { href: "/autonomous", label: "自治运行", icon: "♾" },
      { href: "/companies", label: "公司管理", icon: "🏢" },
    ],
  },
  {
    title: "组织与智能体",
    items: [
      { href: "/agents", label: "Agent 团队", icon: "⬡" },
      { href: "/organization", label: "组织架构", icon: "⌗" },
      { href: "/factory", label: "Agent 工厂", icon: "✦" },
      { href: "/agent-offboarding", label: "离职交接", icon: "⇥" },
      { href: "/models", label: "模型配置", icon: "◈" },
    ],
  },
  {
    title: "协作执行",
    items: [
      { href: "/meetings", label: "会议中心", icon: "◍" },
      { href: "/workflow-builder", label: "工作流", icon: "⇄" },
      { href: "/schedules", label: "日程安排", icon: "🗓" },
      { href: "/automation", label: "自动化规则", icon: "🤖" },
      { href: "/device-console", label: "设备操作台", icon: "🖥" },
    ],
  },
  {
    title: "知识与资产",
    items: [
      { href: "/knowledge", label: "知识库", icon: "❑" },
      { href: "/skill-center", label: "技能中心", icon: "⚡" },
      { href: "/artifacts", label: "成果中心", icon: "▣" },
      { href: "/delivery", label: "交付中心", icon: "📦" },
      { href: "/doc-styles", label: "文档风格", icon: "🎨" },
      { href: "/memory", label: "记忆库", icon: "🧠" },
      { href: "/templates", label: "模板库", icon: "📋" },
      { href: "/assets", label: "素材资产", icon: "🖼" },
    ],
  },
  {
    title: "成长与治理",
    items: [
      { href: "/growth-center", label: "成长中心", icon: "🌱" },
      { href: "/reflection", label: "复盘中心", icon: "🔄" },
      { href: "/training", label: "培训中心", icon: "🎓" },
      { href: "/performance", label: "绩效中心", icon: "📊" },
      { href: "/governance", label: "治理与审计", icon: "⬢" },
      { href: "/external-agents", label: "外部协作", icon: "🔗" },
      { href: "/intervention", label: "干预中心", icon: "🛠" },
      { href: "/skill-growth", label: "技能成长档案", icon: "📈" },
      { href: "/token-stats", label: "Token统计", icon: "🪙" },
      { href: "/logs", label: "审计日志", icon: "📜" },
    ],
  },
];

const COLLAPSED_KEY = "aieos_nav_collapsed";

/** 判断某个导航项是否匹配当前路径。
 *
 * 只用 `pathname === href` 会漏掉所有详情页（`/agents/xxx` 不会高亮
 * "Agent 团队"），用户会失去"我在哪"的感觉。根路径必须精确匹配，
 * 其余用前缀匹配，并避免 `/task` 误匹配 `/tasks`。
 */
function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

function groupHasActive(pathname: string, items: { href: string }[]): boolean {
  return items.some((it) => isActive(pathname, it.href));
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  // 折叠状态：默认全展（老用户习惯），但一旦用户折过就记住。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  useEffect(() => {
    try {
      const raw = localStorage.getItem(COLLAPSED_KEY);
      if (raw) setCollapsed(JSON.parse(raw));
    } catch {
      /* 存储损坏时按"全展"处理即可，不值得打断用户 */
    }
  }, []);

  const toggleGroup = useCallback((title: string) => {
    setCollapsed((prev) => {
      const next = { ...prev, [title]: !prev[title] };
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* 隐私模式下 localStorage 可能不可写；折叠仍然生效，只是不持久 */
      }
      return next;
    });
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

  return (
    <div className="flex h-screen text-gray-100">
      {/* ---------- 侧边栏 ---------- */}
      <aside className="w-60 flex flex-col shrink-0 border-r" style={{ borderColor: "var(--border)", background: "rgba(10,10,16,0.6)" }}>
        <div className="px-5 pt-5 pb-4">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center text-white font-bold"
              style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)" }}>
              ⚡
            </div>
            <div>
              <h1 className="text-base font-bold tracking-wide">AI-EOS</h1>
              <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>人工智能企业操作系统</p>
            </div>
          </div>
        </div>

        <nav className="flex-1 px-3 pb-3 space-y-4 overflow-y-auto">
          {NAV_GROUPS.map((group) => {
            const active = groupHasActive(pathname, group.items);
            // 当前所在分组永远展开——否则用户会"找不到自己现在在哪"
            const isCollapsed = !!collapsed[group.title] && !active;
            return (
              <div key={group.title}>
                <button
                  type="button"
                  onClick={() => toggleGroup(group.title)}
                  aria-expanded={!isCollapsed}
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
                      const active = isActive(pathname, item.href);
                      return (
                        <Link
                          key={item.href}
                          href={item.href}
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
