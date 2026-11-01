import { describe, expect, it } from "vitest";
import {
  MAX_CITATIONS,
  citationsFromEvent,
  citationsFromMeta,
  normalizeCitations,
  safeExternalUrl,
} from "./citations";

describe("safeExternalUrl", () => {
  it("只放行 http/https 绝对地址", () => {
    expect(safeExternalUrl("https://example.com/a?b=1")).toBe("https://example.com/a?b=1");
    expect(safeExternalUrl("http://example.com")).toBe("http://example.com/");
  });

  it("拒绝 javascript:/data:/相对路径/空值", () => {
    expect(safeExternalUrl("javascript:void(0)")).toBeNull();
    expect(safeExternalUrl("data:text/html,<script>x</script>")).toBeNull();
    expect(safeExternalUrl("/relative/path")).toBeNull();
    expect(safeExternalUrl("   ")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl(42)).toBeNull();
  });
});

describe("normalizeCitations", () => {
  it("保留合法条目并压成单行", () => {
    const out = normalizeCitations([
      { title: "  竞品 A\n官网 ", url: "https://a.example.com/p", snippet: "  降价  20% " },
    ]);
    expect(out).toEqual([
      { title: "竞品 A 官网", url: "https://a.example.com/p", snippet: "降价 20%" },
    ]);
  });

  it("丢弃非对象、缺标题、缺链接与非法协议的条目", () => {
    const out = normalizeCitations([
      null,
      "not-an-object",
      { title: "没链接" },
      { url: "https://x.test/no-title" },
      { title: "坏协议", url: "javascript:void(0)" },
      { title: "好的", url: "https://x.test/ok" },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].url).toBe("https://x.test/ok");
  });

  it("按 url 去重，保留第一条", () => {
    const out = normalizeCitations([
      { title: "标题一", url: "https://x.test/dup" },
      { title: "标题二", url: "https://x.test/dup" },
      { title: "标题三", url: "https://x.test/other" },
    ]);
    expect(out.map((c) => c.title)).toEqual(["标题一", "标题三"]);
  });

  it("最多保留 5 条", () => {
    const raw = Array.from({ length: 9 }, (_, i) => ({
      title: `T${i}`,
      url: `https://x.test/${i}`,
    }));
    expect(normalizeCitations(raw)).toHaveLength(MAX_CITATIONS);
    expect(normalizeCitations(raw)[4].title).toBe("T4");
  });

  it("标题与摘要截断到 200 字", () => {
    const out = normalizeCitations([
      { title: "标".repeat(300), url: "https://x.test/long", snippet: "摘".repeat(300) },
    ]);
    expect(out[0].title).toHaveLength(200);
    expect(out[0].snippet).toHaveLength(200);
  });

  it("兼容 href 与 body/content 字段", () => {
    const out = normalizeCitations([
      { title: "A", href: "https://a.test/1", body: "来自 body" },
      { title: "B", url: "https://b.test/2", content: "来自 content" },
    ]);
    expect(out).toEqual([
      { title: "A", url: "https://a.test/1", snippet: "来自 body" },
      { title: "B", url: "https://b.test/2", snippet: "来自 content" },
    ]);
  });

  it("非数组输入返回空列表", () => {
    expect(normalizeCitations(null)).toEqual([]);
    expect(normalizeCitations("https://x.test")).toEqual([]);
    expect(normalizeCitations({ results: [] })).toEqual([]);
  });
});

describe("历史 meta / 流式事件的兼容读取", () => {
  const payload = { citations: [{ title: "来源", url: "https://x.test/s" }] };

  it("meta.citations 与 event.citations 走同一条解析路径", () => {
    expect(citationsFromMeta(payload)).toEqual([
      { title: "来源", url: "https://x.test/s" },
    ]);
    expect(citationsFromEvent(payload)).toEqual(citationsFromMeta(payload));
  });

  it("缺字段、旧数据与脏数据都安全降级为空", () => {
    expect(citationsFromMeta({})).toEqual([]);
    expect(citationsFromMeta(null)).toEqual([]);
    expect(citationsFromMeta({ citations: "bad" })).toEqual([]);
    expect(citationsFromEvent({ type: "done" })).toEqual([]);
  });
});
