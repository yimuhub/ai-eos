// lib/starterTasks.ts 的测试：条数、字段完整、按 id 查找、未知 id 返回空。
import { describe, expect, it } from "vitest";
import { pickStarterTask, starterTasks } from "./starterTasks";

describe("starterTasks", () => {
  it("提供 3–4 条行业无关的示例任务", () => {
    const tasks = starterTasks();
    expect(tasks.length).toBeGreaterThanOrEqual(3);
    expect(tasks.length).toBeLessThanOrEqual(4);
  });

  it("每条任务的 id / title / description / deliverable 都完整且唯一", () => {
    const tasks = starterTasks();
    for (const t of tasks) {
      expect(t.id.trim()).not.toBe("");
      expect(t.title.trim()).not.toBe("");
      expect(t.description.trim()).not.toBe("");
      expect(t.deliverable.trim()).not.toBe("");
    }
    expect(new Set(tasks.map((t) => t.id)).size).toBe(tasks.length);
  });

  it("按 id 能取到对应的示例任务", () => {
    const first = starterTasks()[0];
    const picked = pickStarterTask(first.id);
    expect(picked).not.toBeNull();
    expect(picked).toEqual(first);
  });

  it("未知 id 返回空", () => {
    expect(pickStarterTask("not-a-real-task")).toBeNull();
    expect(pickStarterTask("")).toBeNull();
  });

  it("返回副本，修改不会污染后续调用", () => {
    const tasks = starterTasks();
    tasks[0].title = "被篡改的标题";
    expect(starterTasks()[0].title).not.toBe("被篡改的标题");

    const picked = pickStarterTask(tasks[0].id)!;
    picked.description = "被篡改的描述";
    expect(pickStarterTask(tasks[0].id)!.description).not.toBe("被篡改的描述");
  });
});
