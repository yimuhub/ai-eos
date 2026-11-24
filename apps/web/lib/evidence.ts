// 质量证据的展示口径（纯函数，可单测）
//
// 为什么单独成文件：这些数字是"给老板看的结论"，口径必须唯一。
// 页面各写一套 `toFixed(1)` 必然出现「执行页 85.7%、看板 86%」这种
// 同一事实两个数的尴尬。这里统一三件事：
//   1. 没有样本显示 "—"，不显示 0% —— "没数据"和"全靠人工"必须能分开；
//   2. 百分比只在展示层四舍五入，计算与存储保留原值；
//   3. 闸门证据按"节点 × 轮次"归并，失败项要能定位到具体规则与实际值。

export type GateCheck = {
  rule: string;
  passed: boolean;
  expected?: unknown;
  actual?: unknown;
  blocking?: boolean;
  detail?: string;
};

export type GateRecord = {
  node_key: string;
  node_name?: string;
  attempt?: number;
  gate_round?: number;
  stage?: string;
  checks?: GateCheck[];
  passed: boolean;
  blocking?: boolean;
  degraded?: boolean;
  score?: number | null;
};

export type AutonomyLedger = {
  ai_steps: number;
  human_steps: number;
  manual_logs: number;
  total: number;
  autonomy_rate: number | null;
  manual_reasons?: Record<string, number>;
  executions?: number;
};

export type GateSummary = {
  /** 闸门记录条数（一个节点返工一次会产生两条） */
  rounds: number;
  /** 最终没通过的节点数（按 node_key 去重，取其最后一轮） */
  failedNodes: string[];
  /** 最终仍降级的节点数（按有效最后一轮去重，历史返工不重复计数） */
  degraded: number;
  /** 因闸门触发返工的节点（gate_round=0 是首检，1 已是第一次返工） */
  reworked: string[];
  /** 唯一真实值为"没跑闸门"：空数组 */
  configured: boolean;
};

function gatePosition(g: GateRecord): [number, number] {
  return [g.attempt ?? 1, g.gate_round ?? 0];
}

/** 同一 attempt/round 的重复证据取失败优先，避免输入顺序改变结论。 */
function isLaterGate(candidate: GateRecord, current: GateRecord): boolean {
  const a = gatePosition(candidate);
  const b = gatePosition(current);
  if (a[0] !== b[0]) return a[0] > b[0];
  if (a[1] !== b[1]) return a[1] > b[1];
  if (candidate.passed !== current.passed) return !candidate.passed;
  return JSON.stringify(candidate) > JSON.stringify(current);
}

/** 闸门证据汇总。按 node_key 取有效 attempt 的最后 gate_round，
 *  避免"第一轮没过、返工后过了"被算成失败，也不依赖数据库返回顺序。 */
export function summarizeGates(gates: GateRecord[] | null | undefined): GateSummary {
  const rows = gates ?? [];
  const lastByNode = new Map<string, GateRecord>();
  const roundsSeen = new Set<string>();
  for (const g of rows) {
    const current = lastByNode.get(g.node_key);
    if (!current || isLaterGate(g, current)) lastByNode.set(g.node_key, g);
    const [attempt, round] = gatePosition(g);
    roundsSeen.add(`${g.node_key}#${attempt}#${round}`);
  }
  const failedNodes = Array.from(lastByNode.values())
    .filter((g) => !g.passed)
    .map((g) => g.node_key)
    .sort();
  const reworked = Array.from(lastByNode.keys()).filter((k) =>
    rows.some((g) => g.node_key === k && gatePosition(g)[1] > 0),
  ).sort();
  const degraded = Array.from(lastByNode.values()).filter((g) => g.degraded).length;
  return {
    rounds: roundsSeen.size,
    failedNodes,
    degraded,
    reworked,
    configured: rows.length > 0,
  };
}

/** 某条闸门记录里没过的断言，给页面直接列"规则 / 实际 / 要求"。 */
export function failedChecks(g: GateRecord): GateCheck[] {
  return (g.checks ?? []).filter((c) => !c.passed);
}

/** 断言值的展示：数组拼成"，"，空值显示 "n/a"，布尔显示"是/否"。 */
export function formatCheckValue(v: unknown): string {
  if (v === null || v === undefined) return "n/a";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (Array.isArray(v)) return v.length ? v.join("、") : "（空）";
  if (typeof v === "object") return JSON.stringify(v);
  const s = String(v);
  return s.length > 40 ? `${s.slice(0, 40)}…` : s;
}

/** 自主率文案：无样本一律 "—"，并说明分母。 */
export function formatAutonomy(ledger: AutonomyLedger | null | undefined): string {
  if (!ledger || ledger.autonomy_rate === null || ledger.autonomy_rate === undefined) {
    return "—（暂无样本）";
  }
  const pct = (ledger.autonomy_rate * 100).toFixed(1);
  return `${pct}%（AI ${ledger.ai_steps} / 共 ${ledger.total}）`;
}

/** 自主率明细：把分母三项拆开，解释这个百分比是怎么来的。 */
export function autonomyBreakdown(ledger: AutonomyLedger | null | undefined): string {
  if (!ledger) return "未取到账本";
  const reasons = ledger.manual_reasons ?? {};
  const reasonText = Object.entries(reasons)
    .map(([k, v]) => `${MANUAL_REASON_LABELS[k] ?? k} ${v}`)
    .join(" · ");
  const base = `AI 步骤 ${ledger.ai_steps} · 人工节点 ${ledger.human_steps} · 手记 ${ledger.manual_logs}`;
  return reasonText ? `${base}（${reasonText}）` : base;
}

export const MANUAL_REASON_LABELS: Record<string, string> = {
  unsupported: "AI 做不了",
  quality: "质量不达标",
  judgment: "需要人判断",
  external: "等外部回复",
};

export const MANUAL_REASONS = ["unsupported", "quality", "judgment", "external"] as const;
export type ManualReason = (typeof MANUAL_REASONS)[number];

/** 胜率：分母是"有明确胜负"的 case，tie 不计入；无样本显示 "—"。 */
export function formatWinRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

/** token 比：多 Agent 总 token ÷ 基线总 token。 */
export function formatTokenRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return "—";
  return `${ratio.toFixed(2)}×`;
}

export const VERDICT_LABELS: Record<string, string> = {
  multi_better: "多 Agent 更好",
  baseline_better: "单次基线更好",
  tie: "平局",
};

export function verdictLabel(v: string | null | undefined): string {
  if (!v) return "—";
  return VERDICT_LABELS[v] ?? v;
}

export const EXEC_STATUS_LABELS: Record<string, string> = {
  PENDING: "待跑",
  RUNNING: "运行中",
  COMPLETED: "已完成",
  FAILED: "失败",
  PAUSED: "已暂停",
  CANCELLED: "已取消",
  WAITING_INPUT: "等输入",
  WAITING_APPROVAL: "等审批",
};

export function executionStatusLabel(s: string | null | undefined): string {
  if (!s) return "—";
  return EXEC_STATUS_LABELS[s] ?? s;
}

/** 运行状态是否已经停下（决定轮询要不要继续、按钮要不要给返工）。 */
export function isTerminalStatus(s: string | null | undefined): boolean {
  return s === "COMPLETED" || s === "FAILED" || s === "CANCELLED";
}

/** 状态色：只给"已完成/失败"两个强语义上色，其余中性，避免满屏红绿。 */
export function executionStatusTone(s: string | null | undefined): "ok" | "bad" | "muted" {
  if (s === "COMPLETED") return "ok";
  if (s === "FAILED" || s === "CANCELLED") return "bad";
  return "muted";
}
