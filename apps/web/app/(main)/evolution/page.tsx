"use client";

// CEO 成长与监督
//
// 两个视图共用一页，因为它们回答的是同一个问题：「这个 CEO 这周有没有变强？」
//   · 自我进化：每天微调 / 每周复盘 / 每月迭代，所有版本与待拍板提案都在时间线上；
//   · 周考：监督 Agent 出题、CEO 作答、监督 Agent 评分，老板可人工覆盖。
//
// 数据接口：
//   GET  /evolution            → 当前 playbook + 历史记录 + 定时任务
//   POST /evolution/run        → 手动触发一档进化（会消耗模型 Token）
//   POST /evolution/{id}/actions/{aid} → 对提案拍板
//   GET  /exams                → 最近考试 + 监督员 + 定时任务
//   POST /exams/run            → 手动开考
//   POST /exams/{id}/override  → 人工覆盖评分
//
// 所有写操作都先经 confirmAction，再以响应为准刷新，不做本地乐观更新。

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";

/* ------------------------------------------------------------------ *
 * 类型：逐字段对着 responses_evolution.py / responses_exam.py
 * ------------------------------------------------------------------ */

type EvolutionAction = {
  id: string;
  type: string;
  title: string;
  detail?: string;
  risk?: string;
  applied?: boolean;
  approved?: boolean;
  decided_by?: string | null;
  decided_at?: string | null;
  note?: string | null;
};

type Playbook = {
  id: string;
  version: number;
  source: string;
  persona: Record<string, unknown>;
  principles: string[];
  adjustments: Record<string, unknown>;
  summary?: string | null;
  lessons: string[];
  created_at?: string | null;
};

type EvolutionAssessment = {
  score?: number;
  headline?: string;
  completion_rate?: number;
  failure_rate?: number;
  retry_rate?: number;
  tasks?: {
    total?: number;
    completed?: number;
    failed?: number;
    active?: number;
  };
  missions?: { total?: number; completed?: number; failed?: number };
  issues?: string[];
  wins?: string[];
};

type EvolutionRecord = {
  id: string;
  level: string;
  period_key: string;
  status: string;
  assessment: EvolutionAssessment;
  diagnosis?: string | null;
  summary?: string | null;
  actions: EvolutionAction[];
  metrics: Record<string, unknown>;
  playbook_version?: number | null;
  error?: string | null;
  tokens_used?: number;
  cost?: number;
  source?: string;
  created_at?: string | null;
};

type ScheduleBrief = {
  id: string;
  name: string;
  action_type: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  next_run_at?: string | null;
  run_count?: number;
};

type EvolutionOverview = {
  company_id: string;
  playbook: Playbook | null;
  records: EvolutionRecord[];
  schedules: ScheduleBrief[];
  levels: { level: string; label: string }[];
};

type ExamQuestion = {
  id: string;
  dimension: string;
  dimension_label?: string;
  question: string;
  rubric?: string;
  max_score: number;
};

type ExamAnswer = { id: string; answer: string; answered: boolean };
type ExamGrade = { id: string; score: number; max_score: number; comment: string };

type Exam = {
  id: string;
  period_key: string;
  week_start?: string | null;
  week_end?: string | null;
  status: string;
  focus?: string | null;
  questions: ExamQuestion[];
  answers: ExamAnswer[];
  grades: ExamGrade[];
  scores: Record<string, number | null>;
  personality: {
    type?: string;
    risk_posture?: string;
    decision_style?: string;
    traits?: string[];
    evidence?: string[];
    confidence?: string;
  };
  overall?: number | null;
  grade?: string | null;
  summary?: string | null;
  recommendations: string[];
  metrics: Record<string, unknown>;
  override?: {
    by?: string | null;
    at?: string | null;
    note?: string | null;
    previous?: Record<string, unknown>;
  } | null;
  tokens_used?: number;
  cost?: number;
  error?: string | null;
  source?: string;
  created_at?: string | null;
};

type ExamOverview = {
  company_id: string;
  latest: Exam | null;
  exams: Exam[];
  supervisor: { id: string; name: string; role?: string | null } | null;
  schedule: ScheduleBrief | null;
  dimensions: { key: string; label: string }[];
};

/* ------------------------------------------------------------------ *
 * 小工具
 * ------------------------------------------------------------------ */

const LEVEL_LABEL: Record<string, string> = {
  daily: "每日微调",
  weekly: "每周复盘",
  monthly: "每月迭代",
};

const LEVEL_HINT: Record<string, string> = {
  daily: "只调原则，不改人格与参数；每天一次",
  weekly: "基于一周真实数据重建运行人格；每周一次",
  monthly: "产出需老板拍板的迭代提案；每月一次",
};

