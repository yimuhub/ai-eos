import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/dashboard", useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ clearToken: vi.fn(async () => {}) }));
vi.mock("@/components/Toast", () => ({ notify: vi.fn() }));
vi.mock("@/components/ModeChooser", () => ({ openModeChooser: vi.fn() }));
vi.mock("@/components/NotificationBell", () => ({ default: () => <button aria-label="通知" /> }));
vi.mock("@/lib/apps", () => ({ registerAppNavigator: vi.fn() }));
vi.mock("@/lib/palette", () => ({
  ALL_PAGES_ENTRY: { label: "全部页面", desc: "浏览", icon: "◻" },
  SIDEBAR_GROUPS: [{ title: "工作", hint: "工作", items: [{ href: "/dashboard", label: "首页", desc: "首页", icon: "⌂" }] }],
  isNavActive: (path: string, href: string) => path === href,
  openAllPages: vi.fn(),
  openCommandPalette: vi.fn(),
  pushRecentPage: vi.fn(),
}));

describe("经典导航小屏抽屉", () => {
  beforeEach(() => { localStorage.clear(); });

  it("有对话框标签、锁滚动、Tab 包含、Esc 关闭并归还焦点", async () => {
    const { default: ClassicLayout } = await import("@/components/ClassicLayout");
    render(<ClassicLayout><p>页面内容</p></ClassicLayout>);
    expect(screen.queryByRole("dialog", { name: "主导航" })).toBeNull();
    const opener = screen.getByRole("button", { name: "打开导航菜单" });
    await userEvent.setup().click(opener);

    const drawer = screen.getByRole("dialog", { name: "主导航" });
    expect(drawer.getAttribute("aria-modal")).toBe("true");
    expect(document.body.style.overflow).toBe("hidden");
    const close = screen.getByRole("button", { name: "关闭导航菜单" });
    expect(document.activeElement).toBe(close);
    close.focus();
    await userEvent.setup().tab({ shift: true });
    expect(drawer.contains(document.activeElement)).toBe(true);
    const last = Array.from(drawer.querySelectorAll<HTMLElement>("button,a")).at(-1)!;
    last.focus();
    await userEvent.setup().tab();
    expect(drawer.contains(document.activeElement)).toBe(true);

    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(screen.queryByRole("dialog", { name: "主导航" })).toBeNull();
    expect(document.body.style.overflow).toBe("");
    expect(document.activeElement).toBe(opener);
  });

  it("搜索/全部页面/模式先关闭导航再打开弹窗，且不劫持弹窗 Esc", async () => {
    const { default: ClassicLayout } = await import("@/components/ClassicLayout");
    const palette = await import("@/lib/palette");
    const mode = await import("@/components/ModeChooser");
    const entries = [
      ["搜索页面或操作", palette.openCommandPalette],
      [/全部页面/, palette.openAllPages],
      [/界面模式/, mode.openModeChooser],
    ] as const;
    for (const [buttonName, open] of entries) {
      let removeDialog = () => {};
      vi.mocked(open).mockImplementationOnce(() => {
        expect(screen.queryByRole("dialog", { name: "主导航" })).toBeNull();
        const dialog = document.createElement("div");
        dialog.setAttribute("role", "dialog");
        const close = document.createElement("button");
        close.textContent = "关闭弹窗";
        dialog.append(close);
        document.body.append(dialog);
        close.focus();
        const onKey = (e: KeyboardEvent) => {
          if (e.key !== "Escape") return;
          e.preventDefault();
          dialog.remove();
        };
        window.addEventListener("keydown", onKey);
        removeDialog = () => {
          window.removeEventListener("keydown", onKey);
          dialog.remove();
        };
      });
      const { unmount } = render(<ClassicLayout><p>页面内容</p></ClassicLayout>);
      const opener = screen.getByRole("button", { name: "打开导航菜单" });
      try {
        await userEvent.setup().click(opener);
        await userEvent.setup().click(screen.getByRole("button", { name: buttonName }));
        expect(screen.getByRole("dialog")).toBeTruthy();
        expect(document.activeElement).not.toBe(opener);
        act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
        expect(screen.queryByRole("dialog")).toBeNull();
        expect(screen.queryByRole("dialog", { name: "主导航" })).toBeNull();
      } finally {
        removeDialog();
        unmount();
      }
    }
  });

  it("点遮罩关闭，触摸入口至少 44px", async () => {
    const { default: ClassicLayout } = await import("@/components/ClassicLayout");
    const { container } = render(<ClassicLayout><p>页面内容</p></ClassicLayout>);
    const opener = screen.getByRole("button", { name: "打开导航菜单" });
    expect(opener.className).toContain("min-h-11");
    await userEvent.setup().click(opener);
    const backdrop = container.querySelector(".bg-black\\/60");
    expect(backdrop).toBeTruthy();
    await userEvent.setup().click(backdrop as HTMLElement);
    expect(screen.queryByRole("dialog", { name: "主导航" })).toBeNull();
  });
});
