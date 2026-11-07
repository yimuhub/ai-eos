import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* 模型连通测试：点「测试」→ 后端返回成功/失败都要如实反馈给用户。
 * 这是老板判断"密钥到底通不通"的唯一入口，不能只看有没有抛异常。 */

const apiMock = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
const refreshMock = vi.hoisted(() => vi.fn(async () => {}));

const provider = {
  id: "p1", name: "公司网关", provider_type: "openai_compat",
  base_url: "https://gw.example.com/v1", api_key_env: "OPENAI_COMPAT_API_KEY",
  api_key_present: true, enabled: true, model_count: 1, enabled_model_count: 1,
  last_check_ok: true, last_check_at: "2026-11-07T02:00:00Z", last_check_error: null,
};

const model = {
  id: "m1", name: "主力对话模型", provider_type: "openai_compat",
  base_url: null, model_id: "gw-chat-large", provider_id: "p1",
  provider_name: "公司网关", model_type: "text", multimodal: false,
  capabilities: ["chat"], enabled: true, config_source: "auto",
  config: { temperature: 0.3, max_tokens: 2048 },
};

vi.mock("@/lib/api", () => ({
  api: apiMock,
  getActiveCompanyId: () => "co1",
  describeError: (e: unknown, fallback = "操作失败") =>
    (e as { message?: string })?.message || fallback,
}));

vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => "co1",
  invalidate: vi.fn(),
  useResource: (key: string | null) => {
    let data: unknown = null;
    if (key?.startsWith("models-diag:")) {
      data = {
        model_count: 1, usable_model_ids: ["m1"], ready: true,
        providers: [], hint: "密钥已就位",
      };
    } else if (key?.startsWith("models:")) {
      data = { list: [model], agents: [], providers: [provider], usage: { m1: 1 } };
    } else if (key?.startsWith("convert-cfg:")) {
      data = { convert_model_id: "" };
    }
    return { data, error: null, loading: false, refresh: refreshMock };
  },
}));

vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("@/components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

async function mountPage() {
  const { default: ModelsPage } = await import("@/app/(main)/models/page");
  render(<ModelsPage />);
  await screen.findByText("主力对话模型");
}

describe("模型连通测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("成功：提示里带上模型的真实回复", async () => {
    apiMock.post.mockResolvedValue({ status: "ok", reply: "pong" });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "测试" }));

    expect(apiMock.post).toHaveBeenCalledWith("/models/m1/test");
    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith("success", "连通正常，模型回复：pong");
    });
  });

  it("失败：上游错误原文要给用户看到", async () => {
    apiMock.post.mockResolvedValue({ status: "error", error: "401 Invalid API key" });
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "测试" }));

    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith("error", "401 Invalid API key");
    });
    expect(notifyMock).not.toHaveBeenCalledWith("success", expect.anything());
  });

  it("请求本身失败：网络错误也要可见", async () => {
    apiMock.post.mockRejectedValue(new Error("请求超时，请检查网络"));
    const user = userEvent.setup();
    await mountPage();

    await user.click(screen.getByRole("button", { name: "测试" }));

    await waitFor(() => {
      expect(notifyMock).toHaveBeenCalledWith("error", "请求超时，请检查网络");
    });
  });
});
