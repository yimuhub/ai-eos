import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* 派活链路：新建任务 → 分发给执行成员（可立即开工）→ 启动待办 → 完成后存为模板。
 * 只验证交互契约：点击后发出什么请求、回执文案是否说真话。 */

const apiMock = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
const pushMock = vi.hoisted(() => vi.fn());
const resourceMock = vi.hoisted(() => ({
  data: null as unknown,
  refresh: vi.fn(async () => {}),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
  describeError: (e: unknown, fallback = "操作失败") =>
    (e as { message?: string })?.message || fallback,
}));

vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => "co1",
  useResource: (key: string | null) => ({
    data: key ? resourceMock.data : null,
    error: null,
    loading: false,
    refresh: resourceMock.refresh,
  }),
}));

vi.mock("@/lib/shortcuts", () => ({
  SHORTCUT_EVENTS: {
    newTask: "newTask", focusSearch: "focusSearch", move: "move",
    toggleSelect: "toggleSelect", openItem: "openItem",
  },
  onShortcut: () => () => {},
}));

vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("@/components/AttachmentPicker", () => ({ default: () => null }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("next/link", async () => {
  const React = await import("react");
  return {
    default: ({ href, children, ...rest }: {
      href: string; children: React.ReactNode;
    }) => React.createElement("a", { href, ...rest }, children),
  };
});

const EMPTY_INBOX = {
  counts: { in_progress: 0, awaiting_acceptance: 0, completed: 0, failed: 0 },
  in_progress: [], awaiting_acceptance: [], completed: [], failed: [],
};

async function mountPage() {
  const { default: TasksPage } = await import("@/app/(main)/tasks/page");
  render(<TasksPage />);
  await screen.findByRole("button", { name: "＋ 新建任务" });
}

describe("任务派发链路", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resourceMock.data = {
      tasks: [
        {
          id: "t1", title: "整理本周销售线索", status: "TODO", priority: "HIGH",
          assigned_agent_id: "ag1", progress: 0, current_stage: null,
        },
        {
          id: "t2", title: "上月投放复盘", status: "COMPLETED", priority: "NORMAL",
          assigned_agent_id: "ag1", progress: 1, current_stage: "已完成",
        },
      ],
      agents: [{ id: "ag1", name: "小甲" }],
      deptGroups: [{
        dept_id: "d1", dept_name: "市场部",
        agents: [{ id: "ag1", name: "小甲" }],
      }],
    };
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/tasks/inbox?company_id=co1") return EMPTY_INBOX;
      throw new Error(`unexpected GET ${url}`);
    });
    apiMock.post.mockResolvedValue({});
  });

  it("新建任务：选中成员后按 auto_start 分发，并如实报出开始了几个", async () => {
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/task-dispatch/dispatch") {
        return { created_count: 1, started_count: 1, queued_count: 0 };
      }
      return {};
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "＋ 新建任务" }));
    await user.type(await screen.findByPlaceholderText("任务标题 *"), "整理本周销售线索");
    await user.click(screen.getByRole("button", { name: /全员执行/ }));
    await user.click(screen.getByRole("button", { name: /创建并分发给 1 名成员/ }));

    expect(apiMock.post).toHaveBeenCalledWith(
      "/task-dispatch/dispatch",
      expect.objectContaining({
        company_id: "co1",
        title: "整理本周销售线索",
        target_type: "multi_agent_list",
        agent_ids: ["ag1"],
        auto_start: true,
      }),
    );
    expect(await screen.findByText(
      "✅ 已创建 1 个任务，其中 1 个已开始执行，0 个排队中",
    )).toBeTruthy();
  });

  it("启动待办：调用 start 接口，成功才显示已启动", async () => {
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "全部任务" }));
    await user.click(screen.getByRole("button", { name: "▶ 启动" }));

    expect(apiMock.post).toHaveBeenCalledWith("/tasks/t1/start");
    expect(await screen.findByText("✅ 已启动")).toBeTruthy();
    expect(resourceMock.refresh).toHaveBeenCalled();
  });

  it("启动失败：显示失败原因，不谎报已启动", async () => {
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/tasks/t1/start") throw new Error("没有可用的模型，任务未启动");
      return {};
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "全部任务" }));
    await user.click(screen.getByRole("button", { name: "▶ 启动" }));

    await waitFor(() => {
      expect(screen.getByText("❌ 没有可用的模型，任务未启动")).toBeTruthy();
    });
    expect(screen.queryByText("✅ 已启动")).toBeNull();
  });

  it("完成态：把成功任务存为模板，提示里带模板名", async () => {
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/tasks/t2/save-as-template") return { id: "tpl1", name: "投放复盘模板" };
      return {};
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "全部任务" }));
    await user.click(screen.getByRole("button", { name: "📋 存为模板" }));

    expect(apiMock.post).toHaveBeenCalledWith("/tasks/t2/save-as-template");
    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith(
        "success", "已存为模板「投放复盘模板」，可在自动化页复用",
      );
    });
  });
});
