import Link from "next/link";

/**
 * 404 页面。
 *
 * 改造前没有 not-found 页面，访问不存在的路径会落到 Next.js 默认页
 * （英文、无导航），用户会以为"系统坏了"。这里给出明确的出口。
 */
export default function NotFound() {
  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="card card-static max-w-md w-full text-center space-y-3">
        <div className="text-3xl">🧭</div>
        <h2 className="text-lg font-medium">没有这个页面</h2>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          你访问的地址不存在，也可能是对应功能还未开放。
        </p>
        <div className="flex gap-2 justify-center pt-1">
          <Link href="/" className="btn-primary">回到指挥中心</Link>
          <Link href="/tasks" className="btn-ghost">去看任务</Link>
        </div>
      </div>
    </div>
  );
}
