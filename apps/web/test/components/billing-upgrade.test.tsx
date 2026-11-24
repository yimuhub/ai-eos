import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* 套餐与账单：升级必须走「提交订单 → 平台确认收款 → 启用」。
 * 只验证交互契约：点了哪个按钮、发了什么请求、用户看到什么结果。 */

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const store = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
const assignMock = vi.fn();
const originalLocation = window.location;

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

function stripeOrder(overrides: Record<string, unknown> = {}) {
  return {
    id: "os1", company_id: "co1", plan_code: "pro", plan_label: "专业版",
    status: "PENDING", payment_method: "stripe", amount: 1299, currency: "usd",
    stripe_price_id: "price_pro", stripe_checkout_session_id: null,
    stripe_payment_intent_id: null, checkout_url: null, checkout_expires_at: null,
    receipt_url: null, payment_livemode: false, confirmed_by: null,
    confirmed_at: null, applied_at: null, note: null,
    created_at: "2026-11-09T00:00:00Z",
    ...overrides,
  };
}

async function mountPage() {
  const { default: BillingPage } = await import("@/app/(main)/billing/page");
  render(<BillingPage />);
  await screen.findByText(/套餐与账单/);
}

describe("套餐升级申请流", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("location", { ...originalLocation, assign: assignMock });
    store.data = {
      "billing:sub:co1": SUB,
      "billing:plans": PLANS,
      "billing:orders:co1": [],
      "billing:me": { id: "u1", is_superadmin: false },
      "billing:pending": [],
    };
    apiMock.post.mockResolvedValue({});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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

  it("在线待支付订单 POST checkout 后直跳可信 Stripe，且不 api.get 外链", async () => {
    const checkoutUrl = "https://checkout.stripe.com/c/pay/cs_test_online";
    store.data["billing:orders:co1"] = [stripeOrder()];
    apiMock.post.mockResolvedValue({
      checkout_url: checkoutUrl,
      checkout_expires_at: "2026-11-09T02:00:00Z",
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(await screen.findByRole("button", { name: "去支付" }));

    expect(apiMock.post).toHaveBeenCalledWith("/subscription/orders/os1/checkout");
    expect(assignMock).toHaveBeenCalledWith(checkoutUrl);
    expect(apiMock.get).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "取消" })).toBeTruthy();
  });

  it("已有 Stripe 会话时显示继续支付，在线待支付订单仍可取消", async () => {
    const checkoutUrl = "https://buy.stripe.com/test-session";
    store.data["billing:orders:co1"] = [stripeOrder({
      stripe_checkout_session_id: "cs_test_online",
      checkout_url: checkoutUrl,
    })];
    apiMock.post.mockResolvedValue({
      checkout_url: checkoutUrl,
      checkout_expires_at: "2026-11-09T02:00:00Z",
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(await screen.findByRole("button", { name: "继续支付" }));
    expect(assignMock).toHaveBeenCalledWith(checkoutUrl);
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(apiMock.post).toHaveBeenCalledWith("/subscription/orders/os1/cancel");
    expect(await screen.findByText("订单已取消")).toBeTruthy();
  });

  it("在线 PAID 订单等待自动生效，不显示人工立即启用", async () => {
    store.data["billing:orders:co1"] = [stripeOrder({
      status: "PAID",
      confirmed_at: "2026-11-09T01:00:00Z",
      stripe_payment_intent_id: "pi_test_online",
      payment_livemode: true,
    })];
    await mountPage();

    expect(await screen.findByText("已收款，等待自动生效")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "立即启用" })).toBeNull();
    expect(screen.queryByText(/点「立即启用」/)).toBeNull();
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("展示 Stripe 权威价格与合法回执，未配置在线支付时不伪造价格", async () => {
    store.data["billing:plans"] = [
      { ...PLANS[0], online_available: true, amount: 1299, currency: "usd", stripe_price_id: "price_pro" },
      { ...PLANS[1], online_available: false, amount: 99_999, currency: "usd" },
    ];
    store.data["billing:orders:co1"] = [stripeOrder({
      status: "PAID",
      receipt_url: "https://receipt.stripe.com/receipts/test_online",
      confirmed_at: "2026-11-09T01:00:00Z",
    })];
    await mountPage();

    expect((await screen.findAllByText(/US\$12\.99/)).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/US\$999\.99/)).toBeNull();
    expect(screen.getByRole("link", { name: "查看回执" }).getAttribute("href"))
      .toBe("https://receipt.stripe.com/receipts/test_online");
  });

  it("拒绝非 Stripe 结账地址，不跳转也不请求外链", async () => {
    store.data["billing:orders:co1"] = [stripeOrder()];
    apiMock.post.mockResolvedValue({
      checkout_url: "https://checkout.stripe.com.evil.example/pay",
      checkout_expires_at: "2026-11-09T02:00:00Z",
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(await screen.findByRole("button", { name: "去支付" }));

    expect(await screen.findByText("支付服务返回了不受信任的结账地址")).toBeTruthy();
    expect(assignMock).not.toHaveBeenCalled();
    expect(apiMock.get).not.toHaveBeenCalled();
  });

  it("试用状态不显示取消订阅，ACTIVE 与 PAST_DUE 才显示", async () => {
    await mountPage();
    expect(screen.queryByRole("button", { name: "取消订阅" })).toBeNull();

    for (const subscriptionStatus of ["ACTIVE", "PAST_DUE"]) {
      store.data["billing:sub:co1"] = {
        ...SUB,
        status: subscriptionStatus,
        effective_status: subscriptionStatus,
      };
      const { default: BillingPage } = await import("@/app/(main)/billing/page");
      const view = render(<BillingPage />);
      expect(await screen.findByRole("button", { name: "取消订阅" })).toBeTruthy();
      view.unmount();
    }
  });

  it("超管待确认列表过滤在线订单，只保留线下订单", async () => {
    store.data["billing:me"] = { id: "u1", is_superadmin: true };
    store.data["billing:pending"] = [
      {
        id: "o3", company_id: "co2", company_name: "A 公司", plan_code: "pro",
        plan_label: "专业版", status: "PENDING", payment_method: "offline",
        confirmed_by: null, confirmed_at: null, applied_at: null,
        note: "回单号 001", created_at: "2026-11-09T00:00:00Z",
      },
      stripeOrder({ company_id: "co3", company_name: "在线公司" }),
    ];
    await mountPage();

    expect(await screen.findByText("A 公司")).toBeTruthy();
    expect(screen.queryByText("在线公司")).toBeNull();
    expect(screen.getAllByRole("button", { name: "确认收款" })).toHaveLength(1);
  });
});
