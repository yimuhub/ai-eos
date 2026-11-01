import { describe, expect, it } from "vitest";
import {
  approvalActionZh,
  progressPct,
  sessionStatusMeta,
  sortByUpdatedDesc,
  taskStatusZh,
  timeLabel,
  validateDeclineComment,
} from "./mobile";

const NOW = new Date(2026, 9, 20, 12, 0, 0).getTime();
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();

describe("taskStatusZh", () => {
  it("映射已知状态并保留未知状态原文", () => {
    expect(taskStatusZh("COMPLETED")).toBe("已完成");
    expect(taskStatusZh("RUNNING")).toBe("执行中");
    expect(taskStatusZh("SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(taskStatusZh("")).toBe("未知");
    expect(taskStatusZh(null)).toBe("未知");
  });

  it("作战室成员任务的小写状态同样转中文", () => {
    expect(taskStatusZh("submitted")).toBe("已提交");
    expect(taskStatusZh("approved")).toBe("已通过");
  });
});

describe("approvalActionZh", () => {
  it("已知动作转中文，未知动作回退原值，空值给兜底", () => {
    expect(approvalActionZh("artifact_approve")).toBe("成果验收");
    expect(approvalActionZh("computer_command")).toBe("高危本机操作");
    expect(approvalActionZh("custom_action")).toBe("custom_action");
    expect(approvalActionZh(undefined)).toBe("待审批操作");
  });
});

describe("timeLabel", () => {
  it("按 20 秒 / 5 分钟 / 3 小时 / 2 天 分档", () => {
    expect(timeLabel(iso(20_000), NOW)).toBe("刚刚");
    expect(timeLabel(iso(5 * 60_000), NOW)).toBe("5 分钟前");
    expect(timeLabel(iso(3 * 3_600_000), NOW)).toBe("3 小时前");
    expect(timeLabel(iso(2 * 86_400_000), NOW)).toBe("2 天前");
  });

  it("超过 7 天显示 MM-DD HH:mm，空值/非法值显示 —", () => {
    expect(timeLabel(iso(30 * 86_400_000), NOW)).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(timeLabel("", NOW)).toBe("—");
    expect(timeLabel("not-a-date", NOW)).toBe("—");
    expect(timeLabel(null, NOW)).toBe("—");
  });
});

describe("sessionStatusMeta", () => {
  it("执行中/完成/失败/未知 各自有中文与色调", () => {
    expect(sessionStatusMeta("executing")).toEqual({ label: "执行中", tone: "run" });
    expect(sessionStatusMeta("done")).toEqual({ label: "已完成", tone: "ok" });
    expect(sessionStatusMeta("failed")).toEqual({ label: "失败", tone: "bad" });
    expect(sessionStatusMeta("confirmed")).toEqual({ label: "待开工", tone: "warn" });
    expect(sessionStatusMeta("weird")).toEqual({ label: "weird", tone: "idle" });
    expect(sessionStatusMeta(null)).toEqual({ label: "未知", tone: "idle" });
  });
});

describe("sortByUpdatedDesc", () => {
  it("按更新时间倒序，缺失时间排最后，且不改动入参", () => {
    const input = [
      { id: "old", updated_at: iso(3 * 86_400_000) },
      { id: "new", updated_at: iso(60_000) },
      { id: "none", updated_at: null },
      { id: "mid", updated_at: iso(3_600_000) },
    ];
    const snapshot = input.map((x) => x.id);
    expect(sortByUpdatedDesc(input).map((x) => x.id)).toEqual(["new", "mid", "old", "none"]);
    expect(input.map((x) => x.id)).toEqual(snapshot);
  });

  it("同一时间戳保持原顺序（稳定）", () => {
    const t = iso(60_000);
    const out = sortByUpdatedDesc([
      { id: "a", updated_at: t },
      { id: "b", updated_at: t },
    ]);
    expect(out.map((x) => x.id)).toEqual(["a", "b"]);
  });
});

describe("validateDeclineComment", () => {
  it("空值与纯空白被拦下", () => {
    expect(validateDeclineComment("").ok).toBe(false);
    expect(validateDeclineComment("   ").ok).toBe(false);
    expect(validateDeclineComment(null).ok).toBe(false);
  });

  it("1 字与 2000 字通过，超长被拦下，首尾空白被裁掉", () => {
    expect(validateDeclineComment("改")).toEqual({ ok: true, comment: "改" });
    expect(validateDeclineComment("  请补充数据来源  ")).toEqual({
      ok: true,
      comment: "请补充数据来源",
    });
    expect(validateDeclineComment("字".repeat(2000)).ok).toBe(true);
    expect(validateDeclineComment("字".repeat(2001)).ok).toBe(false);
  });
});

describe("progressPct", () => {
  it("0 任务为 0，正常取整，超界夹紧", () => {
    expect(progressPct(0, 0)).toBe(0);
    expect(progressPct(1, 3)).toBe(33);
    expect(progressPct(2, 3)).toBe(67);
    expect(progressPct(5, 3)).toBe(100);
    expect(progressPct(-1, 3)).toBe(0);
  });
});
