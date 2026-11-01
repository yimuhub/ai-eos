import { describe, expect, it } from "vitest";
import { filterCommands, nextIndex, scoreCommand, type SearchableCommand } from "./commandSearch";

const cmd = (
  id: string,
  label: string,
  extra: Partial<SearchableCommand> = {},
): SearchableCommand => ({ id, label, ...extra });

describe("scoreCommand", () => {
  it("空查询与空白查询都返回 0", () => {
    expect(scoreCommand("", "任务")).toBe(0);
    expect(scoreCommand("   ", "任务")).toBe(0);
  });

  it("中文子串命中，且命中越完整分数越高", () => {
    const exact = scoreCommand("任务", "任务");
    const prefix = scoreCommand("任务", "任务中心");
    const infix = scoreCommand("任务", "所有任务列表");
    expect(exact).toBeGreaterThan(prefix);
    expect(prefix).toBeGreaterThan(infix);
    expect(infix).toBeGreaterThan(0);
  });

  it("英文不区分大小写", () => {
    expect(scoreCommand("TOKEN", "Token统计")).toBeGreaterThan(0);
    expect(scoreCommand("Token", "token-stats")).toBeGreaterThan(0);
    expect(scoreCommand("token", "TOKEN")).toBe(scoreCommand("TOKEN", "TOKEN"));
  });

  it("无匹配返回 0", () => {
    expect(scoreCommand("不存在的词", "任务中心")).toBe(0);
    expect(scoreCommand("zzzz", "任务中心")).toBe(0);
  });

  it("忽略查询词内部空格：ren wu 能命中 renwu", () => {
    expect(scoreCommand("ren wu", "renwu")).toBeGreaterThan(0);
  });

  it("复用既有拼音别名：全拼与首字母子序列都能命中", () => {
    expect(scoreCommand("renwu", "renwu")).toBeGreaterThan(0);
    expect(scoreCommand("zhzx", "zhihuizhongxin")).toBeGreaterThan(0);
    expect(scoreCommand("gzt", "gongzuotai")).toBeGreaterThan(0);
  });
});

describe("filterCommands", () => {
  it("空查询按原顺序返回并受 limit 限制", () => {
    const list = [cmd("a", "甲"), cmd("b", "乙"), cmd("c", "丙")];
    expect(filterCommands(list, "", 2).map((x) => x.id)).toEqual(["a", "b"]);
    expect(filterCommands(list, "   ").map((x) => x.id)).toEqual(["a", "b", "c"]);
  });

  it("按分数降序排列", () => {
    const list = [
      cmd("desc", "所有任务列表"), // 子串
      cmd("exact", "任务"), // 精确
      cmd("prefix", "任务中心"), // 前缀
    ];
    expect(filterCommands(list, "任务").map((x) => x.id)).toEqual([
      "exact",
      "prefix",
      "desc",
    ]);
  });

  it("同分时保持输入顺序（稳定排序）", () => {
    const list = [cmd("first", "同名"), cmd("second", "同名"), cmd("third", "同名")];
    expect(filterCommands(list, "同名").map((x) => x.id)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("按 id 去重，保留第一次出现的项", () => {
    const list = [
      cmd("dup", "旧标题"),
      cmd("other", "别的"),
      cmd("dup", "新标题"),
    ];
    const out = filterCommands(list, "");
    expect(out.map((x) => x.id)).toEqual(["dup", "other"]);
    expect(out[0].label).toBe("旧标题");
  });

  it("别名命中：renwu 能找到「任务」", () => {
    const list = [
      cmd("tasks", "任务", { keywords: ["task", "renwu", "待办"] }),
      cmd("models", "模型配置", { keywords: ["model", "moxing"] }),
    ];
    expect(filterCommands(list, "renwu").map((x) => x.id)).toEqual(["tasks"]);
  });

  it("拼音首字母命中：zhzx 能找到「指挥中心」", () => {
    const list = [
      cmd("dashboard", "指挥中心", { keywords: ["dashboard", "zhihuizhongxin", "总览"] }),
      cmd("tasks", "任务", { keywords: ["renwu"] }),
    ];
    expect(filterCommands(list, "zhzx").map((x) => x.id)).toEqual(["dashboard"]);
  });

  it("不匹配时返回空数组", () => {
    expect(filterCommands([cmd("a", "任务")], "完全不相关")).toEqual([]);
  });

  it("limit 为 0 时返回空数组", () => {
    expect(filterCommands([cmd("a", "任务")], "任务", 0)).toEqual([]);
  });
});

describe("nextIndex", () => {
  it("向下越界回到 0", () => {
    expect(nextIndex(2, 3, 1)).toBe(0);
  });

  it("向上越界回到末尾", () => {
    expect(nextIndex(0, 3, -1)).toBe(2);
  });

  it("空列表始终返回 0", () => {
    expect(nextIndex(0, 0, 1)).toBe(0);
    expect(nextIndex(5, 0, -1)).toBe(0);
  });

  it("越界/负数当前值也能归一化", () => {
    expect(nextIndex(5, 3, 1)).toBe(0);
    expect(nextIndex(-1, 3, 0)).toBe(2);
    expect(nextIndex(Number.NaN, 3, 1)).toBe(1);
  });
});
