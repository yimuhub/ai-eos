import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const appOpener = vi.hoisted(() => ({ current: null as null | ((href: string) => void) }));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(async (url: string) => {
      if (url === "/auth/me") return { display_name: "我" };
      if (url === "/companies") return [{ id: "co1", name: "公司" }];
      if (url.startsWith("/agents?")) return [];
      if (url.startsWith("/war-room/sessions")) return { sessions: [] };
      return [];
    }),
  },
  clearToken: vi.fn(),
  describeError: (e: unknown) => String(e),
  getActiveCompanyId: () => "co1",
  setActiveCompanyId: vi.fn(),
}));
vi.mock("@/lib/ui-mode", () => ({ useUIMode: () => "geek", readUIMode: () => "geek" }));
vi.mock("@/lib/apps", () => ({
  navFor: () => ({ label: "任务", icon: "◻" }),
  openApp: (href: string) => appOpener.current?.(href),
  registerAppOpener: (fn: (href: string) => void) => { appOpener.current = fn; },
  stripEmbed: (href: string) => href,
}));
vi.mock("@/lib/palette", () => ({ openCommandPalette: vi.fn(), pushRecentPage: vi.fn() }));
vi.mock("@/components/Toast", () => ({ notify: vi.fn() }));
vi.mock("@/components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("@/components/NotificationBell", () => ({ default: () => <button aria-label="通知" /> }));
vi.mock("@/components/FirstRunTour", () => ({ default: () => null }));
vi.mock("@/components/workspace/Splitter", () => ({ default: () => null }));
vi.mock("@/components/workspace/GroupEditor", () => ({ default: () => null }));
vi.mock("@/components/workspace/WorkspaceSidebar", () => ({
  default: ({ onCollapse }: { onCollapse: () => void }) => <div><button onClick={onCollapse}>收起左抽屉</button><a href="/tasks">任务</a></div>,
}));
vi.mock("@/components/workspace/WorkspacePanel", () => ({
  default: ({ onCollapse }: { onCollapse: () => void }) => <div>
    <button onClick={onCollapse} aria-label="回到对话">收起右抽屉</button>
    <a href="/tasks">面板任务</a>
    <iframe title="任务页面" />
  </div>,
}));
vi.mock("@/components/workspace/WorkspaceChat", () => ({ default: () => <div>对话</div> }));

describe("工作台小屏抽屉", () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState(null, "", "/workspace");
    appOpener.current = null;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("max-width: 899px"), media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  });

  it("两侧互斥、对话框可圈焦、Esc 先关抽屉且焦点返回入口", async () => {
    const { default: Page } = await import("@/app/(main)/workspace/page");
    render(<Page />);
    const opener = await screen.findByRole("button", { name: "展开左栏" });
    await userEvent.setup().click(opener);
    const left = await screen.findByRole("dialog", { name: "工作台导航" });
    expect(left.getAttribute("aria-modal")).toBe("true");
    expect(document.body.style.overflow).toBe("hidden");
    expect(left.contains(document.activeElement)).toBe(true);

    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", ctrlKey: true, bubbles: true })));
    const right = await screen.findByRole("dialog", { name: "工作台产出" });
    expect(screen.queryByRole("dialog", { name: "工作台导航" })).toBeNull();
    expect(right.contains(document.activeElement)).toBe(true);
    const links = right.querySelectorAll("a");
    links[links.length - 1].focus();
    await userEvent.setup().tab();
    expect(right.contains(document.activeElement)).toBe(true);

    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作台产出" })).toBeNull());
    expect(document.body.style.overflow).toBe("");
    expect(screen.getByRole("button", { name: "展开右栏" })).toBeTruthy();

    const secondOpener = screen.getByRole("button", { name: "展开左栏" });
    secondOpener.focus();
    await userEvent.setup().click(secondOpener);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作台导航" })).toBeNull());
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "展开左栏" }));
  });

  it("iframe 内不劫持 Tab，可用显式控制退出；父页面 Esc 仍关闭", async () => {
    const { default: Page } = await import("@/app/(main)/workspace/page");
    render(<Page />);
    await screen.findByRole("button", { name: "展开左栏" });
    act(() => appOpener.current?.("/tasks"));
    const drawer = await screen.findByRole("dialog", { name: "打开的页面" });
    const frame = screen.getByTitle("任务页面");
    frame.focus();
    expect(document.activeElement).toBe(frame);
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    act(() => document.dispatchEvent(tab));
    expect(tab.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(frame);

    // iframe 文档收不到父页面事件，头栏按钮提供始终可达的退出路径。
    await userEvent.setup().click(screen.getByRole("button", { name: "回到对话" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "打开的页面" })).toBeNull());

    act(() => appOpener.current?.("/tasks"));
    await screen.findByRole("dialog", { name: "打开的页面" });
    screen.getByRole("heading", { name: "开始工作" }).focus();
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "打开的页面" })).toBeNull());
    expect(drawer.isConnected).toBe(false);
  });

  it("从页面入口打开右抽屉会先收起左抽屉", async () => {
    const { default: Page } = await import("@/app/(main)/workspace/page");
    render(<Page />);
    const opener = await screen.findByRole("button", { name: "展开左栏" });
    await userEvent.setup().click(opener);
    expect(screen.getByRole("dialog", { name: "工作台导航" })).toBeTruthy();
    act(() => appOpener.current?.("/tasks"));
    expect(await screen.findByRole("dialog", { name: "打开的页面" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "工作台导航" })).toBeNull();
  });
});
