/**
 * 路由级加载态。
 *
 * 改造前全站没有 loading.tsx：切换到需要请求的页面时，
 * 浏览器停在旧页面上"毫无反应"，用户不知道是在加载还是已经卡死，
 * 于是反复点击导航——这又会叠加并发请求。
 */
export default function MainLoading() {
  return (
    <div className="p-6 space-y-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头骨架 */}
      <div className="space-y-2">
        <div className="h-6 w-48 rounded-md skeleton" />
        <div className="h-3.5 w-72 rounded-md skeleton" />
      </div>

      {/* 指标卡骨架 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="card card-static space-y-3">
            <div className="h-3 w-16 rounded skeleton" />
            <div className="h-7 w-24 rounded skeleton" />
          </div>
        ))}
      </div>

      {/* 列表骨架 */}
      <div className="card card-static space-y-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg skeleton shrink-0" />
            <div className="flex-1 h-3.5 rounded skeleton" />
            <div className="w-16 h-3.5 rounded skeleton shrink-0" />
          </div>
        ))}
      </div>

      <p className="text-xs text-center" style={{ color: "var(--muted)" }}>
        正在加载…
      </p>
    </div>
  );
}
