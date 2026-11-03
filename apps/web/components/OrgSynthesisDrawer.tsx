"use client";

// 「AI 生成组织」四步抽屉：资料盘点 → 分析 → 审阅 → 落地。
//
// 设计取舍：
// - 分析是一次同步请求（后端一次结构化生成），所以这里用阶段动画表达进度，
//   请求回来后展示真实的阶段耗时，不伪造 token/费用。
// - 审阅按「中心 → 部门 → 岗位」展示理由与资料依据；编制可调、岗位可移除，
//   但部门骨架保持不变，避免落地后出现"有岗位没部门"的半成品。
// - 落地与撤销都以后端 run 的快照为准；抽屉只负责把老板的选择发过去。

import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import type { ModelConfig } from "@/lib/types";

type Readiness = {
  score: number;
  level: "high" | "medium" | "low" | string;
  guidance: string;
  document_count: number;
  ready_count: number;
  pending_count: number;
  ready_chars: number;
  dimensions: {
    key: string; label: string; covered: boolean; documents: string[];
  }[];
  missing: { key: string; label: string; hint: string }[];
  documents: {
    id: string; title: string; category: string | null;
    source_type: string; status: string; chunk_count: number;
  }[];
  pending_documents: string[];
};

type DraftAgent = {
  name: string;
  role: string;
  department: string;
  center: string;
  headcount: number;
  is_leader: boolean;
  reports_to: string;
  reason: string;
  evidence: string[];
  system_prompt: string;
  state: "new" | "reuse";
  existing_headcount: number;
};

type Draft = {
  profile: Record<string, string>;
  centers: { name: string; description: string; state?: string }[];
  departments: {
    name: string; center: string; description: string;
    reason: string; state?: string;
  }[];
  agents: DraftAgent[];
  warnings: { level: string; title: string; detail: string }[];
  totals: {
    centers: number; departments: number; roles: number;
    headcount: number; new_headcount: number; reuse_headcount: number;
  };
  readiness?: Readiness;
};

type Run = {
  id: string;
  status: "ANALYZING" | "READY" | "APPLIED" | "ROLLED_BACK" | "FAILED";
  stage: string;
  progress: number;
  model_id: string | null;
  stages: { key: string; label: string; duration_ms: number; summary: string }[];
  draft: Draft;
  usage: {
    prompt_tokens?: number; completion_tokens?: number;
    total_tokens?: number; cost_estimate_cny?: number;
  };
  error: string | null;
  applied_at: string | null;
  snapshot: Record<string, unknown>;
  created_at: string;
  can_apply: boolean;
};

type ApplyResult = {
  run_id: string;
  created_centers: number;
  created_departments: number;
  created_agents: number;
  reused_departments: number;
  reused_headcount: number;
};

type Selection = { included: boolean; headcount: number };

const STAGE_LABELS = ["资料盘点", "公司画像", "组织设计", "交叉审查"];

function keyOf(agent: DraftAgent) {
  return `${agent.department}::${agent.name}`;
}

function statusText(run: Run) {
  return {
    ANALYZING: "分析中",
    READY: "待审阅",
    APPLIED: "已落地",
    ROLLED_BACK: "已撤销",
    FAILED: "失败",
  }[run.status] ?? run.status;
}

function levelStyle(level: string) {
  if (level === "high") {
    return { color: "#fca5a5", bg: "rgba(248,113,113,0.08)", border: "rgba(248,113,113,0.28)" };
  }
  if (level === "low") {
    return { color: "var(--muted)", bg: "rgba(255,255,255,0.03)", border: "var(--border)" };
  }
  return { color: "#fcd34d", bg: "rgba(251,191,36,0.08)", border: "rgba(251,191,36,0.28)" };
}

