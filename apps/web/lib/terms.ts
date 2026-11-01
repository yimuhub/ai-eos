// 用户可见术语的单一事实源（D6「术语与可达性」）
//
// ## 为什么需要它
// 界面文案长期混用：同一个东西，导航里叫「Agent 团队」，向导里叫「AI 员工」，
// 审批页里又叫「Agent」；产出在成果中心叫「成果」，在工作台叫「产出」，
// 在交付中心叫「交付物」。用户要在脑内做一次翻译才能确认「这是不是同一个东西」。
//
// 本文件只冻结「推荐词」，供新代码 / 文案评审引用；**不强制改造历史命名**——
// 数据库字段、接口路径、组件名（agent、artifact、warroom……）保持原样，
// 只要求用户看得见的文字统一到 TERMS 的值。
//
// 术语表文档：docs/术语表.md（与这里的值逐条对应，terms.test.ts 会校验一致性）

export const TERMS = Object.freeze({
  /** 被雇佣、有岗位与人设的数字同事。不要写 Agent / 智能体 / 助手 / 数字员工 */
  aiEmployee: "AI 员工",
  /** 一件被派下去、有状态与负责人的工作。不要写「目标」「指令」（那是任务的属性/动作） */
  task: "任务",
  /** 员工交付的成品（报告、表格、代码）。不要写「产出」「结果」「产物」 */
  deliverable: "交付物",
  /** 组织与计费的边界。不要写「团队」「租户」（团队是公司内的一群人） */
  company: "公司",
  /** 公司内的组织单元。不要写「团队」「组」 */
  department: "部门",
  /** 一段连续的对话上下文。不要写「聊天」「对话记录」 */
  session: "会话",
  /** 首页三栏工作区。不要写「首页」「工作区」 */
  workspace: "工作台",
  /** 全局总览页。不要写「看板」「驾驶舱」 */
  commandCenter: "指挥中心",
  /** 围绕单个目标的实时执行空间。不要写「战场」「作战会话」 */
  warRoom: "作战室",
  /** 危险 / 外发动作的人工放行。不要写「批准」「审核」 */
  approval: "审批",
  /** 可雇佣的人数额度。不要写「座位」「License 数」 */
  seat: "席位",
  /** 大模型。首次出现可写「大模型」，同屏其余位置统一用「模型」 */
  model: "模型",
} as const);

export type TermKey = keyof typeof TERMS;

/**
 * 取规范词。未知 key 返回 undefined（调用方据此决定回退到原文案），
 * 不做拼写纠错——静默纠错会让「术语表没覆盖到」这件事永远暴露不出来。
 */
export function canonicalTerm(key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(TERMS, key)
    ? TERMS[key as TermKey]
    : undefined;
}
