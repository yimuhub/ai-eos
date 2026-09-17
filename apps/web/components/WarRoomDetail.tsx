"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useParams, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

/* ============================================================
   项目作战室（War Room）
   老板下达目标 → 负责人分析拆解 → 成员群聊宣告 → 负责人校对
   → 并行执行 → 产出物提交 → 可视化审核（负责人/老板人工干预）
   → 负责人整合最终交付
   ============================================================ */

type WRTask = {
  id: string; seq: number; agent_id: string; agent_name: string;
  title: string; detail?: string | null;
  announce_status: "pending" | "announced" | "acknowledged" | "corrected";
  announce_text?: string | null; leader_feedback?: string | null;
  status: "todo" | "running" | "submitted" | "approved" | string;
  deliverable_name?: string | null; deliverable_path?: string | null;
  deliverable_note?: string | null;
  reviewed_by?: string | null; review_comment?: string | null;
};
type WRSession = {
  id: string; goal: string; status: string;
  leader_agent_id: string; leader_name: string;
  analysis?: any; deliverable_format?: string | null;
  deliverable_rule?: string | null; final_output_path?: string | null;
  tasks: WRTask[];
};

const STATUS_FLOW = [
  ["analyzing", "负责人分析"], ["announcing", "分工宣告"], ["confirmed", "校对确认"],
  ["executing", "并行执行"], ["done", "交付完成"],
];
const ANN_ZH: Record<string, { label: string; cls: string }> = {
  pending: { label: "待宣告", cls: "bg-white/[0.06] text-[var(--muted)]" },
  announced: { label: "已宣告", cls: "bg-sky-500/15 text-sky-300" },
  acknowledged: { label: "✓ 已确认", cls: "bg-emerald-500/15 text-emerald-300" },
};
const TASK_ZH: Record<string, { label: string; cls: string }> = {
  todo: { label: "待执行", cls: "bg-white/[0.06] text-[var(--muted)]" },
  running: { label: "执行中", cls: "bg-cyan-500/15 text-cyan-300" },
  submitted: { label: "待审核", cls: "bg-amber-500/15 text-amber-300" },
  approved: { label: "✓ 审核通过", cls: "bg-emerald-500/15 text-emerald-300" },
};