export default function OrgSynthesisDrawer({
  open, companyId, onClose, onApplied,
}: {
  open: boolean;
  companyId: string | null;
  onClose: () => void;
  onApplied: () => void;
}) {
  const router = useRouter();
  const [mounted, setMounted] = useState(false);
  const [phase, setPhase] = useState<"readiness" | "analyzing" | "review" | "applying" | "done">("readiness");
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [readinessBusy, setReadinessBusy] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [modelId, setModelId] = useState("");
  const [instructions, setInstructions] = useState("");
  const [selection, setSelection] = useState<Record<string, Selection>>({});
  const [error, setError] = useState("");
  const [stageIdx, setStageIdx] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [rollbackDone, setRollbackDone] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  const loadReadiness = useCallback(async () => {
    if (!companyId) return;
    setReadinessBusy(true);
    try {
      const r = await api.get<Readiness>(
        `/org-synthesis/readiness?company_id=${encodeURIComponent(companyId)}`);
      setReadiness(r);
    } catch (e) {
      setError(describeError(e, "读取资料覆盖度失败"));
    } finally {
      setReadinessBusy(false);
    }
  }, [companyId]);

  const loadModels = useCallback(async () => {
    try {
      const rows = await api.get<ModelConfig[]>("/models", { silent: true });
      const usable = (rows || []).filter(
        (m) => (m.model_type ?? "text") === "text" && m.enabled !== false);
      setModels(usable);
      setModelId((prev) => prev || usable[0]?.id || "");
    } catch {
      // 模型列表读不到不阻塞：后端仍会按系统默认对话模型解析。
    }
  }, []);

  const loadRuns = useCallback(async () => {
    if (!companyId) return;
    try {
      const rows = await api.get<Run[]>(
        `/org-synthesis/runs?company_id=${encodeURIComponent(companyId)}&limit=8`,
        { silent: true });
      setRuns(rows || []);
    } catch {
      // 历史分析失败不影响本次生成。
    }
  }, [companyId]);

  useEffect(() => {
    if (!open || !companyId) return;
    setPhase("readiness");
    setRun(null);
    setError("");
    setApplyResult(null);
    setRollbackDone(false);
    setInstructions("");
    setStageIdx(0);
    setElapsed(0);
    loadReadiness();
    loadModels();
    loadRuns();
  }, [open, companyId, loadReadiness, loadModels, loadRuns]);

  useEffect(() => {
    if (phase !== "analyzing") return;
    setElapsed(0);
    setStageIdx(0);
    const clock = window.setInterval(() => setElapsed((v) => v + 1), 1000);
    const steps = window.setInterval(
      () => setStageIdx((v) => Math.min(v + 1, STAGE_LABELS.length - 1)), 2300);
    return () => {
      window.clearInterval(clock);
      window.clearInterval(steps);
    };
  }, [phase]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && phase !== "analyzing" && phase !== "applying") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, phase, onClose]);

  function applyRunToSelection(source: Run) {
    const next: Record<string, Selection> = {};
    for (const agent of source.draft.agents || []) {
      next[keyOf(agent)] = { included: true, headcount: agent.headcount };
    }
    setSelection(next);
  }

  async function analyze() {
    if (!companyId || phase === "analyzing") return;
    setPhase("analyzing");
    setError("");
    setRun(null);
    try {
      const r = await api.post<Run>("/org-synthesis/analyze", {
        company_id: companyId,
        model_id: modelId || undefined,
        instructions: instructions.trim() || undefined,
      }, { silent: true, timeoutMs: 300_000 });
      setRun(r);
      applyRunToSelection(r);
      setPhase("review");
      loadRuns();
    } catch (e) {
      setError(describeError(e, "生成组织架构失败"));
      setPhase("readiness");
      loadRuns();
    }
  }

  async function openRun(id: string) {
    setBusy(true);
    setError("");
    try {
      const r = await api.get<Run>(`/org-synthesis/runs/${id}`);
      setRun(r);
      applyRunToSelection(r);
      setApplyResult(null);
      setRollbackDone(false);
      setPhase("review");
    } catch (e) {
      setError(describeError(e, "读取分析记录失败"));
    } finally {
      setBusy(false);
    }
  }

  const selectionOf = useCallback((agent: DraftAgent): Selection => {
    return selection[keyOf(agent)] ?? {
      included: true, headcount: agent.headcount,
    };
  }, [selection]);

  function updateSelection(agent: DraftAgent, patch: Partial<Selection>) {
    const key = keyOf(agent);
    setSelection((prev) => {
      const current = prev[key] ?? { included: true, headcount: agent.headcount };
      return { ...prev, [key]: { ...current, ...patch } };
    });
  }

  const includedAgents = useMemo(() => {
    if (!run) return [];
    return (run.draft.agents || []).filter((a) => selectionOf(a).included);
  }, [run, selectionOf]);

  const plannedHeadcount = useMemo(
    () => includedAgents.reduce((sum, a) => sum + selectionOf(a).headcount, 0),
    [includedAgents, selectionOf]);

  const plannedReuse = useMemo(
    () => includedAgents.reduce(
      (sum, a) => sum + Math.min(selectionOf(a).headcount, a.existing_headcount), 0),
    [includedAgents, selectionOf]);

  async function applyOrg() {
    if (!run || !includedAgents.length || busy) return;
    setPhase("applying");
    setError("");
    try {
      const result = await api.post<ApplyResult>(
        `/org-synthesis/runs/${run.id}/apply`,
        {
          model_id: run.model_id || modelId || undefined,
          agents: includedAgents.map((a) => ({
            name: a.name,
            department: a.department,
            headcount: selectionOf(a).headcount,
          })),
        },
        { silent: true, timeoutMs: 120_000 },
      );
      setApplyResult(result);
      setRun((prev) => prev ? { ...prev, status: "APPLIED", snapshot: result as unknown as Record<string, unknown> } : prev);
      setPhase("done");
      onApplied();
      loadRuns();
    } catch (e) {
      setError(describeError(e, "落地组织架构失败"));
      setPhase("review");
    }
  }

  async function rollback() {
    if (!run || busy) return;
    const ok = await confirmAction({
      title: "撤销这次落地？",
      description: "只删除本次生成的部门和员工；原有组织与员工不会受影响。",
      bullets: [
        "本次新建的部门与员工会被删除",
        "已有其他员工的部门会保留，不会连带删除",
      ],
      confirmText: "撤销落地",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    setError("");
    try {
      await api.post(`/org-synthesis/runs/${run.id}/rollback`, {}, { silent: true });
      setRun((prev) => prev ? { ...prev, status: "ROLLED_BACK" } : prev);
      setRollbackDone(true);
      onApplied();
      loadRuns();
    } catch (e) {
      setError(describeError(e, "撤销失败"));
    } finally {
      setBusy(false);
    }
  }

  if (!mounted || !open) return null;

  const draft = run?.draft;
  const warnings = draft?.warnings ?? [];
  const profile = draft?.profile ?? {};

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.72)", backdropFilter: "blur(6px)" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget && phase !== "analyzing" && phase !== "applying") onClose(); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="AI 生成组织架构"
        className="w-full max-w-[1080px] h-[88vh] rounded-2xl flex flex-col overflow-hidden animate-fade-up"
        style={{ background: "var(--surface)", border: "1px solid var(--border)", boxShadow: "0 30px 80px -20px rgba(0,0,0,0.8)" }}
      >
        {/* 头部 */}
        <div className="px-6 py-4 flex items-start gap-4 shrink-0"
          style={{ borderBottom: "1px solid var(--border)", background: "linear-gradient(120deg, rgba(99,102,241,0.10), transparent 70%)" }}>
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-semibold">AI 生成组织架构</h2>
            <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
              知识库资料 → 公司画像 → 组织设计 → 审查 → 一键落地；落地前所有岗位都能改
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {["readiness", "analyzing", "review", "done"].map((key, i) => {
              const order = { readiness: 0, analyzing: 1, review: 2, applying: 2, done: 3 }[phase];
              const active = i === order;
              const passed = i < order;
              return (
                <span key={key} className="flex items-center gap-2">
                  <span className="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-bold"
                    style={{
                      background: passed ? "rgba(52,211,153,0.16)" : active ? "linear-gradient(135deg,#6366f1,#8b5cf6)" : "rgba(255,255,255,0.04)",
                      border: passed ? "1px solid rgba(52,211,153,0.45)" : "1px solid var(--border)",
                      color: passed ? "var(--success)" : active ? "#fff" : "var(--muted)",
                    }}>
                    {passed ? "✓" : i + 1}
                  </span>
                  {i < 3 && <span className="w-5 h-px" style={{ background: "var(--border)" }} />}
                </span>
              );
            })}
            <button onClick={onClose} disabled={phase === "analyzing" || phase === "applying"}
              className="btn-ghost !py-1 !px-2.5 text-xs ml-1" aria-label="关闭">关闭</button>
          </div>
        </div>

        {error && (
          <div className="mx-6 mt-4 px-4 py-3 rounded-xl text-sm shrink-0"
            style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
            {error}
          </div>
        )}

        {/* 内容区 */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {phase === "readiness" && (
            <div className="space-y-5">
              <div className="grid md:grid-cols-[240px_1fr] gap-5">
                <div className="card p-5 flex flex-col items-center justify-center text-center">
                  <div className="relative w-32 h-32 rounded-full flex items-center justify-center"
                    style={{ background: `conic-gradient(#6366f1 ${(readiness?.score ?? 0) * 3.6}deg, rgba(255,255,255,0.06) 0)` }}>
                    <div className="w-[108px] h-[108px] rounded-full flex flex-col items-center justify-center"
                      style={{ background: "var(--surface-2)" }}>
                      <span className="text-3xl font-bold tabular-nums">{readiness?.score ?? "—"}</span>
                      <span className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>资料覆盖度</span>
                    </div>
                  </div>
                  <p className="text-xs mt-4 leading-relaxed" style={{ color: "var(--muted)" }}>
                    {readiness?.guidance || (readinessBusy ? "正在读取知识库…" : "还没有读取到资料")}
                  </p>
                  <p className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
                    {readiness ? `${readiness.ready_count}/${readiness.document_count} 份可读 · ${readiness.ready_chars} 字` : ""}
                  </p>
                </div>

                <div className="space-y-3">
                  <div className="grid sm:grid-cols-2 gap-3">
                    {(readiness?.dimensions ?? []).map((dim) => (
                      <div key={dim.key} className="rounded-xl p-3.5"
                        style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                        <div className="flex items-center gap-2">
                          <span className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold"
                            style={{
                              background: dim.covered ? "rgba(52,211,153,0.16)" : "rgba(251,191,36,0.14)",
                              color: dim.covered ? "var(--success)" : "#fcd34d",
                            }}>{dim.covered ? "✓" : "!"}</span>
                          <span className="text-sm font-medium">{dim.label}</span>
                          <span className="ml-auto text-[10px]" style={{ color: "var(--muted)" }}>
                            {dim.covered ? `${dim.documents.length} 份` : "缺失"}
                          </span>
                        </div>
                        <p className="text-[11px] mt-2 truncate" style={{ color: "var(--muted)" }}>
                          {dim.covered ? dim.documents.join("、") : "建议补充这类资料"}
                        </p>
                      </div>
                    ))}
                  </div>
                  {!!readiness?.pending_documents?.length && (
                    <p className="text-[11px]" style={{ color: "#fcd34d" }}>
                      待处理资料：{readiness.pending_documents.join("、")}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      onClick={() => { onClose(); router.push("/knowledge"); }}
                      className="btn-ghost !py-1.5 text-xs">
                      去知识库补充资料
                    </button>
                    <button onClick={loadReadiness} disabled={readinessBusy}
                      className="btn-ghost !py-1.5 text-xs">
                      {readinessBusy ? "刷新中…" : "刷新覆盖度"}
                    </button>
                  </div>
                </div>
              </div>

              {!!readiness?.missing?.length && (
                <div className="rounded-xl p-4"
                  style={{ background: "rgba(251,191,36,0.06)", border: "1px solid rgba(251,191,36,0.24)" }}>
                  <p className="text-sm font-medium" style={{ color: "#fcd34d" }}>资料缺口</p>
                  <ul className="mt-2 space-y-1.5">
                    {readiness.missing.map((m) => (
                      <li key={m.key} className="text-xs" style={{ color: "var(--muted)" }}>
                        <span style={{ color: "var(--foreground)" }}>{m.label}</span>：{m.hint}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="card p-4 space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <label className="text-xs font-medium">分析模型</label>
                  <select value={modelId} onChange={(e) => setModelId(e.target.value)}
                    className="input !py-1.5 !text-xs max-w-[320px]">
                    <option value="">系统默认对话模型</option>
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>{m.name} · {m.model_id}</option>
                    ))}
                  </select>
                  <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                    模型只用于本次分析，不会替员工改绑
                  </span>
                </div>
                <textarea value={instructions} onChange={(e) => setInstructions(e.target.value)}
                  rows={3} placeholder="补充要求（可选）：例如「先按 12 人规模设计，销售只保留一个负责人」"
                  className="input w-full !text-sm resize-none" />
                <div className="flex items-center justify-between gap-3">
                  <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                    资料越多、业务越具体，岗位与职责就越贴合。没有可读资料时建议先上传。
                  </p>
                  <button onClick={analyze} disabled={!companyId || readinessBusy || !(readiness?.ready_count)}
                    className="btn-primary !py-2 text-sm shrink-0">
                    开始分析 →
                  </button>
                </div>
              </div>

              {!!runs.length && (
                <div>
                  <p className="text-[11px] font-semibold tracking-widest uppercase mb-2"
                    style={{ color: "var(--muted)" }}>最近分析</p>
                  <div className="space-y-1.5">
                    {runs.map((r) => (
                      <button key={r.id} onClick={() => openRun(r.id)} disabled={busy}
                        className="w-full flex items-center gap-3 rounded-xl px-3.5 py-2.5 text-left transition-colors hover:bg-white/[0.03]"
                        style={{ border: "1px solid var(--border)" }}>
                        <span className="text-xs font-medium">{statusText(r)}</span>
                        <span className="text-[11px] truncate" style={{ color: "var(--muted)" }}>
                          {r.created_at?.replace("T", " ").slice(0, 16)}
                        </span>
                        <span className="ml-auto text-[11px]" style={{ color: "var(--muted)" }}>
                          {r.draft?.totals
                            ? `${r.draft.totals.departments} 部门 / ${r.draft.totals.headcount} 人`
                            : "—"}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {phase === "analyzing" && (
            <div className="h-full flex flex-col items-center justify-center py-12">
              <div className="w-14 h-14 rounded-full border-2 border-t-transparent animate-spin"
                style={{ borderColor: "rgba(99,102,241,0.35)", borderTopColor: "#6366f1" }} />
              <p className="text-base font-medium mt-6">{STAGE_LABELS[stageIdx]}…</p>
              <p className="text-xs mt-2" style={{ color: "var(--muted)" }}>
                已用时 {elapsed}s · 正在阅读资料并设计组织，通常需要 20–90 秒
              </p>
              <div className="w-full max-w-[440px] mt-8 space-y-2.5">
                {STAGE_LABELS.map((label, i) => (
                  <div key={label} className="flex items-center gap-3">
                    <span className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0"
                      style={{
                        background: i < stageIdx ? "rgba(52,211,153,0.16)" : i === stageIdx ? "rgba(99,102,241,0.22)" : "rgba(255,255,255,0.03)",
                        color: i < stageIdx ? "var(--success)" : i === stageIdx ? "#a5b4fc" : "var(--muted)",
                        border: "1px solid var(--border)",
                      }}>{i < stageIdx ? "✓" : i + 1}</span>
                    <span className="text-sm" style={{ color: i <= stageIdx ? "var(--foreground)" : "var(--muted)" }}>
                      {label}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {(phase === "review" || phase === "applying") && run && draft && (
            <div className="space-y-5">
              {run.status === "FAILED" && (
                <div className="rounded-xl px-4 py-3 text-sm"
                  style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
                  这次分析失败：{run.error || "模型未返回可用结果"}
                </div>
              )}
              {run.status === "APPLIED" && !rollbackDone && (
                <div className="rounded-xl px-4 py-3 text-sm flex items-center gap-3"
                  style={{ background: "rgba(52,211,153,0.08)", border: "1px solid rgba(52,211,153,0.3)", color: "#6ee7b7" }}>
                  <span className="flex-1">这次组织架构已经落地到部门与员工。</span>
                  <button onClick={rollback} disabled={busy} className="btn-ghost !py-1 text-xs">撤销落地</button>
                </div>
              )}
              {run.status === "ROLLED_BACK" && (
                <div className="rounded-xl px-4 py-3 text-sm"
                  style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)", color: "var(--muted)" }}>
                  这次落地已按快照撤销；原有部门与员工未受影响。
                </div>
              )}

              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <div>
                  <span className="text-2xl font-bold text-gradient tabular-nums">{draft.totals.headcount}</span>
                  <span className="text-xs ml-2" style={{ color: "var(--muted)" }}>计划人数</span>
                </div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  {draft.totals.centers} 中心 · {draft.totals.departments} 部门 · {draft.totals.roles} 岗位
                </div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  新增 {draft.totals.new_headcount} · 复用 {draft.totals.reuse_headcount}
                </div>
                {!!profile.industry && (
                  <span className="text-xs px-2 py-1 rounded-md"
                    style={{ background: "rgba(99,102,241,0.12)", color: "#c7d2fe" }}>
                    {profile.industry}
                  </span>
                )}
              </div>

              {!!profile.summary && (
                <p className="text-sm leading-relaxed rounded-xl p-3.5"
                  style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--muted)" }}>
                  {profile.summary}
                </p>
              )}

              <div className="space-y-4">
                {draft.centers.map((center) => {
                  const depts = draft.departments.filter((d) => d.center === center.name);
                  if (!depts.length) return null;
                  return (
                    <div key={center.name} className="rounded-xl overflow-hidden"
                      style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                      <div className="px-4 py-2.5 flex items-center gap-2"
                        style={{ background: "rgba(99,102,241,0.08)", borderBottom: "1px solid var(--border)" }}>
                        <span className="text-sm font-semibold">{center.name}</span>
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                          {depts.length} 部门 · {depts.reduce((s, d) => s + (draft.agents.filter((a) => a.department === d.name).length), 0)} 岗位
                        </span>
                        {!!center.description && (
                          <span className="ml-auto text-[10px] truncate" style={{ color: "var(--muted)" }}>{center.description}</span>
                        )}
                      </div>
                      <div className="divide-y" style={{ borderColor: "var(--border)" }}>
                        {depts.map((dept) => {
                          const members = draft.agents.filter((a) => a.department === dept.name);
                          return (
                            <div key={dept.name} className="px-4 py-3">
                              <div className="flex items-center gap-2">
                                <span className="text-[13px] font-medium">{dept.name}</span>
                                {dept.state === "reuse" && (
                                  <span className="text-[9px] px-1.5 py-0.5 rounded"
                                    style={{ background: "rgba(34,211,238,0.12)", color: "#67e8f9" }}>已有</span>
                                )}
                                {!!dept.reason && (
                                  <span className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{dept.reason}</span>
                                )}
                                <span className="ml-auto text-[10px]" style={{ color: "var(--muted)" }}>{members.length} 岗位</span>
                              </div>
                              <div className="mt-2 space-y-1.5">
                                {members.map((agent) => {
                                  const sel = selectionOf(agent);
                                  return (
                                    <div key={keyOf(agent)} className="rounded-lg px-3 py-2.5"
                                      style={{
                                        background: sel.included ? "rgba(255,255,255,0.025)" : "rgba(255,255,255,0.01)",
                                        border: "1px solid var(--border)",
                                        opacity: sel.included ? 1 : 0.55,
                                      }}>
                                      <div className="flex items-start gap-2.5">
                                        <input type="checkbox" checked={sel.included}
                                          onChange={(e) => updateSelection(agent, { included: e.target.checked })}
                                          className="mt-0.5 accent-indigo-500" aria-label={`保留${agent.name}`} />
                                        <div className="min-w-0 flex-1">
                                          <div className="flex flex-wrap items-center gap-2">
                                            <span className="text-xs font-semibold">{agent.name}</span>
                                            <span className="text-[10px]" style={{ color: "var(--muted)" }}>{agent.role}</span>
                                            {agent.is_leader && (
                                              <span className="text-[9px] px-1.5 py-0.5 rounded"
                                                style={{ background: "rgba(251,191,36,0.12)", color: "#fcd34d" }}>负责人</span>
                                            )}
                                            <span className="text-[9px] px-1.5 py-0.5 rounded"
                                              style={agent.state === "reuse"
                                                ? { background: "rgba(34,211,238,0.12)", color: "#67e8f9" }
                                                : { background: "rgba(52,211,153,0.12)", color: "#6ee7b7" }}>
                                              {agent.state === "reuse" ? `复用 ${agent.existing_headcount}` : "新增"}
                                            </span>
                                          </div>
                                          {!!agent.reason && (
                                            <p className="text-[11px] mt-1 leading-relaxed" style={{ color: "var(--muted)" }}>
                                              {agent.reason}
                                            </p>
                                          )}
                                          {!!agent.evidence?.length && (
                                            <p className="text-[10px] mt-1 truncate" style={{ color: "#818cf8" }}>
                                              依据：{agent.evidence.join(" · ")}
                                            </p>
                                          )}
                                        </div>
                                        <div className="flex items-center gap-1 shrink-0">
                                          <button onClick={() => updateSelection(agent, { headcount: Math.max(1, sel.headcount - 1) })}
                                            disabled={!sel.included || sel.headcount <= 1}
                                            className="w-6 h-6 rounded-md text-sm disabled:opacity-30"
                                            style={{ border: "1px solid var(--border)" }} aria-label="减少编制">−</button>
                                          <span className="w-7 text-center text-xs tabular-nums">{sel.headcount}</span>
                                          <button onClick={() => updateSelection(agent, { headcount: Math.min(10, sel.headcount + 1) })}
                                            disabled={!sel.included || sel.headcount >= 10}
                                            className="w-6 h-6 rounded-md text-sm disabled:opacity-30"
                                            style={{ border: "1px solid var(--border)" }} aria-label="增加编制">＋</button>
                                        </div>
                                      </div>
                                    </div>
                                  );
                                })}
                                {!members.length && (
                                  <p className="text-[10px]" style={{ color: "#fcd34d" }}>
                                    这个部门还没有岗位，落地后会是空部门。
                                  </p>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>

              {!!warnings.length && (
                <div className="space-y-2">
                  <p className="text-[11px] font-semibold tracking-widest uppercase"
                    style={{ color: "var(--muted)" }}>待确认项（{warnings.length}）</p>
                  {warnings.map((w, i) => {
                    const s = levelStyle(w.level);
                    return (
                      <div key={`${w.title}-${i}`} className="rounded-xl px-3.5 py-2.5"
                        style={{ background: s.bg, border: `1px solid ${s.border}` }}>
                        <p className="text-xs font-medium" style={{ color: s.color }}>{w.title}</p>
                        {!!w.detail && (
                          <p className="text-[11px] mt-1 leading-relaxed" style={{ color: "var(--muted)" }}>{w.detail}</p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {!!run.stages?.length && (
                <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                  分析耗时 {run.stages.reduce((s, x) => s + (x.duration_ms || 0), 0)}ms
                  {run.usage?.total_tokens ? ` · ${run.usage.total_tokens} tokens` : ""}
                  {run.usage?.cost_estimate_cny ? ` · 约 ¥${run.usage.cost_estimate_cny}` : ""}
                </p>
              )}
            </div>
          )}

          {phase === "done" && applyResult && (
            <div className="h-full flex flex-col items-center justify-center text-center py-10">
              <span className="w-16 h-16 rounded-full flex items-center justify-center text-2xl"
                style={{ background: "rgba(52,211,153,0.14)", border: "1px solid rgba(52,211,153,0.4)", color: "var(--success)" }}>✓</span>
              <h3 className="text-xl font-semibold mt-5">组织架构已落地</h3>
              <p className="text-sm mt-2 max-w-[520px]" style={{ color: "var(--muted)" }}>
                新建 {applyResult.created_centers} 个中心、{applyResult.created_departments} 个部门、
                {applyResult.created_agents} 名员工；复用 {applyResult.reused_headcount} 人，
                原有组织未改动。
              </p>
              <div className="flex gap-2 mt-6">
                <button onClick={onClose} className="btn-primary !px-5">完成</button>
                <button onClick={rollback} disabled={busy} className="btn-ghost !px-4">
                  {busy ? "撤销中…" : "撤销这次落地"}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* 底部操作 */}
        {phase === "review" && run && run.status !== "FAILED" && (
          <div className="px-6 py-3.5 flex flex-wrap items-center gap-3 shrink-0"
            style={{ borderTop: "1px solid var(--border)", background: "var(--surface-2)" }}>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              将落地 {includedAgents.length} 个岗位 / {plannedHeadcount} 人
              {plannedReuse > 0 ? `（其中复用 ${plannedReuse} 人）` : ""}
            </p>
            <div className="ml-auto flex items-center gap-2">
              <button onClick={() => setPhase("readiness")} disabled={run.status === "APPLIED"}
                className="btn-ghost !py-1.5 text-xs">返回资料</button>
              <button onClick={analyze} disabled={run.status === "APPLIED"}
                className="btn-ghost !py-1.5 text-xs">重新分析</button>
              {run.status !== "APPLIED" && (
                <button onClick={applyOrg} disabled={!includedAgents.length || busy}
                  className="btn-primary !py-2 text-sm">
                  {busy ? "落地中…" : "确认落地"}
                </button>
              )}
            </div>
          </div>
        )}

        {phase === "applying" && (
          <div className="px-6 py-3.5 text-xs shrink-0"
            style={{ borderTop: "1px solid var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}>
            正在创建部门与员工，请稍候…
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
