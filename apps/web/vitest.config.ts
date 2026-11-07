import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));

// 两层测试：
// - lib/**：纯函数，node 环境（最快）
// - test/components/**：jsdom + testing-library，只验证交互契约（点了按钮发什么请求、
//   失败时用户看到什么），不断言样式
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": root } },
  test: {
    environment: "node",
    environmentMatchGlobs: [["test/components/**/*.test.tsx", "jsdom"]],
    include: ["lib/**/*.test.ts", "test/**/*.test.tsx"],
    setupFiles: ["./test/setup.ts"],
  },
});
