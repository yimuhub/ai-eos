import { describe, expect, it } from "vitest";
import {
  autonomyBreakdown,
  executionStatusLabel,
  failedChecks,
  formatAutonomy,
  formatCheckValue,
  formatTokenRatio,
  formatWinRate,
  isTerminalStatus,
  summarizeGates,
  verdictLabel,
} from "./evidence";

const gate = (over: Record<string, unknown> = {}) => ({
  node_key: "a", passed: true, checks: [], gate_round: 1, ...over,
});

describe("summarizeGates", () => {
  it("空证据返回 configured=false，不伪造成「全部通过」", () => {
    const s = summarizeGates([]);
    expect(s.configured).toBe(false);
    expect(s.rounds).toBe(0);
    expect(s.failedNodes).toEqual([]);
  });

  it("第一轮没过、返工后过了，不算失败节点，但记为返工", () => {
    const s = summarizeGates([
      gate({ passed: false, gate_round: 1 }),
      gate({ passed: true, gate_round: 2 }),
    ]);
    expect(s.failedNodes).toEqual([]);
    expect(s.reworked).toEqual(["a"]);
    expect(s.rounds).toBe(2);
  });

  it("gate_round=1 就是第一次返工，首检从 0 开始", () => {
    const s = summarizeGates([
      gate({ passed: false, gate_round: 0 }),
      gate({ passed: true, gate_round: 1 }),
    ]);
    expect(s.failedNodes).toEqual([]);
    expect(s.reworked).toEqual(["a"]);
    expect(s.rounds).toBe(2);
  });

  it("乱序证据按 attempt 再按 gate_round 取最终值，轮次跨 attempt 不合并", () => {
    const rows = [
      gate({ node_key: "b", attempt: 1, gate_round: 2, passed: true }),
      gate({ node_key: "a", attempt: 1, gate_round: 1, passed: false }),
      gate({ node_key: "b", attempt: 2, gate_round: 0, passed: false }),
      gate({ node_key: "a", attempt: 2, gate_round: 0, passed: true }),
      gate({ node_key: "a", attempt: 1, gate_round: 0, passed: false }),
    ];
    const expected = {
      rounds: 5, failedNodes: ["b"], degraded: 0,
      reworked: ["a", "b"], configured: true,
    };
    expect(summarizeGates(rows)).toEqual(expected);
    expect(summarizeGates([...rows].reverse())).toEqual(expected);
  });

  it("同一有效轮次的冲突证据失败优先，且不受输入顺序影响", () => {
    const rows = [
      gate({ attempt: 2, gate_round: 1, passed: true }),
      gate({ attempt: 2, gate_round: 1, passed: false }),
    ];
    expect(summarizeGates(rows)).toEqual(summarizeGates([...rows].reverse()));
    expect(summarizeGates(rows).failedNodes).toEqual(["a"]);
  });

  it("两个节点各自独立判定，降级单列", () => {
    const s = summarizeGates([
      gate({ node_key: "a", passed: true }),
      gate({ node_key: "b", passed: false }),
      gate({ node_key: "c", passed: true, degraded: true }),
    ]);
    expect(s.failedNodes).toEqual(["b"]);
    expect(s.degraded).toBe(1);
  });

  it("降级 headline 只计有效最终闸门，不计已被返工清除的历史轮次", () => {
    const rows = [
      gate({ node_key: "a", attempt: 1, gate_round: 0, degraded: true, passed: false }),
      gate({ node_key: "a", attempt: 1, gate_round: 1, degraded: false, passed: true }),
      gate({ node_key: "b", attempt: 1, gate_round: 0, degraded: true, passed: false }),
      gate({ node_key: "b", attempt: 1, gate_round: 1, degraded: true, passed: true }),
      gate({ node_key: "c", attempt: 1, gate_round: 0, degraded: false, passed: true }),
      gate({ node_key: "c", attempt: 2, gate_round: 0, degraded: true, passed: false }),
    ];
    expect(summarizeGates(rows)).toEqual({
      rounds: 6, failedNodes: ["c"], degraded: 2,
      reworked: ["a", "b"], configured: true,
    });
    expect(summarizeGates([...rows].reverse()).degraded).toBe(2);
  });
});

describe("failedChecks / formatCheckValue", () => {
  it("只列没过且能给出实际值", () => {
    const rows = failedChecks(
      gate({
        checks: [
          { rule: "min_chars", passed: false, expected: 800, actual: 210 },
          { rule: "min_headings", passed: true, expected: 3, actual: 5 },
        ],
      }) as never,
    );
    expect(rows.map((r) => r.rule)).toEqual(["min_chars"]);
  });

  it("空值与布尔值不输出 undefined/true", () => {
    expect(formatCheckValue(null)).toBe("n/a");
    expect(formatCheckValue(true)).toBe("是");
    expect(formatCheckValue(["x", "y"])).toBe("x、y");
    expect(formatCheckValue([])).toBe("（空）");
  });
});

describe("formatAutonomy", () => {
  it("无样本返回破折号，不返回 0%", () => {
    expect(formatAutonomy({ ai_steps: 0, human_steps: 0, manual_logs: 0, total: 0, autonomy_rate: null }))
      .toBe("—（暂无样本）");
    expect(formatAutonomy(null)).toBe("—（暂无样本）");
  });

  it("有样本时百分比与分母同时给出", () => {
    expect(formatAutonomy({ ai_steps: 6, human_steps: 1, manual_logs: 0, total: 7, autonomy_rate: 6 / 7 }))
      .toBe("85.7%（AI 6 / 共 7）");
  });

  it("明细把三项分母拆开，并翻译介入原因", () => {
    expect(autonomyBreakdown({
      ai_steps: 3, human_steps: 1, manual_logs: 2, total: 6, autonomy_rate: 0.5,
      manual_reasons: { quality: 2 },
    })).toBe("AI 步骤 3 · 人工节点 1 · 手记 2（质量不达标 2）");
  });
});

describe("格式化边界", () => {
  it("胜率/token 比为 null 时给破折号，不显示 NaN", () => {
    expect(formatWinRate(null)).toBe("—");
    expect(formatWinRate(2 / 3)).toBe("66.7%");
    expect(formatTokenRatio(null)).toBe("—");
    expect(formatTokenRatio(1.8)).toBe("1.80×");
  });

  it("未知判定与未知状态原样显示，不丢信息", () => {
    expect(verdictLabel("multi_better")).toBe("多 Agent 更好");
    expect(verdictLabel("weird")).toBe("weird");
    expect(executionStatusLabel("COMPLETED")).toBe("已完成");
    expect(executionStatusLabel("WHATEVER")).toBe("WHATEVER");
  });

  it("只有三种状态算结束，PAUSED 还会被继续", () => {
    expect(isTerminalStatus("COMPLETED")).toBe(true);
    expect(isTerminalStatus("FAILED")).toBe(true);
    expect(isTerminalStatus("PAUSED")).toBe(false);
  });
});
