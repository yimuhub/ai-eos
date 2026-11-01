// C2 左栏「按项目分组」纯函数测试：
// 有项目 → 归到项目名；无项目 → 未归项目；分组作用于过滤后的列表。
import { describe, expect, it } from "vitest";
import { groupThreadsByProject } from "./workspace";

type T = { key: string; projectId?: string | null };
const projects = [
  { id: "p1", name: "官网改版" },
  { id: "p2", name: "618 大促" },
];

describe("groupThreadsByProject", () => {
  it("按项目分组，组顺序跟随项目列表，未归项目排最后", () => {
    const threads: T[] = [
      { key: "war:a", projectId: "p2" },
      { key: "single:b" },
      { key: "group:c", projectId: "p1" },
      { key: "war:d", projectId: "p1" },
    ];
    const groups = groupThreadsByProject(threads, projects);

    expect(groups.map((g) => g.projectId)).toEqual(["p1", "p2", null]);
    expect(groups.map((g) => g.name)).toEqual(["官网改版", "618 大促", "未归项目"]);
    // 组内保持传入顺序（调用方已完成置顶/未读/搜索排序）
    expect(groups[0].threads.map((t) => t.key)).toEqual(["group:c", "war:d"]);
    expect(groups[1].threads.map((t) => t.key)).toEqual(["war:a"]);
    expect(groups[2].threads.map((t) => t.key)).toEqual(["single:b"]);
  });

  it("没有任何项目归属时只出一个「未归项目」组", () => {
    const groups = groupThreadsByProject<T>(
      [{ key: "single:a" }, { key: "meeting:b", projectId: null }],
      projects,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].projectId).toBeNull();
    expect(groups[0].name).toBe("未归项目");
    expect(groups[0].threads.map((t) => t.key)).toEqual(["single:a", "meeting:b"]);
  });

  it("过滤后的列表再分组：空组不出现，未知项目归入未归项目", () => {
    const filtered: T[] = [
      { key: "group:c", projectId: "p1" },
      { key: "war:x", projectId: "gone" },
    ];
    const groups = groupThreadsByProject(filtered, projects);
    expect(groups.map((g) => g.name)).toEqual(["官网改版", "未归项目"]);
    expect(groups[0].threads.map((t) => t.key)).toEqual(["group:c"]);
    expect(groups[1].threads.map((t) => t.key)).toEqual(["war:x"]);
    // 没有 p2 会话 → 不生成空组
    expect(groups.some((g) => g.projectId === "p2")).toBe(false);
  });

  it("空输入返回空数组", () => {
    expect(groupThreadsByProject<T>([], projects)).toEqual([]);
  });
});
