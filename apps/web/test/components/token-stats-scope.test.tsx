import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }));
const fetchers = vi.hoisted(() => new Map<string, () => Promise<unknown>>());

vi.mock("@/lib/api", () => ({
  api: apiMock,
  describeError: (e: unknown) => String(e),
  downloadURL: vi.fn(),
  fetchBlobURL: vi.fn(),
  fetchText: vi.fn(),
}));

vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => "co-active",
  useResource: (key: string | null, fetcher: () => Promise<unknown>) => {
    if (key) fetchers.set(key, fetcher);
    return { data: null, error: null, loading: false, refresh: vi.fn() };
  },
}));

vi.mock("@/components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

describe("Token 统计公司边界", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchers.clear();
    apiMock.get.mockImplementation(async (url: string) => {
      if (url.startsWith("/tokens/stats/by-company")) {
        return [
          { key: "co-active", name: "当前公司", total_tokens: 10 },
          { key: "co-other", name: "其它公司", total_tokens: 999 },
        ];
      }
      if (url.startsWith("/tokens/stats/")) return [];
      return [];
    });
  });

  it("summary、公司维度与既有公司维度都携带激活公司，且公司表不混入其它公司", async () => {
    const { default: Page } = await import("@/app/(main)/token-stats/page");
    render(<Page />);
    expect(await screen.findByRole("heading", { name: /Token 统计与计费/ })).toBeTruthy();

    const bundle = await fetchers.get("tokens:stats:co-active:30")?.() as {
      byCompany: { key: string }[];
    };
    const urls = apiMock.get.mock.calls.map(([url]) => String(url));
    expect(urls).toContain("/tokens/stats/summary?company_id=co-active&days=30");
    expect(urls).toContain("/tokens/stats/by-company?company_id=co-active&days=30");
    for (const dimension of ["by-agent", "by-department", "by-project", "by-task", "by-scenario"]) {
      expect(urls.some((url) => url.includes(`/tokens/stats/${dimension}?company_id=co-active&days=30`))).toBe(true);
    }
    expect(bundle.byCompany.map((row) => row.key)).toEqual(["co-active"]);
  });

  it("计费标准仍走独立账号级 key，不附加公司过滤", async () => {
    const { default: Page } = await import("@/app/(main)/token-stats/page");
    render(<Page />);
    expect(fetchers.has("tokens:stats:co-active:30")).toBe(true);
    expect(fetchers.has("tokens:pricing")).toBe(false);
    await userEvent.setup().click(screen.getByRole("button", { name: /计费设置/ }));
    expect(fetchers.has("tokens:pricing")).toBe(true);
    expect(screen.getByText(/计费标准为独立的账号级配置/)).toBeTruthy();
  });
});
