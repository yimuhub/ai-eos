# AI-EOS 前端设计体系（UI 审美规范）

用户多次强调：界面必须有现代精致审美，拒绝简陋堆砌。改任何 UI 必须套用本体系。

## 全局基调（globals.css）
- 深空底色 `#09090f` + 双色氛围光晕（靛蓝/青 radial-gradient）
- 设计令牌：--surface / --border / --muted / --accent
- 字体：PingFang SC 优先，tabular-nums 数字

## 组件类（必须用，不要手写旧 Tailwind 灰色系）
- `.card`：玻璃拟态卡片，hover 上浮 + 靛蓝辉光阴影；静态卡加 `card-static`
- `.btn-primary`：靛蓝→紫渐变 + 光晕投影 + 按压反馈；危险操作用红橙渐变内联样式
- `.btn-ghost`：描边幽灵按钮
- `.input`：聚焦靛蓝描边 + 光环
- `.text-gradient`：渐变文字

## 页面结构模式
1. 页面容器：`p-6 max-w-[1400px] mx-auto animate-fade-up`
2. 页头：大标题(text-2xl tracking-tight) + 灰色副标题 + 右侧按钮组
3. KPI 卡：顶部渐变色条 + 彩色大数字 + 灰色小标签
4. 卡片头像/图标：6 色循环渐变（AVATAR_COLORS）
5. 状态点：pulse-dot 呼吸动画
6. 批量操作条：底部居中浮动毛玻璃胶囊条
7. 弹窗：毛玻璃遮罩 + 渐变深底面板 + 底部操作区带上边框
8. 空状态：card-static 居中灰字

## 审美细节要求
- 间距有节奏（space-y-5/gap-3），不挤不散
- 色彩有层次（每类信息独立色相，徽章半透明色底）
- 微交互（hover 渐显操作按钮、transition-all、入场动画）
- 拒绝默认白底、拒绝生硬边框堆叠
