import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const notifications = vi.hoisted(() => ({
  fetchUnreadCount: vi.fn(),
  fetchNotifications: vi.fn(),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
  startNotificationPolling: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
const routerPushMock = vi.hoisted(() => vi.fn());
const companyMock = vi.hoisted(() => ({ cid: "co-a" }));

vi.mock("@/lib/api", () => ({
  getActiveCompanyId: () => companyMock.cid,
  describeError: (e: unknown, fallback = "操作失败") =>
    (e as { message?: string })?.message || fallback,
}));
vi.mock("@/lib/notifications", () => notifications);
vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPushMock }) }));

const items = [
  {
    id: "n1", type: "task.completed", title: "第一条通知", body: "任务完成",
    is_read: false, link: null, created_at: "2026-11-24T08:00:00Z",
  },
  {
    id: "n2", type: "approval.pending", title: "第二条通知", body: "等待审批",
    is_read: false, link: null, created_at: "2026-11-24T08:01:00Z",
  },
];

let polling: () => void = () => {};

describe("通知计数基线", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    polling = () => {};
    companyMock.cid = "co-a";
    notifications.fetchNotifications.mockResolvedValue(items);
    notifications.markRead.mockResolvedValue({});
    notifications.markAllRead.mockResolvedValue({});
    notifications.startNotificationPolling.mockImplementation((callback: () => void) => {
      polling = callback;
      return () => {};
    });
  });

  it("单条已读后轮询仍能发现新增未读", async () => {
    notifications.fetchUnreadCount.mockResolvedValueOnce(2).mockResolvedValueOnce(2);
    const { default: NotificationBell } = await import("@/components/NotificationBell");
    render(<NotificationBell />);
    await screen.findByLabelText("通知，2 条未读");
    await userEvent.setup().click(screen.getByLabelText("通知，2 条未读"));
    await userEvent.setup().click(await screen.findByRole("menuitem", { name: /第一条通知/ }));
    await screen.findByLabelText("通知，1 条未读");

    await act(async () => { polling(); });

    await waitFor(() => expect(notifyMock).toHaveBeenCalledWith(
      "info", "📩 1 条新通知（审批动态、任务完成都在这里）",
    ));
  });

  it("全部已读后轮询从零基线发现新增未读", async () => {
    notifications.fetchUnreadCount.mockResolvedValueOnce(2).mockResolvedValueOnce(1);
    const { default: NotificationBell } = await import("@/components/NotificationBell");
    render(<NotificationBell />);
    await screen.findByLabelText("通知，2 条未读");
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("通知，2 条未读"));
    await user.click(await screen.findByRole("button", { name: "全部已读" }));
    await waitFor(() => expect(screen.queryByTestId("notification-badge")).toBeNull());

    await act(async () => { polling(); });

    await waitFor(() => expect(notifyMock).toHaveBeenCalledWith(
      "info", "📩 1 条新通知（审批动态、任务完成都在这里）",
    ));
  });

  it("已读回包跨公司时不改新公司状态或跳转旧链接", async () => {
    let resolveRead!: () => void;
    notifications.markRead.mockReturnValue(new Promise<void>((resolve) => {
      resolveRead = resolve;
    }));
    const companyBNotification = {
      ...items[0], id: "n-b", title: "B 公司通知", is_read: false, link: "/tasks/b",
    };
    notifications.fetchUnreadCount.mockResolvedValueOnce(2).mockResolvedValueOnce(1);
    notifications.fetchNotifications.mockImplementation(async () => (
      companyMock.cid === "co-a" ? items : [companyBNotification]
    ));
    const { default: NotificationBell } = await import("@/components/NotificationBell");
    render(<NotificationBell />);
    const user = userEvent.setup();
    await screen.findByLabelText("通知，2 条未读");
    await user.click(screen.getByLabelText("通知，2 条未读"));
    await user.click(await screen.findByRole("menuitem", { name: /第一条通知/ }));

    companyMock.cid = "co-b";
    act(() => window.dispatchEvent(new CustomEvent("aieos-company-changed")));
    await screen.findByLabelText("通知，1 条未读");
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(screen.getByLabelText("通知，1 条未读"));
    const companyBItem = await screen.findByRole("menuitem", { name: /B 公司通知/ });
    expect(companyBItem.querySelector('[aria-label="未读"]')).not.toBeNull();

    await act(async () => resolveRead());

    expect(screen.getByLabelText("通知，1 条未读")).toBeTruthy();
    expect(companyBItem.querySelector('[aria-label="未读"]')).not.toBeNull();
    expect(routerPushMock).not.toHaveBeenCalled();
  });

  it("公司切换提交后的首帧立即隐藏旧公司未读徽标", async () => {
    let resolveB!: (value: number) => void;
    notifications.fetchUnreadCount.mockResolvedValueOnce(7).mockReturnValueOnce(
      new Promise<number>((resolve) => { resolveB = resolve; }),
    );
    const { default: NotificationBell } = await import("@/components/NotificationBell");
    render(<NotificationBell />);
    await screen.findByLabelText("通知，7 条未读");
    expect(screen.getByTestId("notification-badge").textContent).toBe("7");

    companyMock.cid = "co-b";
    act(() => window.dispatchEvent(new CustomEvent("aieos-company-changed")));

    expect(screen.queryByTestId("notification-badge")).toBeNull();
    expect(screen.getByLabelText("通知")).toBeTruthy();
    await act(async () => resolveB(0));
    expect(screen.queryByTestId("notification-badge")).toBeNull();
  });

  it("全部已读回包跨公司时不清空新公司未读", async () => {
    let resolveAll!: () => void;
    notifications.markAllRead.mockReturnValue(new Promise<void>((resolve) => {
      resolveAll = resolve;
    }));
    notifications.fetchUnreadCount.mockResolvedValueOnce(2).mockResolvedValueOnce(1);
    const { default: NotificationBell } = await import("@/components/NotificationBell");
    render(<NotificationBell />);
    const user = userEvent.setup();
    await screen.findByLabelText("通知，2 条未读");
    await user.click(screen.getByLabelText("通知，2 条未读"));
    await user.click(await screen.findByRole("button", { name: "全部已读" }));

    companyMock.cid = "co-b";
    act(() => window.dispatchEvent(new CustomEvent("aieos-company-changed")));
    await screen.findByLabelText("通知，1 条未读");
    await act(async () => resolveAll());

    expect(screen.getByLabelText("通知，1 条未读")).toBeTruthy();
    expect(screen.queryByTestId("notification-badge")).not.toBeNull();
  });
});
