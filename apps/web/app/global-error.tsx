"use client";

/**
 * 根级错误边界。
 *
 * 为什么必须有：`app/(main)/error.tsx` 只能接住其**子**组件的错误，
 * 如果 `(main)/layout.tsx` 自身（登录态校验、侧边栏渲染）抛错，
 * 用户会看到 Next.js 的默认错误页或纯白屏。
 * 根级 error.tsx 不经过任何 layout，因此这里要自带完整的 html/body。
 */
import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[AI-EOS] 应用外壳渲染失败", error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#0a0a10",
          color: "#e5e5ec",
          fontFamily: "system-ui, -apple-system, 'PingFang SC', sans-serif",
          padding: 24,
        }}
      >
        <div style={{ maxWidth: 460, textAlign: "center" }}>
          <div style={{ fontSize: 34, marginBottom: 12 }}>🛠</div>
          <h1 style={{ fontSize: 18, fontWeight: 500, margin: "0 0 8px" }}>
            应用未能正常启动
          </h1>
          <p style={{ fontSize: 13, color: "#8b8b99", margin: "0 0 6px" }}>
            {error.message || "未知错误"}
          </p>
          {error.digest && (
            <p style={{ fontSize: 12, color: "#8b8b99", margin: "0 0 18px" }}>
              错误编号：<code>{error.digest}</code>
            </p>
          )}
          <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
            <button
              onClick={reset}
              style={{
                padding: "8px 16px", borderRadius: 8, border: "none",
                background: "linear-gradient(135deg,#6366f1,#22d3ee)",
                color: "#fff", cursor: "pointer", fontSize: 13,
              }}
            >
              重试
            </button>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: "8px 16px", borderRadius: 8,
                border: "1px solid #23232e", background: "transparent",
                color: "#8b8b99", cursor: "pointer", fontSize: 13,
              }}
            >
              重新加载
            </button>
          </div>
        </div>
      </body>
    </html>
  );
}
