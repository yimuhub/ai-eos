import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const notifyMock = vi.hoisted(() => vi.fn());
const companyMock = vi.hoisted(() => ({ cid: "co-a" }));
const refreshMock = vi.hoisted(() => vi.fn(async () => {}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
  describeError: (e: unknown, fallback = "操作失败") =>
    (e as { message?: string })?.message || fallback,
}));
vi.mock("@/components/Toast", () => ({ notify: notifyMock }));
vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => companyMock.cid,
  useResource: (key: string | null) => {
    let data: unknown = null;
    if (key?.startsWith("eval-workflows:")) {
      const id = companyMock.cid === "co-a" ? "wf-a" : "wf-b";
      data = [{ id, name: id === "wf-a" ? "A 源工作流" : "B 源工作流", status: "ACTIVE", version: 1 }];
    } else if (key?.startsWith("eval-models:")) {
      data = [{
        id: companyMock.cid === "co-a" ? "model-a" : "model-b",
        name: companyMock.cid === "co-a" ? "A 对话模型" : "B 对话模型",
        model_id: "test-model",
        company_id: companyMock.cid, model_type: "text", enabled: true,
      }];
    } else if (key?.startsWith("evals:")) {
      data = { total: 0, verified_total: 0, manual_total: 0, unavailable_total: 0, by_tier: {} };
    } else if (key?.startsWith("eval-history:")) {
      data = { items: [], total: 0 };
    }
    return { data, error: null, loading: false, refresh: refreshMock };
  },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

describe("评测公司边界", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    companyMock.cid = "co-a";
  });

  it("已有评测证据时切公司不显示旧证据、旧表单或继续旧轮询", async () => {
    const row = {
      id: "eval-a", company_id: "co-a", case_key: "company-a-case",
      suite_key: "default", model_tier: "configured", workflow_id: "wf-a",
      verdict: "unavailable", run_kind: "workflow", status: "unavailable",
      verification: "unavailable", stage: "unavailable",
      error: "A 公司私有证据", blind_order: null, scores: null,
      hashes: { prompt: null, workflow_snapshot: null, multi_output: null,
        baseline_output: null, content_fingerprint: null },
      source_execution_id: null, source_execution_path: null,
      baseline_task_id: null, judge_model_id: null, judge_model_hash: null,
      judge_prompt_hash: null, rubric_version: null, blind_evidence: null,
      evidence: null, tokens: 0, cost: 0, latency_ms: 0, created_at: null,
    };
    apiMock.post.mockResolvedValue(row);
    apiMock.get.mockResolvedValue(row);
    const { default: EvalsPage } = await import("@/app/(main)/evals/page");
    const result = render(<EvalsPage />);
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText("case key（如 行业周报-01）"), "company-a-case");
    await user.selectOptions(screen.getAllByRole("combobox")[0], "wf-a");
    await user.selectOptions(screen.getAllByRole("combobox")[1], "model-a");
    await user.type(screen.getByPlaceholderText(/评测 prompt/), "A 公司 prompt");
    await user.click(screen.getByRole("button", { name: "执行真实评测" }));
    await screen.findByText("A 公司私有证据");
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith(
      "/evals/eval-a", expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    const readsBeforeSwitch = apiMock.get.mock.calls.length;

    companyMock.cid = "co-b";
    result.rerender(<EvalsPage />);

    expect(screen.queryByText("A 公司私有证据")).toBeNull();
    expect(screen.queryByRole("option", { name: /A 源工作流/ })).toBeNull();
    expect(screen.getByPlaceholderText("case key（如 行业周报-01）")).toHaveProperty("value", "");
    expect(screen.getByPlaceholderText(/评测 prompt/)).toHaveProperty("value", "");
    expect(screen.getAllByRole("combobox")[0]).toHaveProperty("value", "wf-b");
    expect(screen.getAllByRole("combobox")[1]).toHaveProperty("value", "model-b");
    expect(apiMock.get.mock.calls.length).toBe(readsBeforeSwitch);
  });

  it("切换公司后丢弃旧公司晚到的启动响应", async () => {
    const pending = deferred<Record<string, unknown>>();
    apiMock.post.mockReturnValue(pending.promise);
    const { default: EvalsPage } = await import("@/app/(main)/evals/page");
    const result = render(<EvalsPage />);
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText("case key（如 行业周报-01）"), "company-a-case");
    await user.selectOptions(screen.getAllByRole("combobox")[0], "wf-a");
    await user.selectOptions(screen.getAllByRole("combobox")[1], "model-a");
    await user.type(screen.getByPlaceholderText(/评测 prompt/), "生成一份周报");
    await user.click(screen.getByRole("button", { name: "执行真实评测" }));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/evals/run", expect.objectContaining({ company_id: "co-a" }),
    ));

    companyMock.cid = "co-b";
    result.rerender(<EvalsPage />);
    await act(async () => {
      pending.resolve({
        id: "eval-a", company_id: "co-a", case_key: "company-a-case",
        suite_key: "default", model_tier: "configured", workflow_id: "wf-a",
        verdict: "unavailable", run_kind: "workflow", status: "unavailable",
        verification: "unavailable", stage: "unavailable",
        error: "A 公司晚到结果", blind_order: null, scores: null,
        hashes: { prompt: null, workflow_snapshot: null, multi_output: null,
          baseline_output: null, content_fingerprint: null },
        source_execution_id: null, source_execution_path: null,
        baseline_task_id: null, judge_model_id: null, judge_model_hash: null,
        judge_prompt_hash: null, rubric_version: null, blind_evidence: null,
        evidence: null, tokens: 0, cost: 0, latency_ms: 0, created_at: null,
      });
    });

    expect(screen.queryByText("A 公司晚到结果")).toBeNull();
    expect(notifyMock).not.toHaveBeenCalled();
    expect(refreshMock).not.toHaveBeenCalled();
  });
});
