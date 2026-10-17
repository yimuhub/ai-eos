"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError, getActiveCompanyId } from "@/lib/api";
import AttachmentPicker from "@/components/AttachmentPicker";
import type { Attachment } from "@/components/AttachmentPicker";

type Plan = {
  path: "DIRECT" | "EXPERT" | "PROJECT";
  reason: string;
  targets: { agent_id: string; agent_name: string; task: string }[];
  team_goal: string;
  est_minutes: number;
  notes: string;
  phrase: string;
};
type ExecResult = {
  path: string;
  jobs?: any[];
  session_id?: string;
  project_id?: string;
  team_size?: number;
};

const PATH_META: Record<string, { label: string; icon: string; desc: string }> = {
  DIRECT: { label: "直接派活", icon: "⚡", desc: "交给现有员工，立刻开工" },
  EXPERT: { label: "专家组队", icon: "📚", desc: "从专家库挑人，蓝图进工厂逐个审查" },
  PROJECT: { label: "作战室立项", icon: "🎯", desc: "负责人拆解全流程，跟踪到交付" },
};

export default function CommandDeckPage() {
  const [phrase, setPhrase] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [planning, setPlanning] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<ExecResult | null>(null);
  const [msg, setMsg] = useState("");
  const [webhook, setWebhook] = useState("");
  const [notifyBusy, setNotifyBusy] = useState(false);
  const [polling, setPolling] = useState(false);
  const timerRef = useRef<any>(null);

  useEffect(() => () => { if (timerRef.current) clearInterval(timerRef.current); }, []);

  const doPlan = useCallback(async () => {
    if (!phrase.trim()) return;
    setPlanning(true); setMsg(""); setResult(null); setPlan(null);
    try {
      const p = await api.post("/command/plan",
        { phrase, attachments }) as Plan;
      setPlan(p);
      setConfirmOpen(true);
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setPlanning(false);
  }, [phrase, attachments]);

  const doExecute = async () => {
    if (!plan) return;
    setExecuting(true);
    try {
      const r = await api.post("/command/execute", { plan }) as ExecResult;
      setResult(r);
      setConfirmOpen(false);
      setMsg("✅ 已放行执行：" + PATH_META[plan.path]?.label +
        "。老板的签字点都在各确认面板里。");
      // 轮询执行状态
      startPolling(r);
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setExecuting(false);
  };

  const startPolling = (r: ExecResult) => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (r.path === "DIRECT" && r.jobs?.length) {
      setPolling(true);
      const ids = r.jobs.map((j: any) => j.task_id || j.id).filter(Boolean);
      const tick = async () => {
        try {
          const rows = await Promise.all(
            ids.map((id: string) => api.get(`/tasks/${id}`).catch(() => null)));
          const live = rows.filter(Boolean);
          if (live.every((t: any) => ["COMPLETED", "FAILED", "CANCELLED"].includes(t.status))) {
            clearInterval(timerRef.current); setPolling(false);
          }
        } catch { /* keep polling */ }
      };
      tick();
      timerRef.current = setInterval(tick, 8000);
    } else if (r.path === "PROJECT" && r.session_id) {
      setPolling(true);
      const tick = async () => {
        try {
          const s = await api.get(`/war-room/${r.session_id}`) as any;
          if (["done", "failed", "stopped", "archived"].includes(s.status)) {
            clearInterval(timerRef.current); setPolling(false);
          }
        } catch { /* keep polling */ }
      };
      tick();
      timerRef.current = setInterval(tick, 8000);
    }
  };

  const openReport = async (ref: string) => {
    const r = await api.get(`/command/report/${ref}?as_json=1`) as any;
    const w = window.open("", "_blank");
    if (w) w.document.write(r.html);
  };

  const pushNotify = async () => {
    if (!webhook || !result) return;
    setNotifyBusy(true);
    try {
      let ref = "";
      if (result.path === "DIRECT" && result.jobs?.[0]) ref = result.jobs[0].task_id || result.jobs[0].id || "";
      else if (result.path === "PROJECT") ref = result.session_id || "";
      else if (result.path === "EXPERT" && result.jobs?.[0]) ref = result.jobs[0].job_id || "";
      const rep = ref ? await api.get(`/command/report/${ref}?as_json=1`) as any : null;
      const r = await api.post("/command/notify",
        { report: rep?.html || JSON.stringify(result), webhook }) as any;
      setMsg(r.ok ? "✅ 已推送到群（" + r.platform + "）" : "❌ 推送失败：" + JSON.stringify(r.detail));
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setNotifyBusy(false);
  };

  const refOf = (): string | null => {
    if (!result) return null;
    if (result.path === "DIRECT" && result.jobs?.[0]) {
      const j0 = result.jobs[0];
      return j0.tasks?.[0]?.task_id || j0.task_id || j0.id || null;
    }
    if (result.path === "PROJECT") return result.session_id || null;
    if (result.path === "EXPERT" && result.jobs?.[0]) return result.jobs[0].job_id || null;
    return null;
  };

  const jobLinks = useMemo(() => {
    if (!result) return [];
    const links: { label: string; href?: string; onClick?: () => void }[] = [];
    if (result.path === "DIRECT") {
      for (const j of result.jobs || []) {
        const id = j.task_id || j.id;
        if (id) links.push({ label: `⚡ 任务 ${String(id).slice(0, 8)}`, href: `/tasks/${id}` });
      }
    } else if (result.path === "EXPERT") {
      links.push({ label: `📚 ${result.team_size} 份专家蓝图已进工厂，逐个审查 →`, href: "/factory" });
    } else if (result.path === "PROJECT") {
      links.push({ label: "🎯 打开作战室实时看板 →", href: `/warroom/${result.session_id}` });
    }
    return links;
  }, [result]);

  return (
    <div className="p-6 max-w-[1100px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-1">📣 一句话指挥台</h1>
      <p className="text-sm text-[var(--muted)] mb-5">
        说一句话，AI 判断最合适的路：直接派活 / 专家组队 / 作战室立项 —— 每一步都有你的确认点
      </p>

      {/* 主输入区 */}
      <div className="card p-5 mb-5">
        <div className="flex items-start gap-2">
          <textarea
            value={phrase}
            onChange={e => setPhrase(e.target.value)}
            rows={2}
            placeholder='例："帮我出一份 Q4 市场推广方案" / "让小红运做一条新品种草内容" / "组建团队两周内交付竞品分析报告"'
            className="input-glow flex-1 resize-none"
            onKeyDown={e => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) doPlan();
            }} />
          <button onClick={doPlan} disabled={planning || !phrase.trim()}
            className="btn-primary self-stretch disabled:opacity-40">
            {planning ? "AI 判断中…" : "下达"}
          </button>
        </div>
        <div className="mt-3 flex items-center justify-between">
          <AttachmentPicker value={attachments} onChange={setAttachments}
            label="📎 附参考材料（可选）" />
          <span className="text-xs text-[var(--muted)]">⌘/Ctrl + Enter 快速下达</span>
        </div>
      </div>

      {msg && <div className="mb-4 text-sm text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-2">{msg}</div>}

      {/* 执行结果控制台 */}
      {result && (
        <div className="card p-4 mb-5 space-y-3">
          <div className="flex items-center gap-2 text-sm font-bold">
            <span className="pulse-dot bg-emerald-400" />
            已放行 · {PATH_META[result.path]?.label}
            {polling && <span className="text-xs text-[var(--muted)] font-normal">正在盯进度，完成后自动停</span>}
          </div>
          <div className="flex flex-wrap gap-2">
            {jobLinks.map((l, i) => l.href ? (
              <a key={i} href={l.href}
                className="px-3 py-1.5 rounded-lg text-xs bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 hover:bg-indigo-500/25 transition-all">
                {l.label}
              </a>
            ) : (
              <span key={i} className="px-3 py-1.5 rounded-lg text-xs text-[var(--muted)]">{l.label}</span>
            ))}
          </div>
          {refOf() && (
            <div className="flex gap-2 flex-wrap items-center">
              <button onClick={() => openReport(refOf()!)}
                className="px-3 py-1.5 rounded-lg text-xs border border-[var(--border)] hover:bg-white/[0.05] transition-all">
                📄 生成分享报告（单文件 HTML）
              </button>
              <input value={webhook} onChange={e => setWebhook(e.target.value)}
                placeholder="钉钉/飞书/企微 webhook（可选）"
                className="input-glow flex-1 min-w-[200px]" />
              <button onClick={pushNotify} disabled={notifyBusy || !webhook}
                className="px-3 py-1.5 rounded-lg text-xs bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 disabled:opacity-40 hover:bg-cyan-500/30">
                {notifyBusy ? "推送中…" : "📤 推送到群"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* 方案确认弹窗 */}
      {plan && confirmOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setConfirmOpen(false)} />
          <div className="relative w-full max-w-lg max-h-[90vh] overflow-y-auto bg-[var(--card)] backdrop-blur-xl border border-[var(--border)] rounded-2xl shadow-2xl p-6 animate-fade-up">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-11 h-11 rounded-xl bg-gradient-to-br from-indigo-500 to-cyan-500 flex items-center justify-center text-xl">
                {PATH_META[plan.path]?.icon}
              </div>
              <div>
                <div className="font-bold">
                  {PATH_META[plan.path]?.label}
                  <span className="text-xs text-[var(--muted)] ml-2">约 {plan.est_minutes} 分钟</span>
                </div>
                <div className="text-xs text-[var(--muted)]">{PATH_META[plan.path]?.desc}</div>
              </div>
            </div>

            <p className="text-sm text-[var(--muted)] mb-3">{plan.reason}</p>

            {plan.path === "DIRECT" ? (
              <div className="space-y-1.5 mb-4">
                <b className="text-xs text-[var(--muted)]">分工（可改任务描述）</b>
                {plan.targets.map((t, i) => (
                  <div key={i} className="flex gap-2 text-xs p-2 rounded-lg bg-white/[0.03] border border-[var(--border)]">
                    <span className="font-medium shrink-0">{t.agent_name}</span>
                    <span className="text-[var(--muted)]">{t.task}</span>
                  </div>
                ))}
              </div>
            ) : plan.path === "EXPERT" ? (
              <div className="mb-4 text-xs p-2 rounded-lg bg-white/[0.03] border border-[var(--border)]">
                组队目标：<b>{plan.team_goal}</b><br />
                <span className="text-[var(--muted)]">AI 将从专家库挑互补工种，蓝图进工厂后你逐个审查批准</span>
              </div>
            ) : (
              <div className="mb-4 text-xs p-2 rounded-lg bg-white/[0.03] border border-[var(--border)]">
                作战室目标：<b>{plan.team_goal}</b><br />
                <span className="text-[var(--muted)]">负责人会自动拆解分工并拉团队，交付前每个签字点都会等你</span>
              </div>
            )}
            {plan.notes && <p className="text-xs text-amber-300/80 mb-4">⚠ {plan.notes}</p>}

            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmOpen(false)}
                className="px-4 py-2 rounded-lg text-sm border border-[var(--border)] text-[var(--muted)] hover:text-white transition-all">
                重新想
              </button>
              <button onClick={doExecute} disabled={executing}
                className="btn-primary disabled:opacity-50">
                {executing ? "放行中…" : "👍 确认执行"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
