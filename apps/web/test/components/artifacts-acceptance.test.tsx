import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* 验收链路：APPROVED 的成果 → 验收通过 / 退回修改。
 * 只验证交互契约：点了哪个按钮、发了什么请求、用户看到什么结果。 */

const apiMock = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
const companyMock = vi.hoisted(() => ({ cid: "co1" as string | null }));
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
  useActiveCompanyId: () => companyMock.cid,
  useResource: (key: string | null) => ({
    data: key ? resourceMock.data : null,
    error: null,
    loading: false,
    refresh: resourceMock.refresh,
  }),
}));

vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("@/components/AttachmentPicker", () => ({ default: () => null }));

async function mountPage() {
  const { default: ArtifactsPage } = await import("@/app/(main)/artifacts/page");
  const result = render(<ArtifactsPage />);
  await screen.findByText("季度经营复盘");
  return { rerender: () => result.rerender(<ArtifactsPage />) };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const oldDetail = {
  id: "a1", name: "旧公司详情", status: "APPROVED", current_version: 2,
  versions: [{ version: 2, status: "APPROVED", content_preview: "旧公司正文" }],
  accepted_at: null, comment_count: 1,
};

describe("成果验收链路", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/artifacts");
    companyMock.cid = "co1";
    resourceMock.data = {
      cid: "co1",
      rows: [{
        id: "a1", name: "季度经营复盘", type: "document",
        status: "APPROVED", version: 2, accepted_at: null,
      }],
    };
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url === "/artifacts/a1") {
        return {
          id: "a1", name: "季度经营复盘", status: "APPROVED", current_version: 2,
          versions: [{ version: 2, status: "APPROVED", content_preview: "# 复盘" }],
          accepted_at: null, comment_count: 0,
        };
      }
      if (url === "/artifacts/a1/comments") return [];
      throw new Error(`unexpected GET ${url}`);
    });
    apiMock.post.mockResolvedValue({});
  });

  it("首次加载的公司上下文就绪后才消费成果直达链接", async () => {
    window.history.replaceState({}, "", "/artifacts?open=a1&rework=1");
    companyMock.cid = null;
    resourceMock.data = null;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url === "/artifacts/a1") return {
        id: "a1", name: "直达成果", status: "REJECTED", current_version: 1,
        versions: [{ version: 1, status: "REJECTED", content_preview: "旧版" }],
        accepted_at: null, comment_count: 0,
      };
      if (url === "/artifacts/a1/comments") return [];
      throw new Error(`unexpected GET ${url}`);
    });
    const { default: ArtifactsPage } = await import("@/app/(main)/artifacts/page");
    const result = render(<ArtifactsPage />);
    expect(apiMock.get).not.toHaveBeenCalledWith("/artifacts/a1");

    companyMock.cid = "co1";
    resourceMock.data = { cid: "co1", rows: [] };
    result.rerender(<ArtifactsPage />);

    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith("/artifacts/a1"));
    expect(await screen.findByText("直达成果")).toBeTruthy();
    expect(screen.getByPlaceholderText(/返工后的完整内容/)).toBeTruthy();
  });

  it("验收通过：调用 accept 接口并给出明确回执", async () => {
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "✓ 验收通过" }));

    expect(apiMock.post).toHaveBeenCalledWith("/artifacts/a1/accept");
    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith("success", "已验收，交付完成");
    });
    expect(await screen.findByText("✅ 已验收，交付完成")).toBeTruthy();
  });

  it("退回修改：必须写意见，提交时带上意见原文", async () => {
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "↩ 退回修改" }));
    const textarea = await screen.findByPlaceholderText(/退回意见/);
    const confirm = screen.getByRole("button", { name: "确认退回" });

    // 空白意见不允许提交：返工的人必须知道改什么
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    await user.type(textarea, "数据口径不对，按 GMV 重算");
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    await user.click(confirm);

    expect(apiMock.post).toHaveBeenCalledWith(
      "/artifacts/a1/decline",
      { comment: "数据口径不对，按 GMV 重算" },
    );
    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith(
        "success", "已退回修改，等待返工后重新提交",
      );
    });
  });

  it("审核驳回：意见必填，提交时保留原文", async () => {
    resourceMock.data = {
      cid: "co1",
      rows: [{
        id: "a1", name: "季度经营复盘", type: "document",
        status: "IN_REVIEW", version: 2, accepted_at: null,
      }],
    };
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "✕ 驳回" }));
    const textarea = await screen.findByPlaceholderText(/驳回意见/);
    const confirm = screen.getByRole("button", { name: "确认驳回" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    await user.type(textarea, "缺少数据来源，请补充");
    await user.click(confirm);
    expect(apiMock.post).toHaveBeenCalledWith("/artifacts/a1/review", {
      decision: "reject", comment: "缺少数据来源，请补充",
    });
  });

  it("返工：展示驳回意见，追加新版本后再提交审核且保留旧历史", async () => {
    let detail = {
      id: "a1", name: "季度经营复盘", status: "REJECTED", current_version: 1,
      versions: [{
        version: 1, status: "REJECTED", content_preview: "旧版结论",
        review_comment: "口径不一致，请重算",
      }],
      accepted_at: null, comment_count: 1,
    };
    resourceMock.data = {
      cid: "co1",
      rows: [{
        id: "a1", name: "季度经营复盘", type: "document",
        status: "REJECTED", version: 1, accepted_at: null,
      }],
    };
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url === "/artifacts/a1") return detail;
      if (url === "/artifacts/a1/comments") return [];
      throw new Error(`unexpected GET ${url}`);
    });
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/artifacts/a1/versions") {
        detail = {
          ...detail, current_version: 2,
          versions: [
            { version: 2, status: "DRAFT", content_preview: "返工后的结论", review_comment: "" },
            ...detail.versions,
          ],
        };
      }
      if (url === "/artifacts/a1/submit") {
        detail = { ...detail, status: "IN_REVIEW" };
      }
      return {};
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "↩ 开始返工" }));
    expect(await screen.findByText(/驳回版本 v1/)).toBeTruthy();
    expect(screen.getAllByText(/口径不一致，请重算/).length).toBeGreaterThan(0);
    await user.type(screen.getByPlaceholderText("填写返工后的完整内容…"), "返工后的结论");
    await user.type(screen.getByPlaceholderText("返工说明（可选）"), "修正数据口径");
    await user.click(screen.getByRole("button", { name: "保存返工版本" }));
    expect(apiMock.post).toHaveBeenCalledWith("/artifacts/a1/versions", {
      content: "返工后的结论", change_note: "修正数据口径",
    });
    expect(await screen.findByText("旧版结论")).toBeTruthy();
    expect(screen.getByText("审核意见：口径不一致，请重算")).toBeTruthy();
    expect(screen.getByText("返工后的结论")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "提交当前草稿审核" }));
    expect(apiMock.post).toHaveBeenCalledWith("/artifacts/a1/submit");
  });

  it("切公司立即隐藏旧列表和详情，并忽略旧详情晚到响应", async () => {
    const pending = deferred<typeof oldDetail>();
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url === "/artifacts/a1") return pending.promise;
      throw new Error(`unexpected GET ${url}`);
    });
    const user = userEvent.setup();
    const view = await mountPage();
    await user.click(screen.getByRole("button", { name: "季度经营复盘" }));

    companyMock.cid = "co2";
    resourceMock.data = { cid: "co2", rows: [] };
    view.rerender();
    // 同一页面实例响应公司切换。
    await waitFor(() => expect(screen.queryByText("季度经营复盘")).toBeNull());
    await act(async () => pending.resolve(oldDetail));
    expect(screen.queryByText("旧公司详情")).toBeNull();
    expect(screen.queryByText("旧公司正文")).toBeNull();
  });

  it("切公司忽略旧评论响应", async () => {
    const comments = deferred<unknown[]>();
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url === "/artifacts/a1") return oldDetail;
      if (url === "/artifacts/a1/comments") return comments.promise;
      throw new Error(`unexpected GET ${url}`);
    });
    const user = userEvent.setup();
    const view = await mountPage();
    await user.click(screen.getByRole("button", { name: "季度经营复盘" }));
    expect(await screen.findByText("旧公司详情")).toBeTruthy();
    companyMock.cid = "co2";
    resourceMock.data = { cid: "co2", rows: [] };
    view.rerender();
    expect(screen.queryByText("旧公司详情")).toBeNull();
    await act(async () => comments.resolve([
      { id: "c-old", author_type: "user", author_id: "u-old", body: "旧公司评论" },
    ]));
    expect(screen.queryByText("旧公司评论")).toBeNull();
  });

  it("切公司忽略旧交接单响应", async () => {
    const sop = deferred<{ artifact_id: string; title: string; markdown: string }>();
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/auth/me") return { id: "u1" };
      if (url.startsWith("/artifacts/a1/sop?")) return sop.promise;
      throw new Error(`unexpected GET ${url}`);
    });
    const user = userEvent.setup();
    const view = await mountPage();
    await user.click(screen.getByRole("button", { name: "📄 交接单" }));
    companyMock.cid = "co2";
    resourceMock.data = { cid: "co2", rows: [] };
    view.rerender();
    await act(async () => sop.resolve({
      artifact_id: "a1", title: "旧公司交接单", markdown: "旧公司交接内容",
    }));
    expect(screen.queryByText(/旧公司交接单/)).toBeNull();
    expect(screen.queryByText("旧公司交接内容")).toBeNull();
  });

  it("退回失败：错误如实显示，不谎报成功", async () => {
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/artifacts/a1/decline") throw new Error("成果已锁定，无法退回");
      return {};
    });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "↩ 退回修改" }));
    const textarea = await screen.findByPlaceholderText(/退回意见/);
    await user.type(textarea, "封面图分辨率太低");
    await user.click(screen.getByRole("button", { name: "确认退回" }));

    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith("error", "成果已锁定，无法退回");
    });
    expect(await screen.findByText("❌ 成果已锁定，无法退回")).toBeTruthy();
  });
});
