import { defineConfig } from "vitest/config";

// 前端单元测试：只覆盖纯函数（lib/api.ts 的错误规范化、lib/validate.ts 的校验规则），
// 不碰 React 组件与网络。测试用相对导入，避免依赖 @/* 别名配置。
export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts"],
  },
});
