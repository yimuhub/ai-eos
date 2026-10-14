"use client";

/**
 * 分段错误边界。
 *
 * 改造前全站没有任何 error.tsx：任何一个页面组件抛错（例如接口返回
 * 与预期结构不符导致 `undefined.map`），用户看到的是 Next.js 的黑屏
 * 或整页白屏，既没有解释也没有恢复入口，只能刷新或放弃。
 *
 * 这里保留侧边栏可用，让用户能直接切到别的页面继续工作。
 */
import { useEffect } from "react";

export default function MainError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 前端控制台留一份，便于用户截图反馈；后端侧对应请求也会有自己的日志
    console.error("[AI-EOS] 页面渲染失败", error);
  }, [error]);

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-4 animate-fade-up">
      <div className="card card-static space-y-3">
        <div className="text-3xl">🧯</div>
        <h2 className="text-lg font-medium">这个页面出错了</h2>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          错误信息：{error.message || "未知错误"}
        </p>
        {error.digest && (
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            错误编号：<code>{error.digest}</code>（可在后端日志中检索）
          </p>
        )}
        <div className="flex gap-2 pt-1">
          <button onClick={reset} className="btn-primary">重试</button>
          <button
            onClick={() => window.location.reload()}
            className="btn-ghost"
          >
            重新加载页面
          </button>
        </div>
        <p className="text-xs" style={{ color: "var(--muted)" }}>
          左侧导航仍可使用，可以先切换到其它页面。
        </p>
      </div>
    </div>
  );
}
