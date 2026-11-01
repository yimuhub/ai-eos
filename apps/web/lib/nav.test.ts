// 导航收敛契约：侧边栏 ≤12 个可见入口，但**不丢任何页面**。
//
// 这些断言故意锁住两件事：
//   1. 视觉收敛（≤5 组、≤12 项，含「全部页面」入口）；
//   2. 可达性不倒退（原 39 个侧边栏页 + 2 个内部页全部仍在 ALL_PAGES，⌘K 可搜）。
import { describe, expect, it } from "vitest";
import {
  ALL_PAGES,
  ALL_PAGES_ENTRY,
  HIDDEN_ITEMS,
  NAV_GROUPS,
  SIDEBAR_GROUPS,
  SIDEBAR_ITEM_COUNT,
  findNavByPath,
  isNavActive,
} from "./nav";

/** 收敛前侧边栏的全部路由：任何一个消失都意味着"页面被删了"，测试必须失败。 */
const ORIGINAL_HREFS = [
  "/workspace", "/dashboard", "/command", "/projects", "/warroom", "/tasks", "/autonomous", "/companies",
  "/agents", "/organization", "/factory", "/experts", "/agent-offboarding", "/models",
  "/meetings", "/workflow-builder", "/schedules", "/automation", "/device-console",
  "/knowledge", "/skill-center", "/artifacts", "/delivery", "/doc-styles", "/memory", "/templates", "/assets",
  "/growth-center", "/reflection", "/training", "/performance", "/governance", "/external-agents",
  "/intervention", "/skill-growth", "/token-stats", "/lan-share", "/diagnostics", "/logs",
];

describe("导航收敛", () => {
  it("侧边栏最多 5 组、最多 12 个可见入口（含「全部页面」）", () => {
    expect(SIDEBAR_GROUPS.length).toBeLessThanOrEqual(5);
    expect(SIDEBAR_ITEM_COUNT).toBeGreaterThan(0);
    expect(SIDEBAR_ITEM_COUNT + 1).toBeLessThanOrEqual(12);
    expect(ALL_PAGES_ENTRY.label).toBe("全部页面");
  });

  it("可见分组不为空，且每项都有标签、图标与说明", () => {
    for (const group of SIDEBAR_GROUPS) {
      expect(group.items.length).toBeGreaterThan(0);
      for (const item of group.items) {
        expect(item.hidden).not.toBe(true);
        expect(item.label.length).toBeGreaterThan(0);
        expect(item.icon.length).toBeGreaterThan(0);
        expect(item.desc.length).toBeGreaterThan(0);
      }
    }
  });

  it("隐藏不是删除：原 39 个路由仍全部在 ALL_PAGES", () => {
    const hrefs = ALL_PAGES.map((page) => page.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    for (const href of ORIGINAL_HREFS) {
      expect(hrefs).toContain(href);
    }
  });

  it("内部页面仍可被 ⌘K 搜到", () => {
    const hrefs = ALL_PAGES.map((page) => page.href);
    for (const item of HIDDEN_ITEMS) {
      expect(hrefs).toContain(item.href);
    }
  });

  it("隐藏项保留原 label / desc / keywords（只多一个 hidden 标记）", () => {
    const dashboard = ALL_PAGES.find((page) => page.href === "/dashboard");
    expect(dashboard?.label).toBe("指挥中心");
    expect(dashboard?.desc).toContain("全局总览");
    expect(dashboard?.keywords).toContain("zhihuizhongxin");

    const models = ALL_PAGES.find((page) => page.href === "/models");
    expect(models?.label).toBe("模型配置");
    expect(models?.keywords).toContain("moxing");
  });

  it("分组总数不超过 5，ALL_PAGES 覆盖全部分组项与内部页", () => {
    expect(NAV_GROUPS.length).toBeLessThanOrEqual(5);
    const fromGroups = NAV_GROUPS.reduce((sum, group) => sum + group.items.length, 0);
    expect(ALL_PAGES.length).toBe(fromGroups + HIDDEN_ITEMS.length);
  });

  it("路径回退与高亮规则保持不变", () => {
    expect(findNavByPath("/agents/abc")?.href).toBe("/agents");
    expect(isNavActive("/tasks/123", "/tasks")).toBe(true);
    expect(isNavActive("/task", "/tasks")).toBe(false);
  });
});
