// 免 Key 演示模式的交付物模板（纯函数，无网络 / 无 DOM / 无后端调用）。
//
// 演示的终点不该只是一段聊天气泡：用户要能把它带走。
// 这里集中放三样东西，供 components/DemoMode.tsx 复用：
// - DEMO_TASK_TITLE：示例任务标题
// - DEMO_DELIVERABLE_MARKDOWN：可下载的 Markdown 正文
// - demoDeliverableFileName / demoDeliverableMarkdown：带时间戳的文件名与完整文件内容
//
// 全部内容都是预设模板，最后一行固定标注「🎭 演示」，不冒充真实模型输出。

export const DEMO_TASK_TITLE = "示例任务：为新品发布会写一条朋友圈文案";

export const DEMO_DISCLAIMER = "🎭 演示模板生成，非真实模型输出";

/** 交付物正文：标题 + 三段式文案 + 素材说明，结尾带演示声明。 */
export const DEMO_DELIVERABLE_MARKDOWN = `# 🎉 新品发布会 · 朋友圈文案

> 本文件由免 API Key 演示模式生成，用于展示「任务 → 交付物」的完整闭环。

## 文案正文

🎉 新品发布会 · 邀请你一起来

我们把「开箱即用的数字员工团队」装进了 AI-EOS，
3 步配置，1 分钟开工。

👉 今晚 8 点，线上见！

## 素材说明

- 结构：钩子 → 卖点 → 行动号召（三段式）
- 语气：轻松口语化，面向朋友圈熟人场景
- 长度：每段不超过 2 行，手机一屏可读完
- 话题标签：#AI_EOS #数字员工

---

${DEMO_DISCLAIMER}`;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** 文件名里的日期：本地时区，YYYYMMDD。 */
function dateStamp(d: Date): string {
  return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}`;
}

/**
 * 下载文件名，例如 `AI-EOS-演示交付物-20261101.md`。
 * 统一清除路径分隔符与文件系统保留字符，防止被解释成目录或非法文件名。
 */
export function demoDeliverableFileName(now: Date = new Date()): string {
  const name = `AI-EOS-演示交付物-${dateStamp(now)}.md`;
  return name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-");
}

/**
 * 完整文件内容：任务标题 + 生成时间 + 演示声明，然后接正文。
 * 同一 Date 多次调用结果一致（可直接用于测试与快照）。
 */
export function demoDeliverableMarkdown(now: Date = new Date()): string {
  const time =
    `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} ` +
    `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
  return `# ${DEMO_TASK_TITLE}

- 生成时间：${time}（本地时区）
- 来源：免 API Key 演示模式 · 纯模板生成，未调用真实模型、未写入数据库

> ${DEMO_DISCLAIMER}

---

${DEMO_DELIVERABLE_MARKDOWN}
`;
}
