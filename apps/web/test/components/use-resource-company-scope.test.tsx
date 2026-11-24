import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(async () => []) },
  getActiveCompanyId: () => null,
  setActiveCompanyId: vi.fn(),
  getToken: () => null,
}));

import { invalidate, setCached, useResource } from "@/lib/useResource";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

describe("useResource 公司 key 切换", () => {
  beforeEach(() => invalidate());

  it("新公司无缓存时首帧不显示旧公司数据", async () => {
    setCached("agents:co-a", [{ id: "agent-a" }]);
    const pending = deferred<Array<{ id: string }>>();
    const fetcher = vi.fn(() => pending.promise);
    const { result, rerender } = renderHook(
      ({ key }) => useResource(key, fetcher),
      { initialProps: { key: "agents:co-a" } },
    );

    expect(result.current.data).toEqual([{ id: "agent-a" }]);
    rerender({ key: "agents:co-b" });

    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);
    await act(async () => pending.resolve([{ id: "agent-b" }]));
    await waitFor(() => expect(result.current.data).toEqual([{ id: "agent-b" }]));
  });

  it("新公司有缓存时立即使用新公司数据", () => {
    setCached("agents:co-a", [{ id: "agent-a" }]);
    setCached("agents:co-b", [{ id: "agent-b" }]);
    const fetcher = vi.fn(async () => []);
    const { result, rerender } = renderHook(
      ({ key }) => useResource(key, fetcher),
      { initialProps: { key: "agents:co-a" } },
    );

    rerender({ key: "agents:co-b" });

    expect(result.current.data).toEqual([{ id: "agent-b" }]);
    expect(result.current.loading).toBe(false);
  });

  it("旧公司请求晚于切换到达时不能覆盖新公司数据", async () => {
    const requests = {
      "agents:co-a": deferred<Array<{ id: string }>>(),
      "agents:co-b": deferred<Array<{ id: string }>>(),
    };
    let activeKey = "agents:co-a";
    const fetcher = vi.fn(() => requests[activeKey as keyof typeof requests].promise);
    const { result, rerender } = renderHook(
      ({ key }) => useResource(key, fetcher),
      { initialProps: { key: "agents:co-a" } },
    );
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    activeKey = "agents:co-b";
    rerender({ key: "agents:co-b" });
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));

    await act(async () => requests["agents:co-a"].resolve([{ id: "agent-a" }]));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();

    await act(async () => requests["agents:co-b"].resolve([{ id: "agent-b" }]));
    expect(result.current.data).toEqual([{ id: "agent-b" }]);
  });
});
