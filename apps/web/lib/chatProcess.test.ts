import { describe, expect, it } from "vitest";
import {
  applyChatProcessEvent,
  formatDuration,
  newChatProcess,
  processActivity,
  processDuration,
  processFromMeta,
  processIdleMs,
} from "./chatProcess";

describe("applyChatProcessEvent", () => {
  it("把阶段、模型、思考、工具收成一条时间线", () => {
    let p = newChatProcess(1000);
    p = applyChatProcessEvent(p, { type: "status", text: "正在检索记忆与知识库…" }, 1100);
    p = applyChatProcessEvent(p, {
      type: "model", model_id: "m1", model_name: "Sage", attempt: 2,
    }, 1200);
    p = applyChatProcessEvent(p, { type: "reasoning", text: "先核对数据。" }, 1300);
    p = applyChatProcessEvent(p, {
      type: "tool_start", id: "t1", name: "web_search",
      label: "联网搜索", args: "竞品定价",
    }, 1400);
    expect(processActivity(p)).toBe("联网搜索 · 竞品定价");

    p = applyChatProcessEvent(p, {
      type: "tool_end", id: "t1", ok: true,
      summary: "找到 3 条来源", duration_ms: 1200,
    }, 2600);
    expect(p.steps.find((s) => s.id === "t1")).toMatchObject({
      status: "done", durationMs: 1200,
      detail: "竞品定价 · 找到 3 条来源",
    });

    p = applyChatProcessEvent(p, { type: "delta", text: "结论如下" }, 2700);
    p = applyChatProcessEvent(p, { type: "done", model: "Sage" }, 3000);
    expect(p.status).toBe("done");
    expect(p.model).toBe("Sage");
    expect(p.steps.map((s) => s.label)).toEqual([
      "正在检索记忆与知识库…",
      "调用模型 · Sage",
      "联网搜索",
      "正在生成回答…",
    ]);
    expect(p.steps[1].detail).toContain("已降级");
    expect(p.reasoning).toBe("先核对数据。");
    expect(processDuration(p, 9999)).toBe(2000);
  });

  it("模型失败也留在时间线上，不影响最终成功", () => {
    let p = newChatProcess(0);
    p = applyChatProcessEvent(p, {
      type: "model_error", model_id: "bad", model_name: "坏模型", message: "HTTP 500",
    }, 100);
    p = applyChatProcessEvent(p, { type: "model", model_name: "好模型", attempt: 2 }, 200);
    p = applyChatProcessEvent(p, { type: "done" }, 300);
    expect(p.steps[0]).toMatchObject({
      kind: "model_error", status: "error", detail: "HTTP 500",
    });
    expect(p.status).toBe("done");
  });

  it("error/done 之后不再显示卡住", () => {
    let p = newChatProcess(0);
    p = applyChatProcessEvent(p, { type: "error", message: "模型不可用" }, 1000);
    expect(p.status).toBe("error");
    expect(p.error).toBe("模型不可用");
    expect(processIdleMs(p, 999999)).toBe(0);
  });

  it("未知事件不改变状态", () => {
    const p = newChatProcess(0);
    expect(applyChatProcessEvent(p, { type: "heartbeat" })).toBe(p);
  });
});

describe("processFromMeta", () => {
  it("历史 meta 能还原步骤、思考与耗时", () => {
    const p = processFromMeta({
      process: {
        duration_ms: 4200,
        model: "Sage",
        reasoning: "先搜索。",
        steps: [
          { kind: "stage", label: "正在检索记忆与知识库…", status: "done" },
          { kind: "tool", label: "联网搜索", status: "done", detail: "找到 2 条来源", duration_ms: 800 },
          { kind: "model_error", label: "坏模型 调用失败", status: "error" },
        ],
      },
    });
    expect(p).not.toBeNull();
    expect(p!.steps).toHaveLength(3);
    expect(p!.steps[1]).toMatchObject({ id: "h-1", detail: "找到 2 条来源", durationMs: 800 });
    expect(p!.reasoning).toBe("先搜索。");
    expect(processDuration(p!, 0)).toBe(4200);
  });

  it("没有 process / 空 process 返回 null", () => {
    expect(processFromMeta(null)).toBeNull();
    expect(processFromMeta({ citations: [] })).toBeNull();
    expect(processFromMeta({ process: {} })).toBeNull();
  });
});

describe("formatDuration", () => {
  it("秒 / 分钟边界", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(400)).toBe("<1s");
    expect(formatDuration(12_000)).toBe("12s");
    expect(formatDuration(63_000)).toBe("1m 3s");
    expect(formatDuration(120_000)).toBe("2m");
  });
});
