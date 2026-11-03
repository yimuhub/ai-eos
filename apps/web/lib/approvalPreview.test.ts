import { describe, expect, it } from "vitest";

import {
  approvalPayloadForDisplay,
  diffLines,
  diffLineTone,
  filePreviewOf,
} from "./approvalPreview";

// ## 为什么钉这一组
//
// 审批门的取舍全靠「老板能看到什么」：diff 取不出来、或者取出来了却被正文 JSON
// 淹没，三档审批就退化成橡皮图章。这几个函数是审批单信息量的唯一来源，
// 一旦有人改回「整坨 payload 摊出来」，这里会先红。

describe("filePreviewOf", () => {
  it("没有 preview 就不渲染（命令 / 外发类审批单）", () => {
    expect(filePreviewOf(null)).toBeNull();
    expect(filePreviewOf(undefined)).toBeNull();
    expect(filePreviewOf({ capability: "run_shell", params: { command: "ls" } })).toBeNull();
    // 后端万一塞进非对象（字符串 / 数组），也不能把渲染搞崩
    expect(filePreviewOf({ preview: "diff..." })).toBeNull();
    expect(filePreviewOf({ preview: [1, 2] })).toBeNull();
  });

  it("取出改动预览", () => {
    const p = filePreviewOf({
      preview: { available: true, path: "/repo/app.py", added_lines: 3, removed_lines: 1 },
    });
    expect(p).toMatchObject({ available: true, path: "/repo/app.py", added_lines: 3 });
  });
});

describe("approvalPayloadForDisplay", () => {
  it("有 diff 时不再铺开正文与锚点", () => {
    const out = approvalPayloadForDisplay({
      capability: "write_file",
      awaiting_execution: true,
      preview: { available: true, path: "/repo/app.py", diff: "@@ x @@" },
      params: { path: "/repo/app.py", content: "一整份文件正文".repeat(50), dry_run: false },
    });
    expect(out.params).toEqual({ path: "/repo/app.py", dry_run: false });
    expect(out.preview).toBeUndefined();
    expect(out.capability).toBe("write_file");
  });

  it("预演失败时保留原参数——没有 diff 可看，正文就是唯一线索", () => {
    const params = { path: "/repo/app.py", content: "abc\n" };
    const out = approvalPayloadForDisplay({
      capability: "write_file",
      preview: { available: false, reason: "文件不存在" },
      params,
    });
    expect(out.params).toEqual(params);
  });

  it("命令类审批原样摊开", () => {
    const out = approvalPayloadForDisplay({
      capability: "run_shell",
      params: { command: "ls -la" },
    });
    expect(out).toEqual({ capability: "run_shell", params: { command: "ls -la" } });
  });

  it("不改原对象", () => {
    const payload = { params: { content: "x" }, preview: { available: true } };
    approvalPayloadForDisplay(payload);
    expect(payload.params).toEqual({ content: "x" });
  });
});

describe("diffLineTone / diffLines", () => {
  it("增删与 hunk 头分别上色", () => {
    expect(diffLineTone("+a = 1")).toBe("add");
    expect(diffLineTone("-a = 0")).toBe("del");
    expect(diffLineTone("@@ -1,2 +1,2 @@")).toBe("hunk");
    // 文件头那一行不是增删，染成绿色会让人以为整份文件是新写的
    expect(diffLineTone("+++ /repo/app.py")).toBe("meta");
    expect(diffLineTone("--- /repo/app.py")).toBe("meta");
    expect(diffLineTone(" 上下文行")).toBe("meta");
  });

  it("拆行去掉结尾空白行，空 diff 不给一行假内容", () => {
    expect(diffLines("-a\n+b\n")).toEqual(["-a", "+b"]);
    expect(diffLines("")).toEqual([]);
    expect(diffLines(undefined)).toEqual([]);
  });
});
