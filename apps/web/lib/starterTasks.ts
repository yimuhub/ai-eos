// 「从第一件事开始」的示例任务库。
//
// 面向「有公司但一件任务都没有」的新用户：给出行业无关、当天就能下达的
// 具体任务，点击后直接预填工作台的「下达任务」弹窗。
// 本文件保持纯数据 + 纯函数，不引入 React，便于在 node 环境单测。

export type StarterTask = {
  /** 稳定标识，用于按 id 取用（不要用标题当 key） */
  id: string;
  /** 任务标题：会填进「下达任务」弹窗的标题框 */
  title: string;
  /** 任务说明：背景 + 要做什么 + 边界 */
  description: string;
  /** 预期产出：让用户一眼知道做完会拿到什么 */
  deliverable: string;
};

const STARTER_TASKS: readonly StarterTask[] = [
  {
    id: "voice-of-customer",
    title: "整理本周客户反馈，产出 3 条改进建议",
    description:
      "汇总本周来自客服、销售、社群等渠道的客户反馈，合并重复项，按影响范围和紧急程度排序，最后给出 3 条本周就能落地的改进建议。",
    deliverable: "一页客户反馈摘要 + 3 条可执行改进建议（含负责人与预期效果）",
  },
  {
    id: "competitor-scan",
    title: "调研 2 个竞品，给出一页对比结论",
    description:
      "选择 2 个直接竞品，从定位、核心功能、定价、目标客户四个维度做对比，重点回答「我们该学什么、不该学什么」。",
    deliverable: "一页竞品对比表 + 3 条可借鉴动作与 1 条差异化建议",
  },
  {
    id: "weekly-team-plan",
    title: "制定下周团队计划并拆到人",
    description:
      "基于本周进展和遗留问题，列出下周必须完成的 3–5 件事，拆成可交付的最小动作，明确每件事的负责人、截止时间和验收标准。",
    deliverable: "下周团队计划表（事项 / 负责人 / 截止时间 / 验收标准）",
  },
  {
    id: "automation-opportunity",
    title: "梳理重复性工作，找出 1 个可自动化的流程",
    description:
      "盘点团队每周重复 3 次以上的手工流程，估算耗时与出错成本，挑出投入产出比最高的 1 个，写清自动化后的输入、步骤和输出。",
    deliverable: "1 份自动化机会评估（现状耗时 / 预期收益 / 落地步骤）",
  },
];

/** 返回全部示例任务（副本，调用方修改不会污染内置数据）。 */
export function starterTasks(): StarterTask[] {
  return STARTER_TASKS.map((t) => ({ ...t }));
}

/** 按 id 查找示例任务；未知 id 返回 null。 */
export function pickStarterTask(id: string): StarterTask | null {
  const found = STARTER_TASKS.find((t) => t.id === id);
  return found ? { ...found } : null;
}
