import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* 验收链路：APPROVED 的成果 → 验收通过 / 退回修改。
 * 只验证交互契约：点了哪个按钮、发了什么请求、用户看到什么结果。 */

const apiMock = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
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

vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("@/components/AttachmentPicker", () => ({ default: () => null }));

async function mountPage() {
  const { default: ArtifactsPage } = await import("@/app/(main)/artifacts/page");
  render(<ArtifactsPage />);
  await screen.findByText("季度经营复盘");
}

describe("成果验收链路", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
          id: "a1", name: "季度经营复盘", status: "APPROVED",
          versions: [{ version: 2, status: "APPROVED", content_preview: "# 复盘" }],
          accepted_at: null, comment_count: 0,
        };
      }
      if (url === "/artifacts/a1/comments") return [];
      throw new Error(`unexpected GET ${url}`);
    });
    apiMock.post.mockResolvedValue({});
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
