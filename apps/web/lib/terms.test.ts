// lib/terms.ts 冻结术语测试（D6）
//
// 锁三件事：
//   1. key 唯一（Object 字面量本身会去重，这里防的是未来改成数组 / 追加构造）；
//   2. 值非空且首尾无空白——带空格的「AI 员工 」在界面上看不出来，却会破坏检索与比对；
//   3. 未知 key 返回 undefined，不抛错、不返回原文（调用方自行回退）；
//   4. docs/术语表.md 的推荐词与 TERMS 完全一致，避免文档先行、代码漂移。
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { TERMS, canonicalTerm } from "./terms";

describe("TERMS", () => {
  it("至少 10 个规范词，且覆盖 D6 要求的核心术语", () => {
    const values = Object.values(TERMS);
    expect(values.length).toBeGreaterThanOrEqual(10);
    for (const required of [
      "AI 员工",
      "任务",
      "交付物",
      "公司",
      "部门",
      "会话",
      "工作台",
      "指挥中心",
      "作战室",
      "审批",
      "席位",
      "模型",
    ]) {
      expect(values).toContain(required);
    }
  });

  it("key 唯一", () => {
    const keys = Object.keys(TERMS);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("值非空且无首尾空白", () => {
    for (const [key, value] of Object.entries(TERMS)) {
      expect(typeof value, key).toBe("string");
      expect(value.trim(), key).not.toBe("");
      expect(value, key).toBe(value.trim());
    }
  });

  it("冻结，防止运行时被页面改写", () => {
    expect(Object.isFrozen(TERMS)).toBe(true);
  });
});

describe("canonicalTerm", () => {
  it("已知 key 返回规范词", () => {
    expect(canonicalTerm("aiEmployee")).toBe("AI 员工");
    expect(canonicalTerm("warRoom")).toBe("作战室");
    expect(canonicalTerm("deliverable")).toBe("交付物");
  });

  it("未知 key 返回 undefined（不抛错、不原样返回）", () => {
    expect(canonicalTerm("agent")).toBeUndefined();
    expect(canonicalTerm("")).toBeUndefined();
    expect(canonicalTerm("toString")).toBeUndefined();
  });
});

describe("docs/术语表.md 与代码一致", () => {
  it("每个规范词都出现在术语表的推荐词列", () => {
    // 测试文件位于 apps/web/lib，仓库根的 docs 在其上三级
    const doc = readFileSync(new URL("../../../docs/术语表.md", import.meta.url), "utf8");
    for (const value of Object.values(TERMS)) {
      expect(doc, `术语表缺少「${value}」`).toContain(`| ${value} |`);
    }
  });
});
