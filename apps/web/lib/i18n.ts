// 界面中英文映射：所有展示给用户的标签统一转中文
export const TASK_STATUS_ZH: Record<string, string> = {
  TODO: "待办",
  PLANNING: "规划中",
  RUNNING: "执行中",
  WAITING: "等待中",
  PAUSED: "已暂停",
  REVIEW: "审核中",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  ALL: "全部",
};

export const AGENT_STATUS_ZH: Record<string, string> = {
  ACTIVE: "在线",
  IDLE: "空闲",
  RUNNING: "工作中",
  PAUSED: "已暂停",
  DISABLED: "已禁用",
  ERROR: "异常",
  ALL: "全部",
};

export const PRIORITY_ZH: Record<string, string> = {
  LOW: "低",
  NORMAL: "普通",
  HIGH: "高",
  URGENT: "紧急",
};

export const ASSET_TYPE_ZH: Record<string, string> = {
  DOCUMENT: "文档",
  IMAGE: "图片",
  CODE: "代码",
  DATA: "数据",
  REPORT: "报告",
  OTHER: "其他",
};

export const EVENT_TYPE_ZH: Record<string, string> = {
  "task.created": "任务创建",
  "task.status_changed": "任务状态变更",
  "agent.created": "员工创建",
  "agent.started_task": "员工开始工作",
  "agent.completed_task": "员工完成任务",
  "agent.failed": "员工执行失败",
  "company.created": "公司创建",
  "department.created": "部门创建",
  "orchestration.started": "协同任务启动",
  "orchestration.failed": "协同任务失败",
  "orchestrator.decomposed": "CEO 拆解子任务",
  "orchestrator.subtask_failed": "子任务失败",
  "message.sent": "消息发送",
  "intervention.added": "人工介入",
  "asset.created": "成果入库",
  "asset.uploaded": "文件上传",
  execution: "运行记录",
  resumed: "恢复执行",
  error: "错误",
};

export const EXEC_STATUS_ZH: Record<string, string> = {
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  paused: "已暂停",
  cancelled: "已取消",
};

export function zh<T extends Record<string, string>>(map: T, key: string): string {
  return map[key] ?? key;
}
