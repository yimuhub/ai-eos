import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* 套餐与账单：升级必须走「提交订单 → 平台确认收款 → 启用」。
 * 只验证交互契约：点了哪个按钮、发了什么请求、用户看到什么结果。 */

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const store = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));

vi.mock("@/lib/api", () => ({
  api: apiMock,
  describeError: (e: unknown, fallback = "操作失败") =>
    (e as { message?: string })?.message || fallback,
}));

vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => "co1",
  useResource: (key: string | null) => ({
    data: key ? store.data[key] ?? null : null,
    error: null,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock("@/components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const SUB = {
  company_id: "co1",
  plan_code: "trial",
  plan_label: "试用",
  status: "TRIALING",
  effective_status: "TRIALING",
  trial_ends_at: "2026-11-20T00:00:00Z",
  period: { start: null, end: null },
  limits: { seats: 60, monthly_tokens: 1_000_000 },
  seats_limit: 60,
  monthly_token_quota: 1_000_000,
  usage: { seats_used: 3, tokens_used_this_month: 250_000 },
};

const PLANS = [
  { plan_code: "pro", label: "专业版", seats_limit: 200, monthly_token_quota: 20_000_000 },
  { plan_code: "enterprise", label: "企业版", seats_limit: null, monthly_token_quota: null },
];

async function mountPage() {
  const { default: BillingPage } = await import("@/app/(main)/billing/page");
  render(<BillingPage />);
  await screen.findByText(/套餐与账单/);
}

describe("套餐升级申请流", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.data = {
      "billing:sub:co1": SUB,
      "billing:plans": PLANS,
      "billing:orders:co1": [],
      "billing:me": { id: "u1", is_superadmin: false },
      "billing:pending": [],
    };
    apiMock.post.mockResolvedValue({});
  });

  it("试用公司点「申请升级」提交订单，并提示等待平台确认", async () => {
    const user = userEvent.setup();
    await mountPage();

    await screen.findByText("已用 3 / 60 席位");
    await user.click(screen.getByRole("button", { name: "申请升级到专业版" }));

    expect(apiMock.post).toHaveBeenCalledWith("/subscription/orders", {
      company_id: "co1",
      plan_code: "pro",
      note: null,
    });
    expect(await screen.findByText(/已提交「专业版」申请/)).toBeTruthy();
  });

  it("已有待确认订单时按钮变为等待状态，不能重复提交", async () => {
    store.data["billing:orders:co1"] = [{
      id: "o1", company_id: "co1", plan_code: "pro", plan_label: "专业版",
      status: "PENDING", confirmed_by: null, confirmed_at: null,
      applied_at: null, note: null, created_at: "2026-11-09T00:00:00Z",
    }];
    await mountPage();

    const btn = await screen.findByRole("button", { name: "等待平台确认收款" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("已收款订单点「立即启用」按 order_id 落套餐", async () => {
    const user = userEvent.setup();
    store.data["billing:orders:co1"] = [{
      id: "o2", company_id: "co1", plan_code: "pro", plan_label: "专业版",
      status: "PAID", confirmed_by: "u9", confirmed_at: "2026-11-09T01:00:00Z",
      applied_at: null, note: null, created_at: "2026-11-09T00:00:00Z",
    }];
    await mountPage();

    await user.click(await screen.findByRole("button", { name: "立即启用" }));

    expect(apiMock.post).toHaveBeenCalledWith("/subscription/plan", {
      company_id: "co1",
      plan_code: "pro",
      order_id: "o2",
    });
    expect(await screen.findByText("「专业版」已生效")).toBeTruthy();
  });

  it("超管能确认收款；非超管完全看不到平台区块", async () => {
    // 非超管：平台待确认区不渲染
    await mountPage();
    expect(screen.queryByText("平台待确认收款（超管可见）")).toBeNull();

    // 超管：能看到别家公司并盖章
    store.data["billing:me"] = { id: "u1", is_superadmin: true };
    store.data["billing:pending"] = [{
      id: "o3", company_id: "co2", company_name: "A 公司", plan_code: "pro",
      plan_label: "专业版", status: "PENDING", confirmed_by: null,
      confirmed_at: null, applied_at: null, note: "回单号 001",
      created_at: "2026-11-09T00:00:00Z",
    }];
    const user = userEvent.setup();
    const { default: BillingPage } = await import("@/app/(main)/billing/page");
    render(<BillingPage />);

    await user.click(await screen.findByRole("button", { name: "确认收款" }));
    expect(apiMock.post).toHaveBeenCalledWith("/subscription/orders/o3/confirm");
    await waitFor(() => {
      expect(screen.getByText(/已确认「A 公司」的「专业版」收款/)).toBeTruthy();
    });
  });
});
