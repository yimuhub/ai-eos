"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api, clearToken } from "@/lib/api";

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

export default function Layout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  async function logout() {
    try {
      await clearToken();
    } catch {}
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

        <nav className="flex-1 px-3 pb-3 space-y-5 overflow-y-auto">
          {NAV_GROUPS.map((group) => (
            <div key={group.title}>
              <p className="px-2 mb-1.5 text-[10px] font-semibold tracking-widest uppercase" style={{ color: "#55555f" }}>
                {group.title}
              </p>
              <div className="space-y-0.5">
                {group.items.map((item) => {
                  const active = pathname === item.href;
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
            </div>
          ))}
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