export default function WarRoomDetail() {
  const router = useParams();
  const search = useSearchParams();
  const projectId = (router as any)?.id as string | undefined;
  const sidParam = (router as any)?.sid as string | undefined || search?.get("sid") || "";
  const [mounted, setMounted] = useState(false);
  const [cid, setCid] = useState("");
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [sessions, setSessions] = useState<WRSession[]>([]);
  const [active, setActive] = useState<WRSession | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  // 发起向导
  const [showKickoff, setShowKickoff] = useState(false);
  const [goal, setGoal] = useState("");
  const [leaderId, setLeaderId] = useState("");
  const [fmt, setFmt] = useState("md");
  // 校对反馈
  const [rejectTask, setRejectTask] = useState<WRTask | null>(null);
  const [feedback, setFeedback] = useState("");
  // 人工审核
  const [reviewTask, setReviewTask] = useState<WRTask | null>(null);
  const [reviewComment, setReviewComment] = useState("");
  const timelineRef = useRef<HTMLDivElement>(null);
  const prev_ref = useRef<any>(null);
  prev_ref.current = active; // keep latest for load()

  const [team, setTeam] = useState<any | null>(null);
  const [chatMsgs, setChatMsgs] = useState<any[]>([]);
  const [sayText, setSayText] = useState("");
  const [previewFile, setPreviewFile] = useState<{ name: string; url: string; content?: string } | null>(null);
  const chatBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    setAgents(await api.get(`/agents?company_id=${cs[0].id}`));
    try {
      let sid: string | undefined;
      if (projectId) {
        const d = await api.get(`/war-room/sessions?company_id=${cs[0].id}&project_id=${projectId}`);
        setSessions(d.sessions || []);
        const a = (prev_ref.current ?? (d.sessions || [])[0]) || null;
        setActive(a);
        sid = a?.id;
      } else if (sidParam) {
        const d = await api.get(`/war-room/${sidParam}?company_id=${cs[0].id}`);
        setSessions([d]);
        setActive(d);
        sid = d.id;
      }
      if (sid) {
        try { setTeam(await api.get(`/war-room/sessions/${sid}/team?company_id=${cs[0].id}`)); } catch {}
        try {
          const ch = await api.get(`/war-room/sessions/${sid}/chat?company_id=${cs[0].id}`);
          setChatMsgs((prev) => (ch.messages?.length >= prev.length ? ch.messages : prev));
        } catch {}
        try {
          const ch = await api.get(`/war-room/sessions/${sid}/chat?company_id=${cs[0].id}`);
          setChatMsgs(ch.messages || []);
        } catch {}
      } else { setTeam(null); setChatMsgs([]); }
    } catch {}
  }, [projectId, sidParam]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (chatBoxRef.current) chatBoxRef.current.scrollTop = chatBoxRef.current.scrollHeight;
  }, [chatMsgs]);
  // 执行中自动刷新
  // 只依赖 status 而不是整个 active 对象：对象每次轮询回来都是新引用，
  // 用它当依赖会导致定时器被反复重建（刷新节奏失控）。
  const activeStatus = active?.status;
  useEffect(() => {
    if (!activeStatus || !["analyzing", "announcing", "executing", "confirmed"].includes(activeStatus)) return;
    const t = setInterval(load, activeStatus === "analyzing" ? 3000 : 6000);
    return () => clearInterval(t);
  }, [activeStatus, load]);

  async function kickoff() {
    if (!goal.trim() || !leaderId) return;
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/kickoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ company_id: cid, goal: goal.trim(),
          leader_agent_id: leaderId, project_id: projectId,
          deliverable_format: fmt }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      // 提交成功即关闭弹窗；负责人在后台分析，看板自动轮询刷新
      setShowKickoff(false);
      setGoal("");
      setMsg("🧠 目标已下达，负责人正在分析拆解（通常需 10-30 秒），下方看板会自动更新…");
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setBusy(false);
  }

  async function announce() {
    if (!active) return;
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/${active.id}/announce?company_id=${cid}`, {
        method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setMsg("✅ 全员已在群聊复述各自任务");
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setBusy(false);
  }

  async function bossSay() {
    if (!sayText.trim() || !active) return;
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/sessions/${active.id}/say?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ text: sayText.trim() }),
      });
      setSayText("");
      setMsg("💬 已发送，成员正在回应…");
      // 立即刷新一次（显示老板消息），再延时拉取 Agent 回应
      const ch0 = await api.get(`/war-room/sessions/${active.id}/chat?company_id=${cid}`);
      setChatMsgs(ch0.messages || []);
      setTimeout(async () => {
        try {
          const ch = await api.get(`/war-room/sessions/${active!.id}/chat?company_id=${cid}`);
          setChatMsgs(ch.messages || []);
          setMsg("");
        } catch {}
      }, 12000);
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  async function openFile(sessId: string, name: string | null | undefined) {
    if (!name) return;
    const url = `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/sessions/${sessId}/file?company_id=${cid}&name=${encodeURIComponent(name)}`;
    setPreviewFile({ name, url });
    // 文本类直接拉内容预览；二进制（docx/pptx/xlsx）提示下载
    const ext = (name.split(".").pop() || "").toLowerCase();
    if (["md", "txt", "csv", "json"].includes(ext)) {
      try {
        const r = await fetch(url, { headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
        setPreviewFile({ name, url, content: await r.text() });
      } catch { setPreviewFile({ name, url, content: "加载失败" }); }
    } else {
      setPreviewFile({ name, url, content: undefined });
    }
  }

  async function ack(task: WRTask, correct: boolean, fb?: string) {
    const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/${active!.id}/acknowledge?company_id=${cid}`, {
      method: "POST",
      headers: { "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
      body: JSON.stringify({ task_id: task.id, correct, feedback: fb }),
    });
    const d = await r.json();
    if (!r.ok) { setMsg("❌ " + (d.detail?.message || d.detail || "")); return; }
    setRejectTask(null); setFeedback("");
    if (d.all_acknowledged) setMsg("✅ 全部任务校对通过，可以开工！");
    await load();
  }

  async function startExec() {
    const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/${active!.id}/start?company_id=${cid}`, {
      method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
    const d = await r.json();
    if (!r.ok) { setMsg("❌ " + (d.detail || "")); return; }
    setMsg(`🚀 已派发 ${d.dispatched} 名成员并行执行`);
    await load();
  }

  async function submitReview(task: WRTask, approve: boolean, comment: string, reviewer: string) {
    const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/tasks/${task.id}/review?company_id=${cid}`, {
      method: "POST",
      headers: { "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
      body: JSON.stringify({ approve, comment, reviewer }),
    });
    setReviewTask(null); setReviewComment("");
    setMsg(approve ? "✅ 审核通过" : "↩️ 已打回重做");
    await load();
  }

  async function finalize() {
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/${active!.id}/finalize?company_id=${cid}`, {
        method: "POST", headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` } });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setMsg(`🎉 最终方案已整合完成（${d.merged_parts} 部分）`);
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setBusy(false);
  }

  const stageIdx = active ? STATUS_FLOW.findIndex(([k]) => k === active.status) : -1;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(99,102,241,0.12) 0%, rgba(34,211,238,0.08) 100%)", border: "1px solid rgba(99,102,241,0.25)" }}>
        <div className="flex items-center gap-4 flex-wrap">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>🎯</div>
          <div>
            <h1 className="text-xl font-bold">项目作战室</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              负责人分析拆解 · 群聊宣告互认 · 并行执行 · 可视化审核 · 统一交付
            </p>
          </div>
          <button onClick={() => setShowKickoff(true)} className="ml-auto btn-primary">
            🎯 下达作战目标
          </button>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg}
        </div>
      )}

      {/* 会话列表 */}
      {sessions.length > 1 && (
        <div className="flex gap-2 mb-4 overflow-x-auto pb-1">
          {sessions.map((s) => (
            <button key={s.id} onClick={() => setActive(s)}
              className={`px-3.5 py-2 rounded-xl text-xs whitespace-nowrap transition-all border ${
                active?.id === s.id ? "bg-indigo-600 text-white border-indigo-600 font-medium"
                  : "hover:bg-white/[0.04]"}`}
              style={active?.id === s.id ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
              🎯 {s.goal.slice(0, 18)}… · {s.status}
            </button>
          ))}
        </div>
      )}

      {!active && (
        <div className="card card-static text-center py-16 space-y-2">
          <p className="text-sm">还没有作战会话</p>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            点击右上角「下达作战目标」，负责人将自动分析并组建跨部门团队
          </p>
        </div>
      )}

      {active && (
        <div className="space-y-5">
          {/* 分析中 / 失败提示 */}
          {active.status === "analyzing" && (
            <div className="card card-static p-5 flex items-center gap-4"
              style={{ border: "1px solid rgba(56,189,248,0.35)" }}>
              <span className="w-10 h-10 rounded-full flex items-center justify-center text-xl"
                style={{ background: "linear-gradient(135deg,#0ea5e9,#22d3ee)" }}>🧠</span>
              <div className="flex-1">
                <p className="text-sm font-medium">负责人正在分析目标并拆解任务…</p>
                <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  {active.leader_name} 正在评估需要哪些人才、如何分工、统一交付规则。通常需 10-30 秒，看板将自动更新。
                </p>
              </div>
              <span className="w-2 h-2 rounded-full bg-sky-400 pulse-dot" />
            </div>
          )}
          {active.status === "failed" && (() => {
            const err = typeof active.analysis?.error === "string" ? active.analysis.error : "";
            return (
              <div className="card card-static p-5 space-y-2" style={{ border: "1px solid rgba(239,68,68,0.45)" }}>
                <p className="text-sm font-medium text-red-300">❌ 作战分析失败</p>
                <p className="text-xs" style={{ color: "var(--muted)" }}>{err || "请重试"}</p>
                <button onClick={() => setShowKickoff(true)} className="btn-primary !py-1.5 !px-4 text-xs">
                  🔄 重新下达目标
                </button>
              </div>
            );
          })()}

          {/* 负责人 / 参战成员 / 总进度 */}
          {team && (
            <div className="card card-static p-5 space-y-3">
              <div className="flex items-center gap-3 flex-wrap">
                <h3 className="font-semibold text-sm flex items-center gap-2">
                  <span className="w-7 h-7 rounded-lg flex items-center justify-center text-sm"
                    style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>🧭</span>
                  指挥官与参战团队
                </h3>
                <span className="text-xs px-2.5 py-1 rounded-full bg-amber-500/15 text-amber-300">
                  🧭 负责人：{team.leader?.name}{team.leader?.role ? ` · ${team.leader.role}` : ""}
                </span>
                <span className="text-[11px] ml-auto" style={{ color: "var(--muted)" }}>
                  统一交付：{team.deliverable_format || "-"} · {team.deliverable_rule || ""}
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2.5">
                {team.members.map((m: any) => (
                  <div key={m.agent_id} className="rounded-xl p-3 space-y-1.5"
                    style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                    <div className="flex items-center gap-2">
                      <span className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs font-bold shrink-0"
                        style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
                        {(m.name || "?").slice(0, 1)}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-medium truncate">{m.name}</p>
                        <p className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{m.role || "未设岗位"}</p>
                      </div>
                    </div>
                    <div className="flex items-center justify-between text-[10px]" style={{ color: "var(--muted)" }}>
                      <span>任务 {m.task_count} 项</span>
                      <span className="text-emerald-300">✓ {m.approved} 通过</span>
                    </div>
                    <div className="h-1 rounded-full overflow-hidden bg-white/[0.06]">
                      <div className="h-full rounded-full transition-all duration-500"
                        style={{ width: `${m.progress_pct}%`, background: "linear-gradient(90deg,#10b981,#22d3ee)" }} />
                    </div>
                  </div>
                ))}
              </div>
              {/* 总进度 */}
              <div className="pt-2 border-t" style={{ borderColor: "var(--border)" }}>
                <div className="flex items-center gap-3 text-[11px] flex-wrap" style={{ color: "var(--muted)" }}>
                  <span>总体进度：</span>
                  <span>宣告 {team.overall_progress.announced}/{team.overall_progress.total_tasks}</span>
                  <span>确认 {team.overall_progress.acknowledged}/{team.overall_progress.total_tasks}</span>
                  <span>提交 {team.overall_progress.submitted}/{team.overall_progress.total_tasks}</span>
                  <span className="text-emerald-300 font-medium">审核通过 {team.overall_progress.approved}/{team.overall_progress.total_tasks} · {team.overall_progress.pct}%</span>
                </div>
                <div className="mt-1.5 h-1.5 rounded-full overflow-hidden bg-white/[0.06]">
                  <div className="h-full rounded-full transition-all duration-500"
                    style={{ width: `${team.overall_progress.pct}%`, background: "linear-gradient(90deg,#10b981,#22d3ee)" }} />
                </div>
              </div>
            </div>
          )}

          {/* 流程进度条 */}
          <div className="card card-static">
            <div className="flex items-center gap-1 flex-wrap">
              {STATUS_FLOW.map(([k, label], i) => (
                <div key={k} className="flex items-center gap-1">
                  <span className={`px-3 py-1.5 rounded-full text-[11px] font-medium ${
                    i <= stageIdx ? "bg-indigo-500/20 text-indigo-200" : "bg-white/[0.04] text-[var(--muted)]"}`}>
                    {i <= stageIdx ? "●" : "○"} {label}
                  </span>
                  {i < STATUS_FLOW.length - 1 && (
                    <span className="w-4 h-px" style={{ background: "var(--border)" }} />
                  )}
                </div>
              ))}
              <span className="ml-auto text-[11px]" style={{ color: "var(--muted)" }}>
                统一交付：{active.deliverable_format || "-"} · {active.deliverable_rule || ""}
              </span>
            </div>
            {/* 负责人分析 */}
            {active.analysis?.analysis && (
              <div className="mt-3 pt-3 border-t text-xs leading-relaxed" style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
                <b style={{ color: "var(--foreground)" }}>📋 负责人分析：</b>{active.analysis.analysis}
              </div>
            )}
          </div>

          {/* 作战群聊 */}
          <div className="card card-static p-0 overflow-hidden flex flex-col" style={{ height: "480px" }}>
            <div className="px-5 py-3 border-b flex items-center gap-2"
              style={{ borderColor: "var(--border)" }}>
              <span className="w-7 h-7 rounded-lg flex items-center justify-center text-sm"
                style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>💬</span>
              <h3 className="font-semibold text-sm flex-1">作战群聊</h3>
              <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300">
                {new Set(chatMsgs.map((m) => m.agent_name || "老板")).size} 人参与
              </span>
            </div>
            <div ref={chatBoxRef} className="flex-1 overflow-y-auto p-4 space-y-2.5">
              {!chatMsgs.length && (
                <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
                  暂无群聊消息 · 成员宣告与交流将实时出现在这里
                </p>
              )}
              {chatMsgs.map((m, i) => {
                const isBoss = m.role === "boss";
                return (
                  <div key={m.id || i} className={`flex ${isBoss ? "justify-end" : "justify-start"}`}>
                    <div className="max-w-[80%] rounded-2xl px-3.5 py-2.5 text-xs whitespace-pre-wrap leading-relaxed"
                      style={isBoss
                        ? { background: "linear-gradient(135deg,#f59e0b,#ef4444)", color: "#fff" }
                        : { background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--foreground)" }}>
                      {!isBoss && (
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="w-5 h-5 rounded-full inline-flex items-center justify-center text-[10px]"
                            style={{ background: "rgba(99,102,241,0.25)" }}>🤖</span>
                          <span className="font-semibold text-[11px] text-indigo-300">{m.agent_name}</span>
                        </div>
                      )}
                      {isBoss && <div className="font-semibold text-[11px] mb-0.5 opacity-90">👑 老板</div>}
                      {m.text}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="px-4 py-3 border-t flex items-center gap-2" style={{ borderColor: "var(--border)" }}>
              <input value={sayText} onChange={(e) => setSayText(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && bossSay()}
                placeholder="👑 以老板身份插入发言，全员可见…"
                className="input flex-1 !py-2" />
              <button onClick={bossSay} disabled={!sayText.trim()}
                className="btn-primary !px-4 !py-2 text-xs disabled:opacity-40 shrink-0">发送</button>
            </div>
          </div>

          {/* 任务列表（含宣告/确认/产出/审核全链路） */}
          <div className="card card-static p-5">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
              <h3 className="font-semibold text-sm">📋 任务分工与执行看板</h3>
              <div className="flex gap-2">
                {active.status === "announcing" && (
                  <>
                    {!active.tasks.every(t => t.announce_status === "announced") && (
                      <button onClick={announce} disabled={busy}
                        className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40">
                        📣 让全员在群聊复述任务
                      </button>
                    )}
                    {active.tasks.every(t => t.announce_status === "acknowledged") && (
                      <button onClick={startExec}
                        className="btn-primary !py-1.5 !px-4 text-xs bg-gradient-to-r from-emerald-600 to-teal-500">
                        🚀 正确，请继续 —— 全员开工
                      </button>
                    )}
                  </>
                )}
                {active.status === "executing" && active.tasks.some(t => t.status === "approved") &&
                 active.tasks.filter(t => t.agent_id !== active.leader_agent_id).every(t => t.status === "approved") && (
                  <button onClick={finalize} disabled={busy}
                    className="btn-primary !py-1.5 !px-4 text-xs">
                    🏁 整合最终交付物
                  </button>
                )}
              </div>
            </div>

            <div className="space-y-2.5">
              {active.tasks.map((t) => (
                <div key={t.id} className="rounded-xl p-3.5 space-y-2"
                  style={{
                    background: t.status === "approved" ? "rgba(16,185,129,0.05)" : "var(--surface-2)",
                    border: t.reviewed_by === "boss" ? "1px solid rgba(245,158,11,0.4)" : "1px solid var(--border)",
                  }}>
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span className={`w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold shrink-0 ${
                      t.seq <= 9 ? "bg-gradient-to-br from-indigo-500 to-violet-500 text-white" : "bg-white/[0.08]"}`}>
                      {t.seq}
                    </span>
                    <span className="text-sm font-medium">{t.title}</span>
                    <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-300">{t.agent_name}</span>
                    <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${(ANN_ZH[t.announce_status] || ANN_ZH.pending).cls}`}>
                      {(ANN_ZH[t.announce_status] || ANN_ZH.pending).label}
                    </span>
                    <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ml-auto ${(TASK_ZH[t.status] || TASK_ZH.todo).cls}`}>
                      {(TASK_ZH[t.status] || TASK_ZH.todo).label}
                    </span>
                  </div>

                  {/* 群聊复述原文 */}
                  {t.announce_text && (
                    <div className="rounded-lg px-3 py-2 text-xs leading-relaxed"
                      style={{ background: "rgba(99,102,241,0.06)", borderLeft: "2px solid #818cf8" }}>
                      💬 <b>{t.agent_name}：</b>{t.announce_text}
                    </div>
                  )}

                  {/* 负责人反馈 */}
                  {t.leader_feedback && (
                    <div className="rounded-lg px-3 py-2 text-xs"
                      style={{
                        background: t.announce_status === "corrected" ? "rgba(239,68,68,0.08)" : "rgba(16,185,129,0.08)",
                        borderLeft: t.announce_status === "corrected" ? "2px solid #f87171" : "2px solid #34d399",
                      }}>
                      🧭 <b>负责人：</b>{t.leader_feedback}
                    </div>
                  )}

                  {/* 校对操作 */}
                  {active.status === "announcing" && t.announce_status === "announced" && (
                    <div className="flex gap-1.5 flex-wrap">
                      <button onClick={() => ack(t, true)}
                        className="px-3 py-1 rounded-lg text-[11px] font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">
                        ✓ 正确，请继续
                      </button>
                      <button onClick={() => { setRejectTask(t); setFeedback(""); }}
                        className="px-3 py-1 rounded-lg text-[11px] hover:bg-red-500/15 text-red-300 transition-colors"
                        style={{ border: "1px solid rgba(248,113,113,0.35)" }}>
                        ✕ 有误，提出纠正
                      </button>
                    </div>
                  )}

                  {/* 产出物 + 审核 */}
                  {t.deliverable_name && (
                    <div className="flex items-center gap-2 flex-wrap pt-1">
                      <button onClick={() => openFile(active.id, t.deliverable_name)}
                        className="text-[11px] px-2.5 py-1 rounded-lg inline-flex items-center gap-1 hover:bg-indigo-500/15 hover:text-indigo-300 transition-colors cursor-pointer"
                        style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}
                        title="点击预览">
                        📄 {t.deliverable_name} 👁
                      </button>
                      {t.status === "submitted" && (
                        <button onClick={() => { setReviewTask(t); setReviewComment(""); }}
                          className="px-3 py-1 rounded-lg text-[11px] font-medium bg-amber-600 hover:bg-amber-500 text-white transition-colors">
                          👁 审核产出物
                        </button>
                      )}
                      {t.review_comment && (
                        <span className="text-[11px]" style={{ color: t.status === "approved" ? "#6ee7b7" : "#fca5a5" }}>
                          {t.reviewed_by === "boss" ? "👑 老板" : "🧭 负责人"}：{t.review_comment}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* 最终产出物（文件卡片，可预览+下载） */}
          {active.final_output_path && (() => {
            const fname = active.final_output_path.split("/").pop();
            return (
              <div className="card card-static p-5 space-y-3"
                style={{ border: "1px solid rgba(16,185,129,0.45)" }}>
                <h3 className="font-semibold text-sm text-emerald-300">🏆 最终整合交付</h3>
                <div className="flex items-center gap-3 flex-wrap">
                  <span className="w-10 h-10 rounded-xl flex items-center justify-center text-xl shrink-0"
                    style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>📄</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium truncate">{fname}</p>
                    <p className="text-[10px]" style={{ color: "var(--muted)" }}>负责人整合的最终方案文档</p>
                  </div>
                  <button onClick={() => openFile(active.id, fname)}
                    className="btn-primary !py-2 !px-4 text-xs">👁 预览</button>
                  <a href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/sessions/${active.id}/file?company_id=${cid}&name=${encodeURIComponent(fname || "")}`}
                    download className="btn-ghost text-xs"
                    style={{ border: "1px solid rgba(16,185,129,0.35)", color: "#6ee7b7" }}>⬇ 下载</a>
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {/* ===== 发起作战弹窗 ===== */}
      {mounted && showKickoff && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setShowKickoff(false)}>
          <div className="w-full max-w-lg rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-bold text-base">🎯 下达作战目标</h3>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              用一句话告诉项目负责人你的目标，TA 将自动分析需要哪些人才、拆解分工、制定统一交付规则
            </p>
            <textarea rows={3} value={goal} onChange={(e) => setGoal(e.target.value)}
              placeholder="例：我现在需要宣传泰山扇子崖的旅游计划，帮我制定宣传方案并落地"
              className="input resize-none" />
            <div className="grid grid-cols-2 gap-2.5">
              <select value={leaderId} onChange={(e) => setLeaderId(e.target.value)} className="input">
                <option value="">— 选择项目负责人 —</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <select value={fmt} onChange={(e) => setFmt(e.target.value)} className="input">
                <option value="md">统一交付：Markdown</option>
                <option value="word">统一交付：Word 文档</option>
                <option value="ppt">统一交付：PPT</option>
                <option value="excel">统一交付：Excel</option>
              </select>
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={() => setShowKickoff(false)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={kickoff} disabled={!goal.trim() || !leaderId || busy}
                className="btn-primary disabled:opacity-40">
                {busy ? "负责人分析中…" : "🚀 开始分析拆解"}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* ===== 纠正反馈弹窗 ===== */}
      {mounted && rejectTask && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setRejectTask(null)}>
          <div className="w-full max-w-md rounded-2xl p-5 space-y-3 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.4)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-sm text-red-300">✕ 指出「{rejectTask.agent_name}」的任务问题</h3>
            <textarea rows={3} value={feedback} onChange={(e) => setFeedback(e.target.value)}
              placeholder="哪里不对、应该怎么改…（必填）" className="input resize-none" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setRejectTask(null)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={() => feedback.trim() && ack(rejectTask, false, feedback.trim())}
                disabled={!feedback.trim()}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 disabled:opacity-40">
                提出纠正
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* ===== 产出物审核弹窗 ===== */}
      {mounted && reviewTask && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setReviewTask(null)}>
          <div className="w-full max-w-xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(245,158,11,0.4)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-sm">👁 审核「{reviewTask.title}」— {reviewTask.agent_name}</h3>
            <pre className="text-[11px] whitespace-pre-wrap rounded-xl p-3 max-h-72 overflow-y-auto"
              style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
              {reviewTask.deliverable_note || "（无预览）"}
            </pre>
            <textarea rows={2} value={reviewComment} onChange={(e) => setReviewComment(e.target.value)}
              placeholder="审核意见（驳回时建议填写修改要求）" className="input resize-none" />
            <div className="flex items-center gap-2 justify-end flex-wrap">
              <span className="text-[10px] mr-auto" style={{ color: "var(--muted)" }}>
                以身份审核：
              </span>
              <button onClick={() => submitReview(reviewTask, true, reviewComment || "通过", "boss")}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-amber-600 to-yellow-500 hover:opacity-90 transition-all">
                👑 老板批准
              </button>
              <button onClick={() => submitReview(reviewTask, false, reviewComment || "需修改", "boss")}
                className="px-4 py-2 rounded-xl text-xs text-red-300 hover:bg-red-500/15"
                style={{ border: "1px solid rgba(248,113,113,0.4)" }}>
                👑 老板打回
              </button>
              <button onClick={() => submitReview(reviewTask, true, reviewComment || "通过", "leader")}
                className="btn-primary text-xs">🧭 负责人批准</button>
              <button onClick={() => submitReview(reviewTask, false, reviewComment || "需修改", "leader")}
                className="px-4 py-2 rounded-xl text-xs text-red-300 hover:bg-red-500/15"
                style={{ border: "1px solid rgba(248,113,113,0.4)" }}>🧭 打回重做</button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 文件预览弹窗 */}
      {mounted && previewFile && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setPreviewFile(null)}>
          <div className="w-full max-w-3xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 pb-3 border-b" style={{ borderColor: "var(--border)" }}>
              <span className="w-9 h-9 rounded-xl flex items-center justify-center text-lg shrink-0"
                style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>📄</span>
              <h3 className="font-semibold text-sm flex-1 truncate">{previewFile.name}</h3>
              <a href={`${previewFile.url}`} download
                className="btn-ghost text-xs"
                style={{ border: "1px solid rgba(16,185,129,0.35)", color: "#6ee7b7" }}>⬇ 下载</a>
              <button onClick={() => setPreviewFile(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
            </div>
            {previewFile.content !== undefined ? (
              <pre className="text-[11px] whitespace-pre-wrap rounded-xl p-4 max-h-[65vh] overflow-y-auto leading-relaxed"
                style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                {previewFile.content}
              </pre>
            ) : (
              <div className="text-center py-12 space-y-3">
                <p className="text-4xl">📦</p>
                <p className="text-sm" style={{ color: "var(--muted)" }}>
                  该文件格式（{previewFile.name.split(".").pop()}）暂不支持在线预览
                </p>
                <a href={previewFile.url} download className="btn-primary !py-2 !px-5 text-xs inline-block">
                  ⬇ 下载查看
                </a>
              </div>
            )}
          </div>
        </div>, document.body
      )}
    </div>
  );
}
