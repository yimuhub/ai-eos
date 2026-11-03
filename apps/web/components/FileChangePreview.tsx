"use client";

// ## 审批单里的文件改动预览
//
// 把后端建单时演算好的 diff（`payload.preview`，只演不写）讲清楚：路径、增删
// 行数、改动处数，以及逐行染色的 diff。干预中心和移动端收件箱共用这一个组件，
// 避免两处各写一份、其中一份悄悄退化。纯逻辑在 `lib/approvalPreview.ts`。

import {
  diffLines,
  diffLineTone,
  filePreviewOf,
  type FileChangePreview,
} from "@/lib/approvalPreview";

const TONE_COLOR: Record<ReturnType<typeof diffLineTone>, string> = {
  add: "var(--success)",
  del: "var(--danger)",
  hunk: "var(--accent-2)",
  meta: "var(--muted)",
};

export default function FileChangeCard({
  preview,
  maxHeightClass = "max-h-56",
}: {
  preview: FileChangePreview;
  maxHeightClass?: string;
}) {
  if (!preview.available) {
    return (
      <div
        className="mt-2 rounded-lg border px-2.5 py-2 text-xs leading-5"
        style={{
          borderColor: "rgba(251,191,36,0.4)",
          background: "rgba(251,191,36,0.08)",
        }}
      >
        <div>⚠️ 这次改动没法预先演算：{preview.reason || "未知原因"}</div>
        <div className="text-[var(--muted)]">
          批准后执行器仍会重新校验，演算失败的原因会照样拦下来。
        </div>
      </div>
    );
  }

  const lines = diffLines(preview.diff);

  return (
    <div
      className="mt-2 rounded-lg border overflow-hidden"
      style={{ borderColor: "var(--border)" }}
    >
      <div
        className="flex items-baseline justify-between gap-2 px-2.5 py-1.5 text-xs flex-wrap"
        style={{ background: "rgba(255,255,255,0.04)" }}
      >
        <span className="font-mono break-all" style={{ color: "var(--foreground)" }}>
          {preview.create ? "＋ 新建 " : "✎ "}
          {preview.path}
        </span>
        <span className="shrink-0 font-mono">
          <span style={{ color: "var(--success)" }}>+{preview.added_lines ?? 0}</span>
          {" "}
          <span style={{ color: "var(--danger)" }}>-{preview.removed_lines ?? 0}</span>
          {" "}
          <span className="text-[var(--muted)]">
            {preview.hunks ?? 0} 处
            {preview.edits ? ` · ${preview.edits} 处替换` : ""}
          </span>
        </span>
      </div>

      {preview.changed === false ? (
        <p className="px-2.5 py-2 text-xs text-[var(--muted)]">
          内容与现状一致，批准也不会改盘。
        </p>
      ) : lines.length > 0 ? (
        <pre
          className={`overflow-auto px-2.5 py-2 text-[11px] leading-5 font-mono ${maxHeightClass}`}
          style={{ background: "rgba(0,0,0,0.25)" }}
        >
          {lines.map((line, i) => (
            <span
              // diff 里可以有完全相同的行，索引是唯一可用的 key
              key={i}
              className="block whitespace-pre"
              style={{ color: TONE_COLOR[diffLineTone(line)] }}
            >
              {line || " "}
            </span>
          ))}
          {preview.diff_truncated && (
            <span className="block text-[var(--muted)]">
              … diff 过长已截断，完整改动以执行后的审计日志为准
            </span>
          )}
        </pre>
      ) : null}
    </div>
  );
}

/** 只有 payload 里带 preview 才渲染——命令 / 外发那类审批单不留空占位。 */
export function ApprovalFilePreview({
  payload,
  maxHeightClass,
}: {
  payload?: Record<string, unknown> | null;
  maxHeightClass?: string;
}) {
  const preview = filePreviewOf(payload);
  return preview ? (
    <FileChangeCard preview={preview} maxHeightClass={maxHeightClass} />
  ) : null;
}
