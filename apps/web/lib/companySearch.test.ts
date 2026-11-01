import { describe, expect, it } from "vitest";
import {
  COMPANY_SEARCH_TYPES,
  MIN_COMPANY_QUERY_LENGTH,
  SNIPPET_MAX,
  buildCompanySearchPath,
  normalizeCompanySearch,
  truncateSnippet,
} from "./companySearch";

describe("truncateSnippet", () => {
  it("短文本原样返回，并折叠换行与多余空白", () => {
    expect(truncateSnippet("  量子\n菠萝   计划 ")).toBe("量子 菠萝 计划");
  });

  it("超过 80 字截断，省略号计入总长度", () => {
    const text = "量".repeat(200);
    const out = truncateSnippet(text);
    expect(Array.from(out)).toHaveLength(SNIPPET_MAX);
    expect(out.endsWith("…")).toBe(true);
  });

  it("按 code point 计数，emoji 不会被切成半个", () => {
    const text = "🍍".repeat(100);
    const out = truncateSnippet(text, 10);
    expect(Array.from(out)).toHaveLength(10);
    expect(out).toBe("🍍".repeat(9) + "…");
  });

  it("max <= 0 返回空串", () => {
    expect(truncateSnippet("内容", 0)).toBe("");
  });
});

describe("normalizeCompanySearch", () => {
  const raw = {
    query: "量子",
    groups: [
      {
        type: "task",
        label: "任务",
        items: [
          { id: "t1", title: "任务一", snippet: "命中内容", href: "/tasks" },
          { id: "t1", title: "重复项", snippet: "应被去重", href: "/tasks" },
          { id: "", title: "坏数据", snippet: "", href: "/tasks" },
        ],
      },
      { type: "memory", label: "", items: [] },
      {
        type: "asset",
        items: [
          { id: "a1", title: "素材", snippet: "x".repeat(200), href: "/assets" },
          { id: "a2", title: "缺 href" },
        ],
      },
    ],
  };

  it("保持后端分组顺序", () => {
    expect(normalizeCompanySearch(raw).map((g) => g.type)).toEqual([
      "task",
      "asset",
    ]);
  });

  it("按 type + id 去重，保留第一次出现", () => {
    const groups = normalizeCompanySearch(raw);
    expect(groups[0].items).toHaveLength(1);
    expect(groups[0].items[0].title).toBe("任务一");
  });

  it("丢弃空分组与缺 id/href 的脏数据，label 缺失时回退类型名", () => {
    const groups = normalizeCompanySearch(raw);
    expect(groups.map((g) => g.type)).not.toContain("memory");
    expect(groups[1].items.map((item) => item.id)).toEqual(["a1"]);
    expect(groups[1].label).toBe("资产");
  });

  it("组内 snippet 截到 80 字", () => {
    const groups = normalizeCompanySearch(raw);
    const snippet = groups[1].items[0].snippet;
    expect(Array.from(snippet)).toHaveLength(SNIPPET_MAX);
  });

  it("非对象 / groups 缺失时返回空数组", () => {
    expect(normalizeCompanySearch(null)).toEqual([]);
    expect(normalizeCompanySearch("oops")).toEqual([]);
    expect(normalizeCompanySearch({ query: "x" })).toEqual([]);
  });
});

describe("buildCompanySearchPath", () => {
  it("省略 types 时不带 types 参数，中文与空格正确编码", () => {
    const path = buildCompanySearchPath("co 1", "量子 菠萝");
    expect(path.startsWith("/search?")).toBe(true);
    expect(path).toContain("company_id=co+1");
    expect(path).toContain("q=%E9%87%8F%E5%AD%90+%E8%8F%A0%E8%90%9D");
    expect(path).not.toContain("types=");
  });

  it("带上 types 并保持顺序", () => {
    const path = buildCompanySearchPath("c1", "abc", ["task", "asset"]);
    expect(path).toContain("types=task%2Casset");
  });

  it("类型清单与后端契约一致", () => {
    expect([...COMPANY_SEARCH_TYPES]).toEqual([
      "task",
      "memory",
      "knowledge",
      "asset",
    ]);
    expect(MIN_COMPANY_QUERY_LENGTH).toBe(2);
  });
});
