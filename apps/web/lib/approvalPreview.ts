// ## 审批单上的「改了哪几行」
//
// 文件写入 / 打补丁进审批门时，老板过去只看到 params 截断到 200 字的 JSON——
// 对 `write_file` 来说那必然是一串断掉的正文，既看不出改了哪个文件，也看不出
// 改了哪几行。三档审批在这种信息量下就是橡皮图章。
//
// 后端建单时已经用执行器的 dry_run 分支演算好 diff（只演不写）放进
// `payload.preview`。这里只放纯函数：把预览取出来、决定除 diff 外还要摊开哪些
// 字段、给 diff 行定色调——都可在 node 环境下直接测。渲染在
// `components/FileChangePreview.tsx`。

export type FileChangePreview = {
  /** 预演是否成功。失败时后端仍会建单，reason 说明为什么看不到 diff */
  available?: boolean;
  reason?: string;
  path?: string;
  /** 与现有文件相比是否真的会有变化 */
  changed?: boolean;
  /** 新建文件：没有原件可回滚 */
  create?: boolean;
  hunks?: number;
  added_lines?: number;
  removed_lines?: number;
  /** apply_patch 的替换处数 */
  edits?: number;
  diff?: string;
  diff_truncated?: boolean;
};

export function filePreviewOf(
  payload?: Record<string, unknown> | null,
): FileChangePreview | null {
  const raw = payload?.preview;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return raw as FileChangePreview;
}

/**
 * 审批单里除 diff 之外还要摊开的字段。
 *
 * 正文与补丁锚点已经逐行画在 diff 里，再铺开一遍会把审批单埋掉；
 * `preview` 本身也已由专用组件呈现，不需要重复一份 JSON。
 */
export function approvalPayloadForDisplay(
  payload: Record<string, unknown>,
): Record<string, unknown> {
  const rest: Record<string, unknown> = { ...payload };
  delete rest.preview;
  const params = rest.params;
  if (filePreviewOf(payload)?.available && params && typeof params === "object"
      && !Array.isArray(params)) {
    const compact: Record<string, unknown> = { ...(params as Record<string, unknown>) };
    delete compact.content;
    delete compact.edits;
    rest.params = compact;
  }
  return rest;
}

/** diff 行的色调。`+++`/`---` 是文件头，不算增删，只能归 meta。 */
export function diffLineTone(line: string): "add" | "del" | "hunk" | "meta" {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  if (line.startsWith("@@")) return "hunk";
  return "meta";
}

/** diff 文本 → 逐行数组（去掉结尾空行，不然渲染出一行多余空白）。 */
export function diffLines(diff?: string): string[] {
  if (!diff) return [];
  return diff.replace(/\n$/, "").split("\n");
}
