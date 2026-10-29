"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, describeError, isCancelled, streamPost } from "@/lib/api";
import { notify } from "@/components/Toast";
import { clock, type WsAgent, type WsMsg } from "@/lib/workspace";

/** 中栏：一句话指挥。复用员工对话接口，附件直接进该员工的私人知识库。 */

type Skill = { id: string; name: string; description?: string | null; source_label?: string };

const SUGGESTIONS = [
  "把这周的任务按优先级排一版，告诉我先做什么",
  "让小红把竞品定价页抓下来，做一版对比表",
  "把上周的交付物汇总成一份周报",
];

export default function WorkspaceChat({
  agent,
  agents,
  cid,
  onPickAgent,
  onOpenTrace,
  onCollapseOut,
  onExpandLeft,
  outHidden,
  leftHidden,
}: {
  agent: WsAgent | null;
  agents: WsAgent[];
  cid: string;
  onPickAgent: (a: WsAgent) => void;
  onOpenTrace: () => void;
  onCollapseOut: () => void;
  onExpandLeft: () => void;
  outHidden: boolean;
  leftHidden: boolean;
}) {
  const [msgs, setMsgs] = useState<WsMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const skillsRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!agent) { setMsgs([]); return; }
    let alive = true;
    setLoading(true);
    api.get<WsMsg[]>(`/agents/${agent.id}/chat/history?limit=50`)
      .then((d) => { if (alive) setMsgs(Array.isArray(d) ? d : []); })
      .catch(() => { if (alive) setMsgs([]); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [agent]);

  // 换员工 / 离开页面时掐断在途的流，避免上一轮的字落进新会话
  useEffect(() => {
    abortRef.current?.abort();
    setBusy(false);
    setStatus("");
  }, [agent?.id]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [msgs, busy, status]);

  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false);
      if (skillsRef.current && !skillsRef.current.contains(e.target as Node)) setSkillsOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const openSkills = useCallback(async () => {
    setSkillsOpen((v) => !v);
    if (skills === null && cid) {
      try {
        setSkills(await api.get<Skill[]>(`/skills?company_id=${cid}`));
      } catch {
        setSkills([]);
      }
    }
  }, [skills, cid]);

  const patchLast = useCallback((patch: (m: WsMsg) => WsMsg) => {
    setMsgs((prev) => {
      if (!prev.length) return prev;
      const next = prev.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!agent || busy || (!text && !files.length)) return;
    const target = agent;
    const ac = new AbortController();
    abortRef.current = ac;
    setBusy(true);
    setStatus("");
    setInput("");
    if (text) setMsgs((p) => [...p, { role: "boss", content: text }]);
    setMsgs((p) => (files.length ? [...p, { role: "boss", content: `📎 已上传 ${files.length} 个附件：${files.map((f) => f.name).join("、")}` }] : p));

    let acc = "";
    let started = false;
    try {
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        try {
          await api.upload(`/kb2/agents/${target.id}/upload`, fd);
        } catch (e) {
          notify("error", `${f.name} 上传失败：${describeError(e)}`);
        }
      }
      setFiles([]);
      if (!text) {
        setMsgs((p) => [...p, { role: "agent", content: "附件已经进我的知识库了，接下来要我拿它们做什么？" }]);
        return;
      }

      setMsgs((p) => [...p, { role: "agent", content: "", streaming: true }]);
      let serverError = "";
      await streamPost(`/agents/${target.id}/chat/stream`, { message: text }, {
        signal: ac.signal,
        onEvent: (ev) => {
          if (ev.type === "delta") {
            started = true;
            acc += String(ev.text || "");
            setStatus("");
            patchLast((m) => ({ ...m, content: acc, streaming: true }));
          } else if (ev.type === "status") {
            setStatus(String(ev.text || ""));
          } else if (ev.type === "done") {
            acc = String(ev.reply ?? acc);
            patchLast((m) => ({
              ...m,
              content: acc,
              streaming: false,
              file_name: ev.file?.file_name,
            }));
          } else if (ev.type === "error") {
            serverError = String(ev.message || "服务端处理失败");
          }
        },
      });
      if (serverError) {
        patchLast((m) => ({ ...m, content: acc || "（没有收到内容）", streaming: false }));
        setMsgs((p) => [...p, { role: "agent", content: `❌ ${serverError}` }]);
      }
    } catch (e) {
      if (ac.signal.aborted || isCancelled(e)) {
        // 主动停止：保留已经收到的字，并说清本轮没有落库
        patchLast((m) => ({
          ...m,
          content: m.content ? `${m.content}\n\n（已停止，这轮回复未保存到历史）` : "（已停止）",
          streaming: false,
        }));
      } else {
        patchLast((m) => ({
          ...m,
          content: started ? m.content : `❌ ${describeError(e, "发送失败")}`,
          streaming: false,
        }));
      }
    } finally {
      if (abortRef.current === ac) abortRef.current = null;
      setBusy(false);
      setStatus("");
    }
  }, [agent, busy, input, files, patchLast]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const title = agent ? agent.name : "工作台";
  const skillCount = useMemo(() => skills?.length ?? 0, [skills]);
  const lastMsg = msgs[msgs.length - 1];
  // 已经吐字之后就不再显示"正在思考"，除非在跑工具（status 有内容）
  const showThinking = busy && (!!status || !lastMsg?.streaming || !lastMsg?.content);

  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--background)" }}>
      {/* 会话头 */}
      <div className="flex items-center gap-2.5 px-4 shrink-0"
        style={{ height: 52, borderBottom: "1px solid var(--border)" }}>
        {leftHidden && (
          <button type="button" onClick={onExpandLeft} title="展开左栏 ⌘B" aria-label="展开左栏"
            className="w-8 h-8 -ml-1 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
            </svg>
          </button>
        )}
        <div className="min-w-0">
          <h1 className="text-sm font-semibold truncate">{title}</h1>
          <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
            {agent ? (agent.role || "数字员工") : "先选一个员工"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          {agent && (
            <button type="button" onClick={onOpenTrace}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              查看轨迹
            </button>
          )}
          <button type="button" onClick={onCollapseOut} title={outHidden ? "展开右栏 ⌘J" : "收起右栏 ⌘J"}
            aria-label={outHidden ? "展开右栏" : "收起右栏"}
            className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" />
            </svg>
          </button>
        </div>
      </div>

      {/* 消息流 */}
      <div className="flex-1 min-h-0 overflow-y-auto ws-scroll px-4 py-4">
        <div className="mx-auto flex max-w-[680px] flex-col gap-4">
          {loading && <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>正在读取对话…</p>}

          {!loading && !msgs.length && (
            <div className="py-8">
              <div className="w-9 h-9 rounded-xl flex items-center justify-center text-sm font-semibold mb-3"
                style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                {agent ? agent.name.slice(0, 1) : "⚡"}
              </div>
              <p className="text-sm font-medium">
                {agent ? `我在，${agent.name}。要做什么？` : "先选一个员工，或者说一句话"}
              </p>
              <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                一句话下达，我会拆解、派给团队，产出直接进右栏。
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s} type="button" onClick={() => { setInput(s); taRef.current?.focus(); }}
                    className="text-[11px] px-2.5 py-1.5 rounded-lg text-left hover:border-indigo-500/40"
                    style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {msgs.map((m, i) => (
            m.role === "boss" ? (
              <div key={i} className="self-end max-w-[86%]">
                <div className="rounded-2xl rounded-br-sm px-3.5 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap"
                  style={{ background: "#1a1b26", border: "1px solid #262735" }}>
                  {m.content}
                </div>
              </div>
            ) : (
              <div key={i} className="flex gap-2.5">
                <div className="w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-semibold shrink-0 mt-0.5"
                  style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                  {(agent?.name || "AI").slice(0, 1)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-medium">{agent?.name || "员工"}</span>
                    <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                      {agent?.role || ""} {m.created_at ? `· ${clock(m.created_at)}` : ""}
                    </span>
                  </div>
                  <div className="text-[13px] leading-relaxed whitespace-pre-wrap" style={{ color: "#c9cad5" }}>
                    {m.content}
                    {m.streaming && <span className="ws-caret" aria-hidden />}
                  </div>
                  {m.file_name && (
                    <p className="mt-1.5 text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-md"
                      style={{ background: "rgba(99,102,241,0.12)", color: "#c7c8ff" }}>
                      📄 {m.file_name} · 已存入交付中心
                    </p>
                  )}
                </div>
              </div>
            )
          ))}

          {showThinking && (
            <div className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
              <span className="w-3 h-3 rounded-full border-2 animate-spin"
                style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} aria-hidden />
              {status || (files.length ? "正在上传附件…" : `${agent?.name || "员工"} 正在思考…`)}
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>

      {/* 输入区 */}
      <div className="px-4 pb-3 shrink-0">
        <div className="mx-auto max-w-[680px] rounded-2xl p-2.5"
          style={{ border: "1px solid #2b2c3a", background: "var(--surface)" }}>
          {files.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1 pb-1.5">
              {files.map((f, i) => (
                <span key={`${f.name}-${i}`} className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-md"
                  style={{ border: "1px solid var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}>
                  <span className="truncate max-w-[160px]">{f.name}</span>
                  <button type="button" aria-label={`移除 ${f.name}`}
                    onClick={() => setFiles((p) => p.filter((_, idx) => idx !== i))}
                    className="hover:text-red-300">×</button>
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder={agent ? `说一句话，让 ${agent.name} 去做…` : "先选一个员工…"}
            className="w-full resize-none bg-transparent outline-none text-[13px] leading-relaxed px-1.5 py-1 max-h-32"
            style={{ color: "var(--foreground)" }}
          />
          <div className="flex items-center gap-1.5 px-1 pt-1.5 border-t" style={{ borderColor: "var(--border)" }}>
            <input ref={fileRef} type="file" multiple className="hidden"
              accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls,.pptx,.ppt,image/*"
              onChange={(e) => {
                const list = Array.from(e.target.files || []);
                if (list.length) setFiles((p) => [...p, ...list]);
                e.target.value = "";
              }} />
            <button type="button" onClick={() => fileRef.current?.click()}
              className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M21 11.5l-8.5 8.5a5 5 0 01-7-7L14 4.5a3.3 3.3 0 014.7 4.7l-8.5 8.5a1.7 1.7 0 01-2.4-2.4L15 8" />
              </svg>
              附件
            </button>

            <div className="relative" ref={skillsRef}>
              <button type="button" onClick={openSkills}
                className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                ⚡ 技能{skills !== null ? ` · ${skillCount}` : ""}
              </button>
              {skillsOpen && (
                <div className="absolute bottom-9 left-0 w-64 rounded-xl p-2 z-20 shadow-2xl"
                  style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                  <p className="text-[10px] px-1 pb-1.5" style={{ color: "var(--muted)" }}>
                    全公司可用技能（在技能中心绑定到员工）
                  </p>
                  <div className="max-h-56 overflow-y-auto ws-scroll">
                    {skills?.slice(0, 30).map((s) => (
                      <div key={s.id} className="px-1.5 py-1 rounded text-[11px] hover:bg-white/[0.04]">
                        <span className="truncate">{s.name}</span>
                        <span className="text-[9px] ml-1.5" style={{ color: "#55555f" }}>{s.source_label}</span>
                      </div>
                    ))}
                    {skills?.length === 0 && <p className="px-1.5 py-2 text-[11px]" style={{ color: "var(--muted)" }}>还没有技能</p>}
                  </div>
                  <a href="/skill-center" className="block text-[10px] px-1.5 pt-1.5 text-indigo-300">去技能中心 →</a>
                </div>
              )}
            </div>

            <div className="relative" ref={pickerRef}>
              <button type="button" onClick={() => setPickerOpen((v) => !v)}
                className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06] max-w-[140px]"
                style={{ border: "1px solid rgba(99,102,241,0.35)", background: "rgba(99,102,241,0.1)", color: "#c7c8ff" }}>
                <span className="truncate">{agent ? agent.name : "选员工"}</span>
                <span aria-hidden className="opacity-60">▾</span>
              </button>
              {pickerOpen && (
                <div className="absolute bottom-9 left-0 w-56 rounded-xl p-1.5 z-20 shadow-2xl"
                  style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                  {agents.map((a) => (
                    <button key={a.id} type="button"
                      onClick={() => { onPickAgent(a); setPickerOpen(false); }}
                      className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[11px] text-left hover:bg-white/[0.06]"
                      style={{ color: a.id === agent?.id ? "#fff" : "var(--muted)" }}>
                      <span className="w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-semibold"
                        style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                        {a.name.slice(0, 1)}
                      </span>
                      <span className="flex-1 truncate">{a.name}</span>
                      {a.role && <span className="text-[9px] truncate max-w-[80px]" style={{ color: "#55555f" }}>{a.role}</span>}
                    </button>
                  ))}
                  {!agents.length && <p className="px-2 py-1.5 text-[11px]" style={{ color: "var(--muted)" }}>还没有员工</p>}
                </div>
              )}
            </div>

            <span className="ml-auto text-[10px] hidden sm:block" style={{ color: "#4a4b58" }}>
              {busy ? "生成中 · 可随时停止" : "↵ 发送 · ⇧↵ 换行"}
            </span>
            {busy ? (
              <button type="button" onClick={stop}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg font-medium"
                style={{ background: "rgba(248,113,113,0.14)", color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}>
                <span className="w-2.5 h-2.5 rounded-[2px]" style={{ background: "currentColor" }} aria-hidden />
                停止
              </button>
            ) : (
              <button type="button" onClick={send} disabled={!input.trim() && !files.length}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg font-medium disabled:opacity-40"
                style={{ background: "linear-gradient(135deg,#6366f1,#4f46e5)", color: "#fff" }}>
                发送
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
