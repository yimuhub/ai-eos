"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Agent = { id: string; name: string };
type Session = { id: string; agent_id: string; title: string; type: string; status: string; summary?: string; created_at?: string };
type Point = { id: string; session_id: string; point: string; tag: string; approved: boolean; applied?: boolean; created_at?: string };

const TYPE_ZH: Record<string, string> = { CONVERSATION: "对话训练", TEXT: "文本精修" };

export default function TrainingPage() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [cid, setCid] = useState("");

  useEffect(() => {
    api.get("/companies").then((cs: any[]) => {
      const c = cs[0]?.id;
      if (c) {
        setCid(c);
        api.get(`/training/sessions?company_id=${c}&status=APPLIED`).then(setSessions).catch(() => {});
      }
    }).catch(() => {});
  }, []);

  async function startNew() {
    if (!title || !content || !cid) return;
    setBusy(true);
    try {
      const r = await api.post(`/training/sessions?company_id=${cid}`, {
        training_type: "CONVERSATION",
        title,
        raw_input: content,
      });
      setMsg(`✅ 训练完成，提取 ${r.points} 个学习点，待确认`);
      setTitle("");
      setContent("");
      api.get(`/training/sessions?company_id=${cid}&status=APPLIED`).then(setSessions).catch(() => {});
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
    setBusy(false);
  }

  async function approvePoint(pid: string, apply: boolean) {
    try {
      await api.post(`/training/points/${pid}/approve`, { apply });
      setMsg(apply ? "✅ 已应用" : "已拒绝");
      api.get(`/training/points?session_id=${selected}`).then(setPoints).catch(() => {});
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="mb-4">
          <h1 className="text-2xl font-bold tracking-tight">🎓 训练中心</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>对话训练 · 学习点提取 · 技能沉淀</p>
        </div>
      <div className="grid grid-cols-[260px_1fr] gap-4">
        {/* 会话列表 */}
        <div className="card card-static p-3">
          <h3 className="font-medium mb-2 text-[var(--foreground)]">训练会话</h3>
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`p-2 mb-1 rounded cursor-pointer text-sm ${selected === s.id ? "bg-white/[0.04] border border-blue-700 text-[var(--foreground)]" : "card border border-[var(--border)] text-[var(--muted)] hover:border-[var(--border)]"}`}
              onClick={() => {
                setSelected(s.id);
                api.get(`/training/points?session_id=${s.id}`).then(setPoints).catch(() => {});
              }}
            >
              <div>{s.title}</div>
              <div className="text-xs text-[var(--muted)] mt-1">{TYPE_ZH[s.type] || s.type}</div>
            </div>
          ))}
          {!sessions.length && <p className="text-xs text-[var(--muted)]">暂无已完成的训练</p>}
        </div>

        {/* 内容区 */}
        <div className="space-y-3">
          {/* 新建 */}
          <div className="card card-static p-3 space-y-2">
            <h3 className="font-medium text-[var(--foreground)]">发起新训练</h3>
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="训练标题（如：抖音内容创作规范）"
              className="input" />
            <textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="要教给 TA 的内容、规范、案例……"
              className="input h-24" />
            <button onClick={startNew} disabled={busy || !title}
              className="btn-primary disabled:opacity-40">
              {busy ? "AI 提取中…" : "发起训练（提取后需确认）"}
            </button>
          </div>
          {msg && <p className="text-sm text-blue-300">{msg}</p>}

          {/* 学习点 */}
          {selected && points.length > 0 && (
            <div className="space-y-2">
              <h3 className="font-medium text-[var(--foreground)]">学习点（需确认）</h3>
              {points.map((p) => (
                <div key={p.id} className="card card-static p-3 text-sm text-[var(--foreground)]">
                  <div className="flex items-start justify-between">
                    <span className="flex-1">{p.point}</span>
                    <span className="ml-2 text-xs text-[var(--muted)]">{p.tag}</span>
                  </div>
                  <div className="mt-2 flex gap-2">
                    <button onClick={() => approvePoint(p.id, true)}
                      className="px-2 py-0.5 bg-green-700 hover:bg-green-600 text-white rounded text-xs">应用</button>
                    <button onClick={() => approvePoint(p.id, false)}
                      className="px-2 py-0.5 bg-white/[0.06] hover:bg-white/[0.1] text-[var(--foreground)] rounded text-xs">拒绝</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