const ADJUSTMENT_LABEL: Record<string, string> = {
  quality_bar: "质量线",
  cost_guard_ratio: "成本护栏",
  parallel_cap: "并发上限",
  retry_budget: "重试预算",
  monthly_focus: "本月重点",
};

function fmtTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function pct(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

function statusTone(status: string): { text: string; border: string; bg: string } {
  if (status === "APPLIED" || status === "GRADED") {
    return { text: "#6ee7b7", border: "rgba(52,211,153,0.4)", bg: "rgba(52,211,153,0.08)" };
  }
  if (status === "AWAITING_APPROVAL" || status === "OVERRIDDEN") {
    return { text: "#fcd34d", border: "rgba(251,191,36,0.4)", bg: "rgba(251,191,36,0.08)" };
  }
  if (status === "SKIPPED" || status === "FAILED") {
    return { text: "#fca5a5", border: "rgba(248,113,113,0.4)", bg: "rgba(248,113,113,0.08)" };
  }
  return { text: "var(--muted)", border: "var(--border)", bg: "rgba(255,255,255,0.02)" };
}

function scoreColor(score?: number | null): string {
  if (score === null || score === undefined) return "var(--muted)";
  if (score >= 90) return "#34d399";
  if (score >= 75) return "#60a5fa";
  if (score >= 60) return "#fbbf24";
  return "#f87171";
}

function List({ items }: { items: unknown[] }) {
  if (!items?.length) return null;
  return (
    <ul className="space-y-1">
      {items.map((item, i) => (
        <li key={i} className="flex gap-2 text-xs leading-5">
          <span style={{ color: "var(--muted)" }}>·</span>
          <span>{typeof item === "string" ? item : JSON.stringify(item)}</span>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ *
 * 页面
 * ------------------------------------------------------------------ */

export default function EvolutionPage() {
  const cid = useActiveCompanyId();
  const [tab, setTab] = useState<"evolution" | "exam">("evolution");

  // 深链：/evolution?tab=exam（CEO 页入口卡片直接指向周考）
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("tab");
    if (q === "exam") setTab("exam");
  }, []);

  function switchTab(next: "evolution" | "exam") {
    setTab(next);
    const url = next === "exam" ? "/evolution?tab=exam" : "/evolution";
    window.history.replaceState(null, "", url);
  }

  const evo = useResource<EvolutionOverview>(
    cid ? `evolution:${cid}` : null,
    () => api.get<EvolutionOverview>("/evolution?limit=30"),
  );
  const exams = useResource<ExamOverview>(
    cid ? `exams:${cid}` : null,
    () => api.get<ExamOverview>("/exams?limit=12"),
  );

  const records = useMemo(() => evo.data?.records ?? [], [evo.data]);
  const playbook = evo.data?.playbook ?? null;
  const levels = useMemo(
    () => evo.data?.levels?.length
      ? evo.data.levels
      : Object.keys(LEVEL_LABEL).map((k) => ({ level: k, label: LEVEL_LABEL[k] })),
    [evo.data],
  );

  const [level, setLevel] = useState("weekly");
  const [running, setRunning] = useState(false);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [scheduleBusy, setScheduleBusy] = useState(false);

  async function runEvolution() {
    if (!cid || running) return;
    const ok = await confirmAction({
      title: `执行一次「${LEVEL_LABEL[level] ?? level}」？`,
      description: "会读取本期真实运行数据并调用模型生成新的人格/原则，消耗 Token。若本期已有记录则直接复用，不重复计费。",
      confirmText: "开始进化",
      danger: false,
    });
    if (!ok) return;
    setRunning(true);
    try {
      const res = await api.post<EvolutionRecord>("/evolution/run", {
        company_id: cid,
        level,
      });
      if (res.status === "SKIPPED") {
        notify("info", `本期跳过：${res.error ?? "没有可用的运行数据"}`);
      } else {
        notify(
          "success",
          `${LEVEL_LABEL[res.level] ?? res.level}完成：${res.assessment?.score ?? "—"} 分` +
            (res.playbook_version ? ` · playbook v${res.playbook_version}` : ""),
        );
      }
      evo.refresh();
    } catch (e) {
      notify("error", describeError(e, "进化执行失败"));
    } finally {
      setRunning(false);
    }
  }

  async function decide(record: EvolutionRecord, action: EvolutionAction, approve: boolean) {
    if (!approve) {
      const ok = await confirmAction({
        title: "驳回这项提案？",
        description: action.title,
        confirmText: "驳回",
        danger: true,
      });
      if (!ok) return;
    }
    setDeciding(action.id);
    try {
      const res = await api.post<EvolutionRecord>(
        `/evolution/${record.id}/actions/${action.id}`,
        { approve, note: null },
      );
      notify(
        approve ? "success" : "info",
        approve
          ? `已批准：${action.title}${res.playbook_version ? `（playbook v${res.playbook_version}）` : ""}`
          : `已驳回：${action.title}`,
      );
      evo.refresh();
    } catch (e) {
      notify("error", describeError(e, "拍板失败"));
    } finally {
      setDeciding(null);
    }
  }

  async function ensureEvolutionSchedules() {
    if (!cid || scheduleBusy) return;
    setScheduleBusy(true);
    try {
      const res = await api.post<{ created: Record<string, string> }>(
        "/evolution/schedules",
        { company_id: cid, level: "daily" },
      );
      const n = Object.keys(res.created ?? {}).length;
      notify("success", n ? `已启用 ${n} 个进化定时任务` : "定时任务已存在，无需重复创建");
      evo.refresh();
    } catch (e) {
      notify("error", describeError(e, "启用定时失败"));
    } finally {
      setScheduleBusy(false);
    }
  }

  /* ---------------- 周考 ---------------- */

  const examList = useMemo(() => exams.data?.exams ?? [], [exams.data]);
  const [selectedExamId, setSelectedExamId] = useState<string | null>(null);

  useEffect(() => {
    if (!examList.length) {
      setSelectedExamId(null);
      return;
    }
    if (!selectedExamId || !examList.some((e) => e.id === selectedExamId)) {
      setSelectedExamId(examList[0].id);
    }
  }, [examList, selectedExamId]);

  const exam = useMemo(
    () => examList.find((e) => e.id === selectedExamId) ?? exams.data?.latest ?? null,
    [examList, selectedExamId, exams.data],
  );

  const [examRunning, setExamRunning] = useState(false);

  async function runExam() {
    if (!cid || examRunning) return;
    const ok = await confirmAction({
      title: "现在开一场周考？",
      description: "监督 Agent 会出题、CEO 作答、监督 Agent 评分，会消耗 Token。同一周已有考卷时直接复用。",
      confirmText: "开考",
      danger: false,
    });
    if (!ok) return;
    setExamRunning(true);
    try {
      const res = await api.post<Exam>("/exams/run", { company_id: cid });
      if (res.status === "SKIPPED") {
        notify("info", `本周考试跳过：${res.error ?? "模型不可用"}`);
      } else {
        notify(
          "success",
          `${res.period_key} 周考完成：${res.overall ?? "—"} 分（${res.grade ?? "—"}）`,
        );
      }
      exams.refresh();
    } catch (e) {
      notify("error", describeError(e, "开考失败"));
    } finally {
      setExamRunning(false);
    }
  }

  async function ensureExamSchedule() {
    if (!cid || scheduleBusy) return;
    setScheduleBusy(true);
    try {
      const res = await api.post<{ created: Record<string, string> }>(
        "/exams/schedules",
        { company_id: cid },
      );
      const n = Object.keys(res.created ?? {}).length;
      notify("success", n ? "已启用每周自动周考" : "周考定时任务已存在");
      exams.refresh();
    } catch (e) {
      notify("error", describeError(e, "启用周考定时失败"));
    } finally {
      setScheduleBusy(false);
    }
  }

  /* ---------------- 人工覆盖 ---------------- */

  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideOverall, setOverrideOverall] = useState("");
  const [overrideSummary, setOverrideSummary] = useState("");
  const [overrideNote, setOverrideNote] = useState("");
  const [overriding, setOverriding] = useState(false);

  function openOverride(target: Exam) {
    setOverrideOverall(target.overall !== null && target.overall !== undefined ? String(target.overall) : "");
    setOverrideSummary(target.summary ?? "");
    setOverrideNote("");
    setOverrideOpen(true);
  }

  async function submitOverride() {
    if (!exam || overriding) return;
    const overall = overrideOverall.trim() === "" ? null : Number(overrideOverall);
    if (overall !== null && (Number.isNaN(overall) || overall < 0 || overall > 100)) {
      notify("error", "总分必须是 0–100 之间的数字");
      return;
    }
    setOverriding(true);
    try {
      const res = await api.post<Exam>(`/exams/${exam.id}/override`, {
        overall,
        summary: overrideSummary.trim() || null,
        note: overrideNote.trim() || null,
      });
      notify("success", `已人工覆盖：总分 ${res.overall ?? "—"}（${res.grade ?? "—"}）`);
      setOverrideOpen(false);
      exams.refresh();
    } catch (e) {
      notify("error", describeError(e, "覆盖评分失败"));
    } finally {
      setOverriding(false);
    }
  }

  const evoError = evo.error;
  const examError = exams.error;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-5">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">📈 CEO 成长与监督</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            每天微调、每周复盘、每月迭代；每周由监督 Agent 出题考试。全部自动运行，老板可随时人工干预。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Link href="/experience" className="btn-ghost text-xs whitespace-nowrap">
            🧩 经验池
          </Link>
          <button onClick={() => { evo.refresh(); exams.refresh(); }} className="btn-ghost text-xs">
            刷新
          </button>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {[
          { key: "evolution" as const, label: "自我进化", icon: "🧬" },
          { key: "exam" as const, label: "周考测评", icon: "🎓" },
        ].map((t) => (
          <button
            key={t.key}
            onClick={() => switchTab(t.key)}
            className={`whitespace-nowrap ${t.key === tab ? "btn-primary !py-1.5 !px-4 text-xs" : "btn-ghost text-xs"}`}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {!cid && <p className="text-sm" style={{ color: "var(--muted)" }}>加载中…</p>}

      {tab === "evolution" && (
        <>
          {evoError && (
            <div
              className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
              style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}
            >
              <span>🔌</span>
              <span className="flex-1">读取进化记录失败：{evoError}</span>
              <button onClick={() => evo.refresh()} className="btn-ghost !py-1 text-xs">重试</button>
            </div>
          )}

          {/* 运行人格 + 定时任务 */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div className="card card-static p-4 lg:col-span-2 space-y-3">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div>
                  <h2 className="font-medium text-sm">🧠 当前运行人格</h2>
                  <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                    每次进化都会生成新版本；旧版本保留可追溯。
                  </p>
                </div>
                {playbook && (
                  <span className="badge" style={{ borderColor: "rgba(99,102,241,0.5)", color: "#a5b4fc" }}>
                    v{playbook.version}
                  </span>
                )}
              </div>

              {!playbook ? (
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  还没有运行人格。点下方「立即进化」，系统会根据真实运行数据生成第一版。
                </p>
              ) : (
                <div className="space-y-3">
                  {playbook.summary && (
                    <p className="text-sm leading-6">{playbook.summary}</p>
                  )}
                  <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2">
                    {Object.entries(playbook.adjustments ?? {})
                      .filter(([k]) => k !== "monthly_focus")
                      .map(([k, v]) => (
                        <div key={k} className="rounded-lg px-3 py-2" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                          <div className="text-[10px]" style={{ color: "var(--muted)" }}>
                            {ADJUSTMENT_LABEL[k] ?? k}
                          </div>
                          <div className="text-lg font-bold tabular-nums">{String(v)}</div>
                        </div>
                      ))}
                  </div>
                  {Array.isArray(playbook.adjustments?.monthly_focus) &&
                    playbook.adjustments.monthly_focus.length > 0 && (
                      <div>
                        <div className="text-[11px] font-medium mb-1" style={{ color: "var(--muted)" }}>本月重点</div>
                        <div className="flex flex-wrap gap-1.5">
                          {(playbook.adjustments.monthly_focus as string[]).map((f, i) => (
                            <span key={i} className="text-[11px] px-2 py-0.5 rounded"
                              style={{ background: "rgba(251,191,36,0.1)", color: "#fcd34d", border: "1px solid rgba(251,191,36,0.25)" }}>
                              {f}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  {playbook.persona && Boolean(playbook.persona.risk_posture) && (
                    <div className="text-xs" style={{ color: "var(--muted)" }}>
                      风险姿态：{String(playbook.persona.risk_posture)}
                      {playbook.persona.decision_style ? ` · 决策风格：${String(playbook.persona.decision_style)}` : ""}
                    </div>
                  )}
                  <div>
                    <div className="text-[11px] font-medium mb-1" style={{ color: "var(--muted)" }}>运行原则</div>
                    <List items={playbook.principles} />
                  </div>
                  {playbook.lessons?.length > 0 && (
                    <div>
                      <div className="text-[11px] font-medium mb-1" style={{ color: "var(--muted)" }}>沉淀的教训</div>
                      <List items={playbook.lessons.slice(0, 6)} />
                    </div>
                  )}
                  <div className="text-[10px]" style={{ color: "var(--muted)" }}>
                    来源 {playbook.source} · 生成于 {fmtTime(playbook.created_at)}
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-3">
              <div className="card card-static p-4 space-y-3">
                <div>
                  <h2 className="font-medium text-sm">⏱ 自动进化节奏</h2>
                  <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                    日 / 周 / 月三档，到点自动跑；同周期只跑一次。
                  </p>
                </div>
                {(evo.data?.schedules ?? []).length === 0 ? (
                  <button
                    onClick={ensureEvolutionSchedules}
                    disabled={scheduleBusy || !cid}
                    className="btn-ghost text-xs w-full disabled:opacity-40"
                  >
                    {scheduleBusy ? "启用中…" : "启用三档自动进化"}
                  </button>
                ) : (
                  <div className="space-y-2">
                    {(evo.data?.schedules ?? []).map((s) => (
                      <div key={s.id} className="rounded-lg px-3 py-2 text-xs"
                        style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                        <div className="font-medium">{s.name}</div>
                        <div className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                          下次 {fmtTime(s.next_run_at)} · 已跑 {s.run_count ?? 0} 次
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="card card-static p-4 space-y-2">
                <h2 className="font-medium text-sm">▶ 立即进化</h2>
                <select
                  value={level}
                  onChange={(e) => setLevel(e.target.value)}
                  className="input text-xs"
                  aria-label="选择进化档位"
                >
                  {levels.map((l) => (
                    <option key={l.level} value={l.level}>{l.label}</option>
                  ))}
                </select>
                <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                  {LEVEL_HINT[level] ?? ""}
                </p>
                <button
                  onClick={runEvolution}
                  disabled={running || !cid}
                  className="btn-primary text-xs w-full disabled:opacity-40"
                >
                  {running ? "执行中（会调用模型）…" : "立即进化"}
                </button>
                <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                  本期已有记录时直接复用，不会重复扣费。
                </p>
              </div>
            </div>
          </div>

          {/* 进化时间线 */}
          <section className="space-y-2">
            <h2 className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--muted)" }}>
              进化时间线
            </h2>
            {evo.loading && records.length === 0 && (
              <p className="text-xs" style={{ color: "var(--muted)" }}>加载中…</p>
            )}
            {!evo.loading && records.length === 0 && (
              <div className="card card-static p-4 text-xs" style={{ color: "var(--muted)" }}>
                还没有进化记录。等第一天的任务跑完后，点「每日微调」生成第一版。
              </div>
            )}
            <div className="space-y-2">
              {records.map((r) => {
                const tone = statusTone(r.status);
                const a = r.assessment ?? {};
                const pending = (r.actions ?? []).filter((x) => !x.applied);
                return (
                  <div key={r.id} className="card card-static p-4 space-y-2.5">
                    <div className="flex items-start justify-between gap-3 flex-wrap">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="badge" style={{ borderColor: tone.border, color: tone.text, background: tone.bg }}>
                          {LEVEL_LABEL[r.level] ?? r.level}
                        </span>
                        <span className="text-xs font-medium">{r.period_key}</span>
                        {r.playbook_version ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded"
                            style={{ background: "rgba(99,102,241,0.12)", color: "#a5b4fc" }}>
                            人格 v{r.playbook_version}
                          </span>
                        ) : null}
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                          {fmtTime(r.created_at)} · {r.source === "manual" ? "手动" : "定时"}
                        </span>
                      </div>
                      {typeof a.score === "number" && (
                        <div className="text-right">
                          <div className="text-lg font-bold tabular-nums" style={{ color: scoreColor(a.score) }}>
                            {a.score}
                          </div>
                          <div className="text-[10px]" style={{ color: "var(--muted)" }}>{a.headline ?? "运行分"}</div>
                        </div>
                      )}
                    </div>

                    {r.diagnosis && (
                      <p className="text-xs leading-5 whitespace-pre-wrap">{r.diagnosis}</p>
                    )}
                    {r.summary && r.summary !== r.diagnosis && (
                      <p className="text-xs leading-5" style={{ color: "var(--muted)" }}>{r.summary}</p>
                    )}

                    {a.tasks && (
                      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]" style={{ color: "var(--muted)" }}>
                        <span>任务 {a.tasks.total ?? 0}（完成 {a.tasks.completed ?? 0} / 失败 {a.tasks.failed ?? 0}）</span>
                        <span>完成率 {pct(a.completion_rate)}</span>
                        <span>重试率 {pct(a.retry_rate)}</span>
                        {r.tokens_used ? <span>{r.tokens_used} token</span> : null}
                      </div>
                    )}

                    {(a.issues?.length ?? 0) > 0 && (
                      <div>
                        <div className="text-[11px] font-medium mb-1" style={{ color: "#fca5a5" }}>待改进</div>
                        <List items={a.issues ?? []} />
                      </div>
                    )}
                    {(a.wins?.length ?? 0) > 0 && (
                      <div>
                        <div className="text-[11px] font-medium mb-1" style={{ color: "#6ee7b7" }}>做得好的</div>
                        <List items={a.wins ?? []} />
                      </div>
                    )}

                    {r.error && (
                      <div className="text-[11px]" style={{ color: "#fca5a5" }}>跳过原因：{r.error}</div>
                    )}

                    {(r.actions ?? []).length > 0 && (
                      <div className="space-y-2 pt-1 border-t" style={{ borderColor: "var(--border)" }}>
                        {(r.actions ?? []).map((action) => {
                          const applied = Boolean(action.applied);
                          const risky = action.risk === "high";
                          return (
                            <div key={action.id} className="flex items-start justify-between gap-3 flex-wrap text-xs">
                              <div className="min-w-0">
                                <div className="flex items-center gap-1.5 flex-wrap">
                                  <span className="font-medium">{action.title}</span>
                                  <span className="text-[10px] px-1.5 py-0.5 rounded"
                                    style={{
                                      background: risky ? "rgba(248,113,113,0.12)" : "rgba(255,255,255,0.05)",
                                      color: risky ? "#fca5a5" : "var(--muted)",
                                    }}>
                                    {risky ? "高风险" : "低风险"}
                                  </span>
                                  {applied ? (
                                    <span className="text-[10px]" style={{ color: "#6ee7b7" }}>已生效</span>
                                  ) : (
                                    <span className="text-[10px]" style={{ color: "#fcd34d" }}>待拍板</span>
                                  )}
                                </div>
                                {action.detail && (
                                  <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{action.detail}</div>
                                )}
                              </div>
                              {!applied && pending.length > 0 && (
                                <div className="flex items-center gap-1.5 shrink-0">
                                  <button
                                    onClick={() => decide(r, action, true)}
                                    disabled={deciding === action.id}
                                    className="btn-primary !py-1 !px-3 text-[11px] disabled:opacity-40"
                                  >
                                    {deciding === action.id ? "处理中…" : "批准"}
                                  </button>
                                  <button
                                    onClick={() => decide(r, action, false)}
                                    disabled={deciding === action.id}
                                    className="btn-ghost !py-1 text-[11px] disabled:opacity-40"
                                  >
                                    驳回
                                  </button>
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        </>
      )}

      {tab === "exam" && (
        <>
          {examError && (
            <div
              className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
              style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}
            >
              <span>🔌</span>
              <span className="flex-1">读取周考记录失败：{examError}</span>
              <button onClick={() => exams.refresh()} className="btn-ghost !py-1 text-xs">重试</button>
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div className="card card-static p-4 space-y-2 lg:col-span-2">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div>
                  <h2 className="font-medium text-sm">🎓 监督 Agent</h2>
                  <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                    独立于 CEO：负责出题、评分与给出人格结论，不参与经营决策。
                  </p>
                </div>
                <button
                  onClick={runExam}
                  disabled={examRunning || !cid}
                  className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40"
                >
                  {examRunning ? "考试进行中…" : "立即开考"}
                </button>
              </div>
              <div className="text-xs" style={{ color: "var(--muted)" }}>
                {exams.data?.supervisor
                  ? `${exams.data.supervisor.name}${exams.data.supervisor.role ? ` · ${exams.data.supervisor.role}` : ""}（首次开考时自动创建）`
                  : "还没有监督 Agent，首次开考时会自动创建并复制 CEO 的模型配置。"}
              </div>
              <div className="flex items-center gap-2 flex-wrap text-[11px]" style={{ color: "var(--muted)" }}>
                {exams.data?.schedule ? (
                  <>
                    <span>每周自动周考已启用</span>
                    <span>· 下次 {fmtTime(exams.data.schedule.next_run_at)}</span>
                    <span>· 已跑 {exams.data.schedule.run_count ?? 0} 次</span>
                  </>
                ) : (
                  <>
                    <span>还没有启用自动周考</span>
                    <button
                      onClick={ensureExamSchedule}
                      disabled={scheduleBusy || !cid}
                      className="btn-ghost !py-1 text-[11px] disabled:opacity-40"
                    >
                      {scheduleBusy ? "启用中…" : "启用每周自动周考"}
                    </button>
                  </>
                )}
              </div>
            </div>

            <div className="card card-static p-4 space-y-2">
              <h2 className="font-medium text-sm">🗂 历史考卷</h2>
              {examList.length === 0 ? (
                <p className="text-xs" style={{ color: "var(--muted)" }}>还没有考卷。</p>
              ) : (
                <div className="space-y-1.5 max-h-[220px] overflow-auto pr-1">
                  {examList.map((e) => {
                    const active = e.id === exam?.id;
                    const tone = statusTone(e.status);
                    return (
                      <button
                        key={e.id}
                        onClick={() => setSelectedExamId(e.id)}
                        className="w-full text-left rounded-lg px-2.5 py-2 text-xs transition-colors"
                        style={{
                          background: active ? "rgba(99,102,241,0.14)" : "rgba(255,255,255,0.02)",
                          border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
                        }}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium">{e.period_key}</span>
                          <span style={{ color: tone.text }}>{e.overall ?? "—"} 分</span>
                        </div>
                        <div className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                          {e.status === "SKIPPED" ? "跳过" : e.grade ? `${e.grade} 级` : e.status}
                          {e.personality?.type ? ` · ${e.personality.type}` : ""}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {!exam && !exams.loading && (
            <div className="card card-static p-4 text-xs" style={{ color: "var(--muted)" }}>
              还没有考试记录。点「立即开考」让监督 Agent 出第一份卷子。
            </div>
          )}

          {exam && (
            <>
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                <div className="card card-static p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <h2 className="font-medium text-sm">综合评分</h2>
                    <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                      {exam.period_key}
                    </span>
                  </div>
                  <div className="flex items-end gap-2">
                    <div className="text-4xl font-bold tabular-nums" style={{ color: scoreColor(exam.overall) }}>
                      {exam.overall ?? "—"}
                    </div>
                    <div className="pb-1 text-sm" style={{ color: scoreColor(exam.overall) }}>
                      {exam.grade ?? ""}
                    </div>
                  </div>
                  <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                    状态：{exam.status}
                    {exam.override ? " · 已人工覆盖" : ""}
                    {exam.tokens_used ? ` · ${exam.tokens_used} token` : ""}
                  </div>
                  {exam.error && <div className="text-[11px]" style={{ color: "#fca5a5" }}>{exam.error}</div>}
                  {exam.status === "GRADED" || exam.status === "OVERRIDDEN" ? (
                    <button
                      onClick={() => openOverride(exam)}
                      className="btn-ghost text-xs w-full"
                    >
                      人工干预评分
                    </button>
                  ) : null}
                </div>

                <div className="card card-static p-4 space-y-2 lg:col-span-2">
                  <h2 className="font-medium text-sm">🧩 人格画像</h2>
                  {exam.personality?.type ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-base font-semibold">{exam.personality.type}</span>
                        {exam.personality.risk_posture && (
                          <span className="badge" style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
                            {exam.personality.risk_posture}
                          </span>
                        )}
                        {exam.personality.confidence && (
                          <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                            置信度 {exam.personality.confidence}
                          </span>
                        )}
                      </div>
                      {exam.personality.decision_style && (
                        <p className="text-xs" style={{ color: "var(--muted)" }}>
                          决策风格：{exam.personality.decision_style}
                        </p>
                      )}
                      {(exam.personality.traits?.length ?? 0) > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {(exam.personality.traits ?? []).map((t, i) => (
                            <span key={i} className="text-[11px] px-2 py-0.5 rounded"
                              style={{ background: "rgba(99,102,241,0.12)", color: "#c7d2fe" }}>
                              {t}
                            </span>
                          ))}
                        </div>
                      )}
                      {(exam.personality.evidence?.length ?? 0) > 0 && (
                        <div>
                          <div className="text-[11px] font-medium mb-1" style={{ color: "var(--muted)" }}>判断依据</div>
                          <List items={exam.personality.evidence ?? []} />
                        </div>
                      )}
                    </div>
                  ) : (
                    <p className="text-xs" style={{ color: "var(--muted)" }}>
                      这场考试还没有人格结论（可能被跳过或尚未评分）。
                    </p>
                  )}
                  {exam.summary && <p className="text-xs leading-5">{exam.summary}</p>}
                </div>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                <div className="card card-static p-4 space-y-2">
                  <h2 className="font-medium text-sm">📊 维度得分</h2>
                  <div className="space-y-2">
                    {(exams.data?.dimensions?.length ?? 0) > 0 ? (
                      exams.data?.dimensions.map((d) => {
                        const v = exam.scores?.[d.key];
                        return (
                          <div key={d.key}>
                            <div className="flex items-center justify-between text-xs">
                              <span>{d.label}</span>
                              <span className="tabular-nums" style={{ color: scoreColor(v) }}>
                                {v === null || v === undefined ? "—" : `${v}`}
                              </span>
                            </div>
                            <div className="h-1.5 rounded-full mt-1 overflow-hidden" style={{ background: "var(--border)" }}>
                              <div
                                className="h-full rounded-full"
                                style={{ width: `${Math.max(0, Math.min(100, v ?? 0))}%`, background: scoreColor(v) }}
                              />
                            </div>
                          </div>
                        );
                      })
                    ) : (
                      <p className="text-xs" style={{ color: "var(--muted)" }}>暂无维度数据。</p>
                    )}
                  </div>
                  {(exam.recommendations?.length ?? 0) > 0 && (
                    <div className="pt-1">
                      <div className="text-[11px] font-medium mb-1" style={{ color: "var(--muted)" }}>改进建议</div>
                      <List items={exam.recommendations} />
                    </div>
                  )}
                </div>

                <div className="card card-static p-4 space-y-2 lg:col-span-2">
                  <h2 className="font-medium text-sm">📝 考卷与作答</h2>
                  {exam.focus && (
                    <p className="text-[11px]" style={{ color: "var(--muted)" }}>本期重点：{exam.focus}</p>
                  )}
                  <div className="space-y-2">
                    {(exam.questions ?? []).map((q, i) => {
                      const ans = exam.answers?.find((a) => a.id === q.id);
                      const grade = exam.grades?.find((g) => g.id === q.id);
                      return (
                        <div key={q.id} className="rounded-lg px-3 py-2 text-xs"
                          style={{ background: "rgba(255,255,255,0.02)", border: "1px solid var(--border)" }}>
                          <div className="flex items-start justify-between gap-2">
                            <div className="font-medium leading-5">
                              {i + 1}. {q.question}
                            </div>
                            <span className="shrink-0 tabular-nums" style={{ color: scoreColor(grade ? (grade.score / Math.max(1, grade.max_score)) * 100 : null) }}>
                              {grade ? `${grade.score}/${grade.max_score}` : "—"}
                            </span>
                          </div>
                          <div className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                            {q.dimension_label ?? q.dimension}
                            {q.rubric ? ` · 评分点：${q.rubric}` : ""}
                          </div>
                          <div className="mt-1.5 text-[11px] leading-5">
                            <span style={{ color: "var(--muted)" }}>CEO 答：</span>
                            {ans?.answer ? ans.answer : <span style={{ color: "#fcd34d" }}>（未作答）</span>}
                          </div>
                          {grade?.comment && (
                            <div className="mt-0.5 text-[11px] leading-5" style={{ color: "var(--muted)" }}>
                              评语：{grade.comment}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>

              {exam.override && (
                <div className="card card-static p-4 text-xs space-y-1">
                  <div className="font-medium">人工覆盖记录</div>
                  <div style={{ color: "var(--muted)" }}>
                    {fmtTime(exam.override.at)} · 原总分 {String(exam.override.previous?.overall ?? "—")}
                  </div>
                  {exam.override.note && <div style={{ color: "var(--muted)" }}>备注：{exam.override.note}</div>}
                </div>
              )}
            </>
          )}

          {overrideOpen && exam && (
            <div className="card card-static p-4 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="font-medium text-sm">人工干预 · {exam.period_key}</h2>
                <button onClick={() => setOverrideOpen(false)} className="btn-ghost !py-1 text-xs">关闭</button>
              </div>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                覆盖会保留原评分快照。留空表示不改对应字段。
              </p>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                <label className="text-xs space-y-1">
                  <div style={{ color: "var(--muted)" }}>总分（0–100）</div>
                  <input
                    className="input text-xs"
                    value={overrideOverall}
                    onChange={(e) => setOverrideOverall(e.target.value)}
                    inputMode="decimal"
                    placeholder="例如 82"
                  />
                </label>
                <label className="text-xs space-y-1 md:col-span-3">
                  <div style={{ color: "var(--muted)" }}>结论摘要</div>
                  <input
                    className="input text-xs"
                    value={overrideSummary}
                    onChange={(e) => setOverrideSummary(e.target.value)}
                    placeholder="对这场考试的人工结论"
                  />
                </label>
              </div>
              <label className="text-xs space-y-1 block">
                <div style={{ color: "var(--muted)" }}>备注（为什么改）</div>
                <textarea
                  className="input text-xs min-h-[70px]"
                  value={overrideNote}
                  onChange={(e) => setOverrideNote(e.target.value)}
                  placeholder="例如：成本维度评分过严，按实际预算执行情况修正"
                />
              </label>
              <div className="flex items-center gap-2">
                <button
                  onClick={submitOverride}
                  disabled={overriding}
                  className="btn-primary text-xs disabled:opacity-40"
                >
                  {overriding ? "提交中…" : "确认覆盖"}
                </button>
                <button onClick={() => setOverrideOpen(false)} className="btn-ghost text-xs">
                  取消
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
