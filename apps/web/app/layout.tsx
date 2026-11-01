import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import PerfMonitor from "@/components/PerfMonitor";
import "./globals.css";

const geistSans = localFont({
  src: "./fonts/GeistVF.woff",
  variable: "--font-geist-sans",
  weight: "100 900",
});
const geistMono = localFont({
  src: "./fonts/GeistMonoVF.woff",
  variable: "--font-geist-mono",
  weight: "100 900",
});

export const metadata: Metadata = {
  title: "AI-EOS · 人工智能企业操作系统",
  description:
    "用 AI 数字员工组建可运转的公司：下达目标而非步骤，自主拆解、并行执行、交付成果。",
};

/**
 * 移动端 / PWA：viewportFit=cover 让刘海屏内容铺满，
 * themeColor 与 --background 保持一致（Android 状态栏/任务切换不闪白）。
 * manifest 由 app/manifest.ts 元数据路由自动注入 <link rel="manifest">。
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#09090f",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
        {/* 会话级交互延迟采样：不渲染 UI，只写 sessionStorage */}
        <PerfMonitor />
      </body>
    </html>
  );
}
