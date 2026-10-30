"use client";
import { useEffect, useMemo, useState } from "react";
import type { WsAgent, WsChatGroup } from "@/lib/workspace";

/**
 * 新建 / 编辑群聊：起名 + 勾人。
 *
 * 内置分组也能编辑（加人、改名），但它的成员来自预设；删掉的人不会被下次点开
 * 自动补回来——只有「补齐预设员工」才会按预设补缺。
 */
export default function GroupEditor({
  open,
  group,
  agents,
  busy,
  onClose,
  onSave,
  onDelete,
}: {
  open: boolean;
  /** null = 新建 */
  group: WsChatGroup | null;
  agents: WsAgent[];
  busy: boolean;
  onClose: () => void;
  onSave: (name: string, agentIds: string[]) => void;
  onDelete?: () => void;
}) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [q, setQ] = useState("");

  useEffect(() => {
    if (!open) return;
    setName(group?.name || "");
    setPicked(group?.agent_ids || []);
    setQ("");
  }, [open, group]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const filtered = useMemo(() => {
    const key = q.trim().toLowerCase();
    if (!key) return agents;
    return agents.filter((a) =>
      a.name.toLowerCase().includes(key) || (a.role || "").toLowerCase().includes(key));
  }, [agents, q]);

  if (!open) return null;

  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const canSave = !!name.trim() && picked.length > 0 && !busy;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true"
      aria-label={group ? "编辑群聊" : "新建群聊"}>
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden />
      <div className="relative w-full max-w-md rounded-2xl shadow-2xl overflow-hidden"
        style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
        <div className="px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
          <p className="text-sm font-semibold">{group ? "编辑群聊" : "新建群聊"}</p>
          <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
            拉几位员工进来，之后在群里说一句话，他们会依次回应。
          </p>
        </div>

        <div className="px-4 py-3 space-y-3">
          <label className="block">
            <span className="text-[10px] tracking-[0.12em] uppercase" style={{ color: "#55555f" }}>群名称</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={60}
              placeholder="例如：双十一作战群 / 官网改版评审"
              className="mt-1.5 w-full h-9 px-2.5 rounded-lg text-[13px] outline-none focus:ring-1 focus:ring-indigo-500/50"
              style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--foreground)" }}
            />
          </label>

          <div>
            <div className="flex items-center justify-between">
              <span className="text-[10px] tracking-[0.12em] uppercase" style={{ color: "#55555f" }}>
                成员 · 已选 {picked.length}
              </span>
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="搜索员工"
                className="w-32 h-6 px-2 rounded-md text-[11px] outline-none"
                style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--foreground)" }}
              />
            </div>
            <div className="mt-1.5 max-h-[248px] overflow-y-auto ws-scroll rounded-xl p-1"
              style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>
              {filtered.map((a) => {
                const on = picked.includes(a.id);
                return (
                  <button key={a.id} type="button" onClick={() => toggle(a.id)} data-agent={a.id}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left hover:bg-white/[0.05]"
                    style={on ? { background: "rgba(99,102,241,0.14)", color: "#e6e7ff" } : { color: "var(--muted)" }}>
                    <span className="w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-semibold shrink-0"
                      style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                      {a.name.slice(0, 1)}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-xs truncate">{a.name}</span>
                      {a.role && <span className="block text-[9px] truncate" style={{ color: "#55555f" }}>{a.role}</span>}
                    </span>
                    <span className="w-4 h-4 rounded flex items-center justify-center text-[10px] shrink-0"
                      style={on
                        ? { background: "var(--accent)", color: "#fff" }
                        : { border: "1px solid var(--border)", color: "transparent" }}>
                      ✓
                    </span>
                  </button>
                );
              })}
              {!filtered.length && (
                <p className="px-2 py-3 text-[11px]" style={{ color: "var(--muted)" }}>
                  {agents.length ? "没有匹配的员工" : "还没有员工，先去招人"}
                </p>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 px-4 py-3" style={{ borderTop: "1px solid var(--border)" }}>
          {group && onDelete && (
            <button type="button" onClick={onDelete} disabled={busy}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-red-500/10 disabled:opacity-40"
              style={{ color: "#fca5a5" }}>
              删除群聊
            </button>
          )}
          <span className="ml-auto text-[10px] hidden sm:block" style={{ color: "#4a4b58" }}>
            {!name.trim() ? "先起个名字" : !picked.length ? "至少选一位员工" : ""}
          </span>
          <button type="button" onClick={onClose} disabled={busy}
            className="text-[11px] px-3 py-1.5 rounded-lg hover:bg-white/[0.06]"
            style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
            取消
          </button>
          <button type="button" onClick={() => onSave(name.trim(), picked)} disabled={!canSave}
            className="text-[11px] px-3 py-1.5 rounded-lg font-medium disabled:opacity-40"
            style={{ background: "linear-gradient(135deg,#6366f1,#4f46e5)", color: "#fff" }}>
            {busy ? "保存中…" : group ? "保存" : "建群"}
          </button>
        </div>
      </div>
    </div>
  );
}
