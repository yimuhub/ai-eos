// lib/demoDeliverable.ts 的测试：文件名格式 / 日期补零 / 无路径分隔符 /
// Markdown 含任务标题、正文与演示声明 / 固定 Date 下输出稳定。
import { describe, expect, it } from "vitest";
import {
  DEMO_DELIVERABLE_MARKDOWN,
  DEMO_DISCLAIMER,
  DEMO_TASK_TITLE,
  demoDeliverableFileName,
  demoDeliverableMarkdown,
} from "./demoDeliverable";

describe("demoDeliverableFileName", () => {
  it("按本地日期生成 YYYYMMDD 文件名", () => {
    const name = demoDeliverableFileName(new Date(2026, 10, 1, 9, 30, 0));
    expect(name).toBe("AI-EOS-演示交付物-20261101.md");
    expect(name).toMatch(/^AI-EOS-演示交付物-\d{8}\.md$/);
  });

  it("月份和日期补零", () => {
    expect(demoDeliverableFileName(new Date(2026, 0, 2))).toBe(
      "AI-EOS-演示交付物-20260102.md",
    );
  });

  it("不含路径分隔符等非法字符", () => {
    const name = demoDeliverableFileName(new Date(2026, 11, 31));
    expect(name).not.toMatch(/[\\/:*?"<>|\u0000-\u001f]/);
    expect(name.endsWith(".md")).toBe(true);
  });
});

describe("demoDeliverableMarkdown", () => {
  const fixed = new Date(2026, 10, 1, 8, 5, 3);

  it("包含任务标题、生成时间与演示声明", () => {
    const md = demoDeliverableMarkdown(fixed);
    expect(md).toContain(DEMO_TASK_TITLE);
    expect(md).toContain("2026-11-01 08:05:03");
    expect(md).toContain(DEMO_DISCLAIMER);
  });

  it("包含交付物正文，并以演示声明收尾", () => {
    const md = demoDeliverableMarkdown(fixed);
    expect(md).toContain("# 🎉 新品发布会 · 朋友圈文案");
    expect(md).toContain(DEMO_DELIVERABLE_MARKDOWN);
    expect(md.trimEnd().endsWith(DEMO_DISCLAIMER)).toBe(true);
  });

  it("同一 Date 多次调用输出完全一致", () => {
    expect(demoDeliverableMarkdown(fixed)).toBe(demoDeliverableMarkdown(fixed));
    expect(demoDeliverableFileName(fixed)).toBe(demoDeliverableFileName(fixed));
  });
});
