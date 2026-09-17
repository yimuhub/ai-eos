"use client";
import { useRef, useState } from "react";

/* ============================================================
   通用附件选择器：所有输入场景统一使用
   - 选择即真实上传（POST /assets/upload，落盘+入库）
   - 展示已传附件标签（文件名 + 大小），可单个移除
   - onChange 回调返回完整附件列表 [{id, name, size}]
   ============================================================ */

export type Attachment = { id: string; name: string; size: number };

const ACCEPT = ".pdf,.docx,.txt,.md,.csv,.json,.xlsx,.pptx,.png,.jpg,.jpeg,.webp";
const MAX_MB = 20;

export default function AttachmentPicker({
  value,
  onChange,
  label = "添加附件",
  disabled = false,
}: {
  value: Attachment[];
  onChange: (next: Attachment[]) => void;
  label?: string;
  disabled?: boolean;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [err, setErr] = useState("");

  const pickFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setErr("");
    setUploading(true);
    const added: Attachment[] = [];
    for (const f of Array.from(files)) {
      if (f.size > MAX_MB * 1024 * 1024) {
        setErr(`「${f.name}」超过 ${MAX_MB}MB 上限`);
        continue;
      }
      try {
        const fd = new FormData();
        fd.append("file", f);
        // company_id 由后端从 token 校验，这里取 localStorage 的 cid 或让调用方传入
        const cs = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/companies`, {
          headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        }).then((r) => r.json());
        const cid = cs[0]?.id;
        const r = await fetch(
          `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/upload?company_id=${cid}`,
          { method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` }, body: fd });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        added.push({ id: d.id, name: d.name || f.name, size: d.size ?? f.size });
      } catch {
        // 上传失败也保留本地信息，提交时由调用方决定
        added.push({ id: "", name: f.name, size: f.size });
        setErr("部分附件上传失败，已暂存本地");
      }
    }
    onChange([...value, ...added]);
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  };

  const remove = (idx: number) => onChange(value.filter((_, i) => i !== idx));

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <label className={`btn-ghost text-xs cursor-pointer inline-flex items-center gap-1.5 ${disabled ? "opacity-40 pointer-events-none" : ""}`}>
        📎 {uploading ? "上传中…" : label}
        <input ref={fileRef} type="file" multiple accept={ACCEPT} className="hidden"
          disabled={disabled || uploading}
          onChange={(e) => pickFiles(e.target.files)} />
      </label>
      {value.map((a, i) => (
        <span key={`${a.id}-${i}`}
          className="text-[11px] px-2 py-0.5 rounded-full inline-flex items-center gap-1"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          📄 {a.name}
          {a.size > 0 && <span className="opacity-60">{Math.max(1, Math.round(a.size / 1024))}KB</span>}
          <button type="button" onClick={() => remove(i)}
            className="hover:text-red-300 ml-0.5" title="移除">✕</button>
        </span>
      ))}
      {err && <span className="text-[11px] text-amber-300">{err}</span>}
    </div>
  );
}
