# AI-EOS 移动端（V4.1）· 老板视角

Expo Router + React Native，把 AI-EOS 后端 API 包进老板随身 App。

## 四个 Tab

| Tab | 文件 | 能力 |
|---|---|---|
| 📊 KPI | `app/index.tsx` | 拉 `/dashboard` 显示 Agent / 任务 / 项目 / 专家计数，无数据时显示引导 |
| ⚡ 一句话 | `app/command.tsx` | `/command/plan` 出方案 → 老板可视化确认 → `/command/execute` → `/command/report` 看结果；带📎附件口 |
| ✍️ 签字门 | `app/approve.tsx` | 关键步骤（作战室签字 / 自动化审批 / 交付验收）聚合成待签列表，通过/驳回；呼吸灯标注 |
| 🔌 连接 | `app/server.tsx` | 配 base URL（本机便携 / 远程自托管）+ 登录/注册 + 测试连接；token 走 `expo-secure-store` 加密持久化 |

## 设计原则（延续 V3/V4）

- **老板视角 ≠ 全功能桌面壳**：移动端只暴露高频刚需，重操作回到桌面壳或浏览器。
- **可视化确认点永不消失**：一句话下达必须出方案 → 老板点"确认执行"才真跑（产品设计哲学）。
- **附件口硬要求**：输入页必带📎（V4.2 接 `react-native-image-picker` 真选图）。
- **API 客户端独立**：`lib/api.ts` 与桌面壳共用同套后端，`/auth` `/command` `/experts` `/dashboard` 全通。

## 运行（需 Expo Go 或 Android Studio）

```bash
cd mobile
npm install
npx expo start
# 手机 Expo Go 扫码，或
npx expo start --android / --ios
```

## 出包

```bash
npx eas build -p android   # 云打包 APK/AAB
npx eas build -p ios       # 云打包 ipa（需 Apple 账号）
# 本地（需 Xcode / Android SDK）：
npx react-native run-android / run-ios
```

## 与桌面壳关系

- 桌面壳（V4.0-P1）：全功能、双击即开、数据随 `data/` 走。
- 移动端（V4.1）：老板随身视角，**通过 Service Connection 页连到你已起的服务器**（本机便携 8001 或远程）。
- 共用 API 层：`/auth` `/command` `/experts` `/dashboard` `/governance`（P2 接上审批落地）。

## 当前实现状态（V4.1 首版）

| 项 | 状态 |
|---|---|
| Metro 打包 900 modules | ✅ 已验证（HBC 2.5MB） |
| TypeScript 类型检查 | ✅ 归零 |
| 四个 Tab 路由 | ✅ 已实现 |
| 一句话下达（含📎附件口） | ✅ 已实现 |
| 签字门真审批 API | 🕐 V4.2 接 `/governance`（当前用 Alert 回执） |
| 呼吸灯动画 | 🕐 V4.2 接 `react-native-reanimated` |
| 附件选图 | 🕐 V4.2 接 `react-native-image-picker` |
| EAS 云打包 | 🕐 V4.2 签名 + 公证 |
