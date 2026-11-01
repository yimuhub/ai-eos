import type { MetadataRoute } from "next";

/**
 * PWA 清单：Next 会自动把它挂到 /manifest.webmanifest 并注入 <link rel="manifest">。
 * 颜色与 globals.css 的深色主题保持一致（--background #09090f）。
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "AI-EOS 移动端",
    short_name: "AI-EOS",
    description: "在手机上审批、验收与只读查看作战会话。",
    start_url: "/m",
    scope: "/",
    display: "standalone",
    background_color: "#09090f",
    theme_color: "#09090f",
    lang: "zh-CN",
    icons: [
      {
        src: "/icon.svg",
        sizes: "any",
        type: "image/svg+xml",
        purpose: "any",
      },
    ],
  };
}
