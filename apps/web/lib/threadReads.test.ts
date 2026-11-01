// threadReads 的纯逻辑与队列行为：合并规则、批量上报、失败回填。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({
  api: { get: vi.fn(), post: vi.fn() },
  getActiveCompanyId: vi.fn(),
}));

import { api, getActiveCompanyId } from "./api";
import {
  THREAD_READS_DEBOUNCE_MS,
  fetchThreadReads,
  flushThreadReads,
  queueThreadRead,
  readsToMap,
  resetThreadReads,
  startThreadReadPolling,
} from "./threadReads";

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const activeCompany = vi.mocked(getActiveCompanyId);

beforeEach(() => {
  vi.useFakeTimers();
  resetThreadReads();
  get.mockReset();
  post.mockReset();
  activeCompany.mockReset();
  activeCompany.mockReturnValue("c1");
});

afterEach(() => {
  resetThreadReads();
  vi.useRealTimers();
});

describe("readsToMap", () => {
  it("过滤非法行，同一 key 取较大值", () => {
    expect(readsToMap([
      { thread_key: "war:a", read_at: 100 },
      { thread_key: " war:a ", read_at: 300 },
      { thread_key: "war:b", read_at: 200 },
      { thread_key: "", read_at: 400 },
      { thread_key: "war:c", read_at: Number.NaN },
      { thread_key: "war:d", read_at: -1 },
    ])).toEqual({ "war:a": 300, "war:b": 200 });
  });

  it("非数组输入返回空映射", () => {
    expect(readsToMap(null)).toEqual({});
    expect(readsToMap(undefined)).toEqual({});
  });
});

describe("上报队列", () => {
  it("防抖窗口内合并成一次请求，同一 key 只增不减", async () => {
    post.mockResolvedValue({});
    queueThreadRead("c1", "war:a", 100);
    queueThreadRead("c1", "war:a", 50);
    queueThreadRead("c1", "war:a", 250);
    queueThreadRead("c1", "war:b", 200);
    expect(post).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(THREAD_READS_DEBOUNCE_MS);
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/thread-reads", {
      company_id: "c1",
      reads: [
        { thread_key: "war:a", read_at: 250 },
        { thread_key: "war:b", read_at: 200 },
      ],
    }, { silent: true });
  });

  it("空公司/空 key/非法时间戳直接忽略", async () => {
    queueThreadRead("", "war:a", 100);
    queueThreadRead("c1", "  ", 100);
    queueThreadRead("c1", "war:a", Number.NaN);
    await vi.advanceTimersByTimeAsync(THREAD_READS_DEBOUNCE_MS);
    expect(post).not.toHaveBeenCalled();
  });

  it("发送失败回填队列，下次 flush 重试同一批", async () => {
    post.mockRejectedValueOnce(new Error("offline"));
    queueThreadRead("c1", "war:a", 100);
    await vi.advanceTimersByTimeAsync(THREAD_READS_DEBOUNCE_MS);
    expect(post).toHaveBeenCalledTimes(1);

    post.mockResolvedValueOnce({});
    await flushThreadReads();
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith("/thread-reads", {
      company_id: "c1",
      reads: [{ thread_key: "war:a", read_at: 100 }],
    }, { silent: true });
  });

  it("不同公司分别上报", async () => {
    post.mockResolvedValue({});
    queueThreadRead("c1", "war:a", 100);
    queueThreadRead("c2", "war:b", 200);
    await vi.advanceTimersByTimeAsync(THREAD_READS_DEBOUNCE_MS);
    expect(post).toHaveBeenCalledTimes(2);
    const bodies = post.mock.calls.map((call) => call[1] as { company_id: string });
    expect(bodies.map((b) => b.company_id).sort()).toEqual(["c1", "c2"]);
  });
});

describe("拉取与轮询", () => {
  it("fetchThreadReads 用激活公司拼查询，返回数组", async () => {
    get.mockResolvedValue({ reads: [{ thread_key: "war:a", read_at: 1 }] });
    await expect(fetchThreadReads()).resolves.toEqual([{ thread_key: "war:a", read_at: 1 }]);
    expect(get).toHaveBeenCalledWith("/thread-reads?company_id=c1", { silent: true });
  });

  it("没有激活公司时不发请求", async () => {
    activeCompany.mockReturnValue(null);
    await expect(fetchThreadReads()).resolves.toEqual([]);
    expect(get).not.toHaveBeenCalled();
  });

  it("无 window（SSR）时轮询返回空停止函数", () => {
    const stop = startThreadReadPolling(() => {});
    expect(typeof stop).toBe("function");
    expect(() => stop()).not.toThrow();
    expect(get).not.toHaveBeenCalled();
  });
});
