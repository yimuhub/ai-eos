"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";

/* 作战室总览：跨项目全部作战会话 + 右上角直接下达作战任务（自动建项目） */

type Sess = {
  id: string; goal: string; status: string;
  leader_name?: string; project_id?: string | null;
  analysis?: any;
  tasks: { id: string; status: string; announce_status: string; agent_name: string; title: string }[];
};

const STATUS_ZH: Record<string, { label: string; cls: string }> = {
  analyzing: { label: "🧠 负责人分析中", cls: "text-sky-300" },
  announcing: { label: "📣 分工宣告中", cls: "text-indigo-300" },
  confirmed: { label: "✓ 已确认待开工", cls: "text-violet-300" },
  executing: { label: "⚡ 并行执行中", cls: "text-cyan-300" },
  delivering: { label: "📦 汇总交付中", cls: "text-amber-300" },
  review: { label: "👁 审核中", cls: "text-amber-300" },
  done: { label: "🏁 已完成", cls: "text-emerald-300" },
  failed: { label: "❌ 失败", cls: "text-red-300" },
};

export default function WarRoomHubPage() {
  const router = useRouter();
  const [mounted, setMounted] = useState(false);
  const [cid, setCid] = useState("");
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [agents, setAgents] = useState<{ id: string; name: string; role?: string | null }[]>([]);
  const [msg, setMsg] = useState("");
  // 直接下达
  const [showKickoff, setShowKickoff] = useState(false);
  const [goal, setGoal] = useState("");
  const [projName, setProjName] = useState("");
  const [leaderId, setLeaderId] = useState("");
  const [fmt, setFmt] = useState("md");
  const [busy, setBusy] = useState(false);
  // 批量选择与删除
  const [batchSel, setBatchSel] = useState<Set<string>>(new Set());
  const [showBatchDelete, setShowBatchDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // 单卡操作确认（删除/停止）
  const [confirmOp, setConfirmOp] = useState<{ sid: string; name: string; action: "delete" | "stop" } | null>(null);

  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    try {
      const d = await api.get(`/war-room/sessions?company_id=${cs[0].id}`);
      setSessions(d.sessions || []);
    } catch {}
    setAgents(await api.get(`/agents?company_id=${cs[0].id}`));
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!sessions.some((s) => ["analyzing", "announcing", "executing"].includes(s.status))) return;
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, [sessions, load]);

  async function lifecycle(sid: string, action: "pause" | "resume" | "stop" | "delete") {
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/sessions/${sid}/lifecycle?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ action }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setMsg(action === "delete" ? "🗑 已彻底删除" :
             action === "pause" ? "⏸ 已暂停（现场保留）" :
             action === "resume" ? "▶ 已恢复" : "⏹ 已停止");
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  async function batchDelete() {
    if (!batchSel.size) return;
    setDeleting(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/sessions/batch-delete?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ session_ids: Array.from(batchSel) }),
      });
      const d = await r.json();
      setMsg(`🗑 已彻底删除 ${d.deleted_count} 场作战${d.failed?.length ? `，失败 ${d.failed.length}` : ""}`);
      setShowBatchDelete(false); setBatchSel(new Set());
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setDeleting(false);
  }

  function toggleSel(sid: string) {
    setBatchSel((prev) => {
      const n = new Set(prev); if (n.has(sid)) n.delete(sid); else n.add(sid); return n;
    });
  }

  async function kickoff() {
    if (!goal.trim() || !leaderId) return;
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/war-room/kickoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ company_id: cid, goal: goal.trim(),
          leader_agent_id: leaderId, project_name: projName.trim() || undefined,
          deliverable_format: fmt }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      setShowKickoff(false); setGoal(""); setProjName("");
      setMsg("🧠 目标已下达，新项目已自动创建，负责人正在分析拆解…");
      // 直接跳进该作战的详情页看实时进展
      if (d.project_id) router.push(`/projects/${d.project_id}/warroom`);
      else await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setBusy(false);
  }

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
          <div className="ml-auto flex items-center gap-2">
            <button onClick={() => { setShowBatchDelete(true); setBatchSel(new Set()); }}
              className="btn-ghost">🗑 批量删除</button>
            <button onClick={() => setShowKickoff(true)} className="btn-primary">
              🎯 下达作战任务
            </button>
          </div>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg}
        </div>
      )}

      {/* 会话卡片：全部可点击进入详情 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {sessions.map((s) => {
          const st = STATUS_ZH[s.status] || STATUS_ZH.analyzing;
          const approved = s.tasks.filter((t) => t.status === "approved").length;
          const announced = s.tasks.filter((t) => t.announce_status !== "pending").length;
          const href = s.project_id ? `/projects/${s.project_id}/warroom` : `/warroom/${s.id}`;
          const pct = s.tasks.length ? Math.round(approved / s.tasks.length * 100) : 0;
          const canPause = ["announcing", "confirmed", "executing"].includes(s.status);
          const canResume = s.status === "paused";
          const canStop = !["done", "stopped"].includes(s.status);
          return (
            <Link key={s.id} href={href} className="card group relative overflow-hidden block transition-all">
              <div className="absolute top-0 left-0 w-full h-[2px] opacity-70 group-hover:opacity-100 transition-opacity"
                style={{ background: "linear-gradient(90deg,#6366f1,transparent)" }} />
              <div className="flex items-start gap-3">
                <span className="w-9 h-9 rounded-xl flex items-center justify-center text-base shrink-0"
                  style={{
                    background: s.status === "failed" ? "rgba(239,68,68,0.15)" : "linear-gradient(135deg,#6366f1,#8b5cf6)",
                  }}>{s.status === "failed" ? "❌" : s.status === "done" ? "🏆" : "🎯"}</span>
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-sm leading-snug line-clamp-2 group-hover:text-indigo-300 transition-colors">{s.goal}</p>
                  <p className="text-[10px] mt-1 flex items-center gap-1 flex-wrap" style={{ color: "var(--muted)" }}>
                    <span>🧭 负责人：<b style={{ color: "var(--foreground)" }}>{s.leader_name || "?"}</b></span>
                    <span>· 👥 参与 {new Set(s.tasks.map(t => t.agent_name)).size} Agent</span>
                    <span>· 📋 {s.tasks.length} 项任务</span>
                  </p>
                </div>
              </div>

              {/* 成员进度条 */}
              {s.tasks.length > 0 && (
                <div className="mt-2.5 space-y-1">
                  {s.tasks.slice(0, 3).map((t) => (
                    <div key={t.id} className="flex items-center gap-1.5 text-[10px]" style={{ color: "var(--muted)" }}>
                      <span className={`w-1 h-1 rounded-full shrink-0 ${
                        t.status === "approved" ? "bg-emerald-400" :
                        t.status === "submitted" ? "bg-amber-400" :
                        t.status === "running" ? "bg-cyan-400 pulse-dot" : "bg-white/30"}`} />
                      <span className="truncate">{t.agent_name} · {t.title}</span>
                    </div>
                  ))}
                  {s.tasks.length > 3 && (
                    <p className="text-[10px]" style={{ color: "var(--muted)" }}>… 共 {s.tasks.length} 项</p>
                  )}
                  {/* 总进度条 */}
                  <div className="pt-1">
                    <div className="h-1 rounded-full overflow-hidden bg-white/[0.06]">
                      <div className="h-full rounded-full transition-all duration-500"
                        style={{ width: `${pct}%`, background: "linear-gradient(90deg,#10b981,#22d3ee)" }} />
                    </div>
                    <p className="text-[9px] mt-0.5" style={{ color: "var(--muted)" }}>
                      审核通过 {approved}/{s.tasks.length} · {pct}%
                    </p>
                  </div>
                </div>
              )}

              <div className="mt-3 flex items-center justify-between gap-1 flex-wrap">
                <span className={`text-[11px] px-2.5 py-1 rounded-full ${st.cls}`}
                  style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}>
                  {st.label}{s.status === "analyzing" && <span className="inline-block w-1 h-1 rounded-full bg-sky-400 pulse-dot ml-1" />}
                </span>
              </div>

              {/* 悬停生命周期操作条（与项目中心一致：平时隐藏，挪上去显示） */}
              <div className="mt-auto pt-3 mt-3 border-t flex gap-1.5 opacity-0 group-hover:opacity-100 transition-all duration-200"
                style={{ borderColor: "var(--border)" }} onClick={(e) => e.stopPropagation()}>
                {canResume ? (
                  <button onClick={() => lifecycle(s.id, "resume")}
                    className="flex-1 text-[11px] py-1.5 rounded-lg hover:bg-emerald-500/12 text-emerald-300 transition-colors"
                    style={{ border: "1px solid rgba(16,185,129,0.3)" }}>▶ 恢复</button>
                ) : (
                  <button onClick={() => canPause && lifecycle(s.id, "pause")} disabled={!canPause}
                    className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                      canPause ? "hover:bg-amber-500/12 text-amber-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                    style={{ border: "1px solid var(--border)" }}>⏸ 暂停</button>
                )}
                <button onClick={() => canStop && setConfirmOp({ sid: s.id, name: s.goal.slice(0, 18), action: "stop" })} disabled={!canStop}
                  className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                    canStop ? "hover:bg-orange-500/12 text-orange-300" : "opacity-30 cursor-not-allowed text-[var(--muted)]"}`}
                  style={{ border: "1px solid var(--border)" }}>⏹ 停止</button>
                <button onClick={() => setConfirmOp({ sid: s.id, name: s.goal.slice(0, 18), action: "delete" })}
                  className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                  style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑 删除</button>
              </div>
            </Link>
          );
        })}
      </div>

      {!sessions.length && (
        <div className="card card-static text-center py-16 space-y-2">
          <p className="text-sm">还没有作战会话</p>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            点击右上角「🎯 下达作战任务」发起第一场作战，将自动创建新项目并组队开工
          </p>
        </div>
      )}

      {/* 批量删除确认弹窗 */}
      {mounted && showBatchDelete && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setShowBatchDelete(false)}>
          <div className="w-full max-w-lg rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.45)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2.5">
              <h3 className="font-bold text-sm text-red-300 flex-1">🗑 批量删除作战会话</h3>
              {batchSel.size > 0 && (
                <span className="text-[11px] px-2 py-0.5 rounded-full bg-red-500/15 text-red-300">已选 {batchSel.size}</span>
              )}
              <button onClick={() => setShowBatchDelete(false)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              将彻底删除选中的作战会话及其全部任务、宣告记录与产出物引用，不可恢复！
            </p>
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {/* 全选 */}
              <button onClick={() => setBatchSel(batchSel.size === sessions.length ? new Set() : new Set(sessions.map((x) => x.id)))}
                className="w-full px-3 py-2 rounded-xl text-xs text-left hover:bg-white/[0.05]"
                style={{ border: "1px solid var(--border)" }}>
                {batchSel.size === sessions.length ? "☑" : "☐"} 全选（{sessions.length} 场）
              </button>
              {sessions.map((x) => {
                const on = batchSel.has(x.id);
                return (
                  <button key={x.id} onClick={() => toggleSel(x.id)}
                    className={`w-full px-3 py-2 rounded-xl text-xs text-left transition-all ${on ? "bg-red-500/10 border border-red-500/40" : "hover:bg-white/[0.04]"}`}
                    style={!on ? { border: "1px solid var(--border)" } : {}}>
                    {on ? "✓" : "☐"} {x.goal.slice(0, 30)}…
                    <span className="ml-2 opacity-60">{STATUS_ZH[x.status]?.label || x.status}</span>
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-2 pt-1">
              <span className="text-[10px] ml-auto" style={{ color: "var(--muted)" }}>⚠️ 删除不可恢复</span>
            </div>
            <div className="flex justify-end">
              <button onClick={batchDelete} disabled={!batchSel.size || deleting}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90 disabled:opacity-40 transition-all">
                {deleting ? "删除中…" : `红橙渐变确认：彻底删除 ${batchSel.size} 场`}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 单卡 停止/删除 确认弹窗 */}
      {mounted && confirmOp && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setConfirmOp(null)}>
          <div className="w-full max-w-sm rounded-2xl p-5 space-y-4 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: confirmOp.action === "delete" ? "1px solid rgba(239,68,68,0.45)" : "1px solid rgba(245,158,11,0.4)" }}
            onClick={(e) => e.stopPropagation()}>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">
              {confirmOp.action === "delete"
                ? `⚠️ 彻底删除作战「${confirmOp.name}…」？\n全部任务、宣告记录、产出物引用将清除，不可恢复！`
                : `⏹ 停止作战「${confirmOp.name}…」？\n未完成任务将被取消，已有成果保留。`}
            </p>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmOp(null)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={() => { lifecycle(confirmOp.sid, confirmOp.action); setConfirmOp(null); }}
                className={`px-4 py-2 rounded-xl text-xs font-medium text-white transition-all ${
                  confirmOp.action === "delete"
                    ? "bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90"
                    : "bg-gradient-to-r from-orange-600 to-amber-500 hover:opacity-90"}`}>
                {confirmOp.action === "delete" ? "确认彻底删除" : "确认停止"}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* ===== 下达作战任务弹窗 ===== */}
      {mounted && showKickoff && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setShowKickoff(false)}>
          <div className="w-full max-w-lg rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-bold text-base">🎯 下达作战任务</h3>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              正式下达后将<b>自动创建新项目</b>（出现在项目列表），负责人随即开始分析组队
            </p>
            <textarea rows={3} value={goal} onChange={(e) => setGoal(e.target.value)}
              placeholder="作战目标 *&#10;例：我现在需要宣传泰山扇子崖的旅游计划，帮我制定宣传方案并落地"
              className="input resize-none" />
            <input value={projName} onChange={(e) => setProjName(e.target.value)}
              placeholder="项目名称（可选，默认取目标前60字）" className="input" />
            <div className="grid grid-cols-2 gap-2.5">
              <select value={leaderId} onChange={(e) => setLeaderId(e.target.value)} className="input">
                <option value="">— 项目负责人 —</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.role ? ` · ${a.role}` : ""}</option>)}
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
                {busy ? "下达中…" : "🚀 正式下达并创建项目"}
              </button>
            </div>
          </div>
        </div>, document.body
      )}
    </div>
  );
}
