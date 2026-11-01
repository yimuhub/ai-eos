"use client";

// CEO 指挥台（超级 Agent）
//
// 老板只面对这一个入口：指定 CEO → 定并发 → 一句话下目标 → 看拆解预览 → 开工
// → 盯作战视图（3 秒轮询）→ 收最终报告 / 每日日报。
//
// ## 几个刻意的取舍
// 1. **预览与开工分两步**：`POST /ceo/missions` 只做拆解（响应里 subtasks 是计划），
//    真正建任务/执行在 `POST /ceo/missions/{id}/launch`。所以"开工"按钮成功前
//    绝不显示"已开工"——所有状态都来自响应。
// 2. **容量三层公开**：effective = min(你设置, 供应商上限, 系统余量)，
//    三个数都摆出来，避免"我点了 10 为什么只跑 4"的猜疑。
// 3. **轮询按状态收敛**：只有 PLANNING/RUNNING/RECLAIMING/PAUSED 才 3 秒拉一次详情，
//    终态立即停；组件卸载 / 切换 mission 时 clearTimeout + abort 在途请求。
// 4. **报告不引依赖**：后端返回 Markdown 文本，这里用 <pre> 原样呈现，
//    不为一个渲染器给"克隆下来就能跑"增加依赖。
//
// 数据接口与 `apps/api/app/api/routes/ceo.py` 一一对应；请求走 lib/api，
// company_id 由 API 层自动注入，useResource 的 key 都带 cid（切公司自动重取）。

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { api, ApiError, describeError, isCancelled } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";

/* ------------------------------------------------------------------ *
 * 类型：逐字段对着 responses_ceo.py，不引入契约外的字段
 * ------------------------------------------------------------------ */

type CeoAgentOption = { id: string; name: string; role: string | null };

type CeoConfig = {
  company_id: string;
  has_ceo: boolean;
  ceo_agent_id: string;
  ceo_agent_name: string;
  candidates: CeoAgentOption[];
};

type CeoCapacity = {
  requested: number;
  provider_cap: number;
  provider_type: string;
  worker_free: number;
  effective: number;
  min_parallel: number;
  hard_cap: number;
  default_parallel: number;
};

type PlannedSubtask = {
  index: number;
  title: string;
  description: string;
  agent_id: string;
  agent_name: string;
  agent_matched: boolean;
  /** 指向 new_agents 的下标；null/undefined = 这条分工不涉及新招 */
  new_agent_index?: number | null;
};

/**
 * CEO 提议或已招的新员工。
 * 开工前 `agent_id` 为空（只是提议），开工后回填真实 id。
 */
type NewAgent = {
  name: string;
  role: string;
  department: string;
  group: string;
  reason: string;
  agent_id: string;
  proposed_name?: string;
};

type CeoPreview = {
  mission_id: string;
  title: string;
  status: string;
  objective: string;
  deliverable: string;
  redlines: string[];
  acceptance: string[];
  terminology: Record<string, string>;
  extra_instructions: string[];
  anchor_version: number;
  subtasks: PlannedSubtask[];
  subtask_count: number;
  new_agents: NewAgent[];
  coordinator_agent_id: string;
  coordinator_agent_name: string;
  max_parallel: number;
  effective_parallel: number;
  provider_cap: number;
  provider_type: string;
  min_parallel: number;
  hard_cap: number;
};

type CeoMissionItem = {
  mission_id: string;
  title: string;
  status: string;
  subtask_count: number;
  completed_count: number;
  failed_count: number;
  off_track_count: number;
  coordinator_agent_name: string;
  effective_parallel: number;
  degraded: boolean;
  created_at: string;
  finished_at: string;
};

type WorkflowBrief = {
  id: string;
  name: string;
  version: number;
  source: string;
  status: string;
  score: number | null;
  run_count: number;
  success_count: number;
  failure_count: number;
  reuse_key: string;
  previous_workflow_id: string;
};

type WorkflowFeedbackItem = {
  id: string;
  workflow_id: string;
  success: boolean;
  score: number;
  lessons: string[];
  note: string;
  source: string;
  created_at: string;
};

type WorkflowDetail = WorkflowBrief & {
  owner_agent_id: string;
  meta: Record<string, unknown>;
  feedback: WorkflowFeedbackItem[];
};

type CeoSlot = {
  task_id: string;
  title: string;
  status: string;
  agent_id: string;
  agent_name: string;
  progress: number;
  stage: string;
  off_track: boolean;
  result_excerpt: string;
};

type CeoDetail = {
  mission_id: string;
  title: string;
  instruction: string;
  status: string;
  objective: string;
  deliverable: string;
  redlines: string[];
  acceptance: string[];
  terminology: Record<string, string>;
  extra_instructions: string[];
  anchor_version: number;
  coordinator_agent_id: string;
  coordinator_agent_name: string;
  subtask_count: number;
  new_agents: NewAgent[];
  completed_count: number;
  failed_count: number;
  off_track_count: number;
  retry_count: number;
  max_parallel: number;
  effective_parallel: number;
  provider_cap: number;
  degraded: boolean;
  degrade_reason: string;
  stats: Record<string, unknown>;
  report_excerpt: string;
  started_at: string;
  finished_at: string;
  /** CEO 为此场统筹自动生成的工作流（为空表示生成失败，未影响开工） */
  workflow_id: string;
  slots: CeoSlot[];
};

type CeoReport = {
  mission_id: string;
  title: string;
  status: string;
  report: string;
  artifact_id: string | null;
  finished_at: string;
  stats: Record<string, unknown>;
};

type CeoDigest = {
  company_id: string;
  generated_at: string;
  window_hours: number;
  markdown: string;
  stats: Record<string, unknown>;
};

type CeoDigestSchedule = {
  schedule_id: string;
  trigger_config: Record<string, unknown>;
  created: boolean;
};

/* ------------------------------------------------------------------ *
 * 常量与小工具
 * ------------------------------------------------------------------ */

const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];
/** 需要 3 秒轮询详情的中途状态（其余一律不轮询） */
const ACTIVE = ["PLANNING", "RUNNING", "RECLAIMING", "PAUSED"];
const PARALLEL_PRESETS = [3, 5, 8, 10];
const POLL_MS = 3000;

const WF_SOURCE_LABEL: Record<string, string> = {
  ceo: "CEO 自动生成",
  manual: "手工编排",
};

const ACTION_LABEL: Record<"pause" | "resume" | "cancel" | "reclaim", string> = {
  pause: "已暂停",
  resume: "已继续",
  cancel: "已取消",
  reclaim: "已提交补跑",
};

const STATUS_META: Record<string, { label: string; cls: string; dot: string }> = {
  PLANNING: { label: "规划中", cls: "text-purple-300", dot: "bg-purple-400" },
  RUNNING: { label: "执行中", cls: "text-cyan-300", dot: "bg-cyan-400" },
  RECLAIMING: { label: "回收审查", cls: "text-sky-300", dot: "bg-sky-400" },
  PAUSED: { label: "已暂停", cls: "text-amber-300", dot: "bg-amber-400" },
  WAITING: { label: "等待中", cls: "text-yellow-300", dot: "bg-yellow-400" },
  REVIEW: { label: "评审中", cls: "text-blue-300", dot: "bg-blue-400" },
  TODO: { label: "待开工", cls: "text-[var(--muted)]", dot: "bg-gray-500" },
  COMPLETED: { label: "已完成", cls: "text-emerald-300", dot: "bg-emerald-400" },
  FAILED: { label: "失败", cls: "text-red-300", dot: "bg-red-400" },
  CANCELLED: { label: "已取消", cls: "text-[var(--muted)]", dot: "bg-gray-600" },
};

function isTerminal(status?: string | null): boolean {
  return !!status && TERMINAL.includes(status);
}

function isActive(status?: string | null): boolean {
  return !!status && ACTIVE.includes(status);
}

function fmtTime(v?: string | null): string {
  if (!v) return "—";
  return v.slice(0, 16).replace("T", " ");
}

function StatusPill({ status }: { status: string }) {
  const m = STATUS_META[status] ?? { label: status, cls: "text-[var(--muted)]", dot: "bg-gray-500" };
  return (
    <span className={`badge ${m.cls}`} style={{ borderColor: "currentColor", background: "rgba(255,255,255,0.03)" }}>
      <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}

function Section({
  title,
  hint,
  aside,
  children,
}: {
  title: string;
  hint?: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card card-static p-4">
      <div className="flex items-start justify-between gap-3 flex-wrap mb-3">
        <div>
          <h2 className="font-medium text-[var(--foreground)]">{title}</h2>
          {hint && <p className="text-xs mt-0.5 leading-5 text-[var(--muted)]">{hint}</p>}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

function InlineError({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <div className="rounded-lg px-3 py-2 text-xs leading-5 mt-2"
      style={{ border: "1px solid rgba(248,113,113,0.45)", background: "rgba(248,113,113,0.1)", color: "#fca5a5" }}>
      {children}
    </div>
  );
}

function BulletList({ items, tone = "normal" }: { items: string[]; tone?: "normal" | "danger" }) {
  if (!items.length) return <p className="text-xs text-[var(--muted)]">—</p>;
  return (
    <ul className="space-y-1 text-xs leading-5">
      {items.map((t, i) => (
        <li key={i} className="flex gap-2">
          <span aria-hidden style={{ color: tone === "danger" ? "var(--danger)" : "var(--muted)" }}>•</span>
          <span className={tone === "danger" ? "text-red-200" : "text-[var(--foreground)]"}>{t}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * CEO 提议/已招的新员工卡片。
 * 预览阶段是「将新招」（黄框），开工后是「已入职」（绿框），共用一套布局。
 */
function NewAgentCards({ agents, hired }: { agents: NewAgent[]; hired: boolean }) {
  if (!agents.length) return null;
  const tone = hired
    ? { border: "rgba(52,211,153,0.4)", background: "rgba(52,211,153,0.08)", title: "#6ee7b7" }
    : { border: "rgba(251,191,36,0.4)", background: "rgba(251,191,36,0.08)", title: "#fcd34d" };
  return (
    <div>
      <div className="text-[11px] font-medium mb-1" style={{ color: tone.title }}>
        {hired ? `本次新招 ${agents.length} 人（已入职）` : `将新招 ${agents.length} 人（开工时自动入职）`}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {agents.map((a, i) => (
          <div key={`${a.agent_id || a.name}-${i}`} className="rounded-lg px-2.5 py-2 text-xs"
            style={{ border: `1px solid ${tone.border}`, background: tone.background }}>
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="font-medium text-[var(--foreground)]">{a.name}</span>
              {a.role && <span className="text-[10px] text-[var(--muted)]">{a.role}</span>}
              {hired && a.agent_id && (
                <span className="text-[10px] px-1.5 py-0.5 rounded"
                  style={{ background: "rgba(52,211,153,0.14)", color: "#6ee7b7" }}>
                  已入职
                </span>
              )}
            </div>
            <div className="mt-0.5 text-[10px] text-[var(--muted)]">
              部门：{a.department || "—"} · 组：{a.group || "未分组"}
            </div>
            {a.reason && (
              <div className="mt-1 text-[10px] leading-4 text-[var(--muted)] line-clamp-2">理由：{a.reason}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 页面
 * ------------------------------------------------------------------ */

export default function CeoPage() {
  const cid = useActiveCompanyId();

  /* ------ 全局：并发档位（被容量条、拆解、开工共用） ------ */
  const [parallel, setParallel] = useState(3);

  /* ------ 1. CEO 指派 ------ */
  const config = useResource<CeoConfig>(
    cid ? `ceo:config:${cid}` : null,
    () => api.get<CeoConfig>("/ceo/config"),
  );
  const [pickAgentId, setPickAgentId] = useState("");
  const [assigning, setAssigning] = useState(false);
  const [assignError, setAssignError] = useState("");

  /* ------ 2. 并发容量 ------ */
  const capacity = useResource<CeoCapacity>(
    cid ? `ceo:capacity:${cid}:${parallel}` : null,
    () => api.get<CeoCapacity>(`/ceo/capacity?requested=${parallel}`),
  );

  /* ------ 3. 一句话下目标 ------ */
  const [instruction, setInstruction] = useState("");
  const [planning, setPlanning] = useState(false);
  const [createError, setCreateError] = useState("");
  const [preview, setPreview] = useState<CeoPreview | null>(null);
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState("");

  /* ------ 4. 统筹列表 + 作战视图 ------ */
  const missions = useResource<CeoMissionItem[]>(
    cid ? `ceo:missions:${cid}` : null,
    () => api.get<CeoMissionItem[]>("/ceo/missions?limit=30"),
  );
  // `?? []` 每次都是新数组，会让下游 effect 每帧重跑——用 useMemo 固定引用
  const missionList: CeoMissionItem[] = useMemo(() => missions.data ?? [], [missions.data]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CeoDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailTick, setDetailTick] = useState(0);
  const [acting, setActing] = useState<string | null>(null);
  const [instr, setInstr] = useState("");
  const [appending, setAppending] = useState(false);
  const [instrError, setInstrError] = useState("");

  // 列表刷新函数放进 ref：详情轮询里发现状态变化时也能安全调用
  const listRefreshRef = useRef(missions.refresh);
  listRefreshRef.current = missions.refresh;

  // 深链：/ceo?mission=<id>（后端完成后通知里就是这个链接）
  const deepLinkRef = useRef<string | null>(null);
  useEffect(() => {
    const m = new URLSearchParams(window.location.search).get("mission");
    if (m) {
      deepLinkRef.current = m;
      setSelectedId(m);
    }
  }, []);

  // 切公司：旧公司的 mission 不能继续显示（否则详情 404 / 张冠李戴）
  const prevCidRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (prevCidRef.current !== undefined && prevCidRef.current !== cid) setSelectedId(null);
    prevCidRef.current = cid;
  }, [cid]);

  // 换选中项时清空上一场的详情（手动刷新 detailTick 不清，避免闪一下空态）
  useEffect(() => {
    setDetail(null);
    setDetailError("");
  }, [selectedId]);

  // 没选中且不是深链进来时，默认看最近一场
  useEffect(() => {
    if (selectedId || deepLinkRef.current) return;
    const first = missionList[0];
    if (first) setSelectedId(first.mission_id);
  }, [missionList, selectedId]);

  // 详情轮询：立即拉一次，未到终态才排下一次；卸载/切换时 abort + clearTimeout
  useEffect(() => {
    if (!cid || !selectedId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sawActive = false;
    const abort = new AbortController();

    const tick = async () => {
      try {
        const d = await api.get<CeoDetail>(`/ceo/missions/${selectedId}`, { signal: abort.signal });
        if (!alive) return;
        setDetail(d);
        setDetailError("");
        if (isActive(d.status)) {
          sawActive = true;
          timer = setTimeout(tick, POLL_MS);
        } else {
          // 从"跑到结束"的那一刻把列表的数字也刷新掉
          if (sawActive) listRefreshRef.current();
          sawActive = false;
        }
      } catch (e) {
        if (!alive || isCancelled(e)) return;
        setDetailError(describeError(e, "任务详情加载失败"));
        // 只有网络/服务端故障才自动重试；404/403 这类"再试也一样"的错误不空转
        const status = e instanceof ApiError ? e.status : 500;
        if (status === 0 || status >= 500) timer = setTimeout(tick, POLL_MS * 2);
      }
    };
    void tick();

    return () => {
      alive = false;
      abort.abort();
      if (timer) clearTimeout(timer);
    };
  }, [cid, selectedId, detailTick]);

  /* ------ 5. 最终报告（仅终态才拉） ------ */
  const selectedStatus = detail && detail.mission_id === selectedId ? detail.status : null;
  const report = useResource<CeoReport>(
    cid && selectedId && isTerminal(selectedStatus)
      ? `ceo:report:${cid}:${selectedId}:${selectedStatus}`
      : null,
    () => api.get<CeoReport>(`/ceo/missions/${selectedId}/report`),
  );

  /* ------ 5.5 工作流（CEO 自动生成，可人工反馈升级） ------ */
  const activeWorkflowId = detail && detail.mission_id === selectedId ? detail.workflow_id : "";
  const workflow = useResource<WorkflowDetail>(
    cid && activeWorkflowId ? `workflow:${cid}:${activeWorkflowId}` : null,
    () => api.get<WorkflowDetail>(`/workflows/${activeWorkflowId}`),
  );
  const [wfScore, setWfScore] = useState(80);
  const [wfLessons, setWfLessons] = useState("");
  const [wfBusy, setWfBusy] = useState(false);
  const [wfError, setWfError] = useState("");
  const [wfOptimized, setWfOptimized] = useState<WorkflowBrief | null>(null);

  useEffect(() => {
    setWfOptimized(null);
    setWfError("");
    setWfLessons("");
  }, [activeWorkflowId]);

  async function submitWorkflowFeedback() {
    const current = workflow.data;
    if (!current || wfBusy) return;
    setWfBusy(true);
    setWfError("");
    try {
      const lessons = wfLessons.split("\n").map((s) => s.trim()).filter(Boolean);
      const res = await api.post<{ workflow: WorkflowBrief; optimized_from: string }>(
        `/workflows/${current.id}/feedback`,
        { score: wfScore, lessons, note: "CEO 指挥台人工复盘", optimize: true },
      );
      setWfLessons("");
      workflow.refresh();
      if (res.optimized_from) {
        setWfOptimized(res.workflow);
        notify("success", `反馈已记录；低于 80 分，已生成工作流 v${res.workflow.version}`);
      } else {
        setWfOptimized(null);
        notify("success", "反馈已记录");
      }
    } catch (e) {
      setWfError(describeError(e, "提交工作流反馈失败"));
    } finally {
      setWfBusy(false);
    }
  }

  /* ------ 6. CEO 日报 ------ */
  const digest = useResource<CeoDigest>(
    cid ? `ceo:digest:${cid}` : null,
    () => api.get<CeoDigest>("/ceo/digest"),
  );
  const [digestBusy, setDigestBusy] = useState<"run" | "schedule" | null>(null);
  const [digestError, setDigestError] = useState("");
  const [schedule, setSchedule] = useState<CeoDigestSchedule | null>(null);

  /* ------ 动作 ------ */

  async function assignCeo() {
    if (!cid || !pickAgentId) return;
    const switching = !!config.data?.has_ceo;
    if (switching) {
      const ok = await confirmAction({
        title: "更换 CEO？",
        description: "新的 CEO 会接管之后所有统筹；进行中的任务不受影响。",
        confirmText: "更换",
        danger: false,
      });
      if (!ok) return;
    }
    setAssigning(true);
    setAssignError("");
    try {
      const res = await api.post<{ ceo_agent_id: string; ceo_agent_name: string }>("/ceo/config", {
        company_id: cid,
        agent_id: pickAgentId,
      });
      // 以响应为准：卡片上显示的永远是服务端确认的那个人
      notify("success", `已指定 ${res.ceo_agent_name} 为 CEO`);
      config.refresh();
      capacity.refresh(); // 负责人的供应商档位可能变了 → 三层容量重算
      setPickAgentId("");
    } catch (e) {
      setAssignError(describeError(e, "指定 CEO 失败"));
    } finally {
      setAssigning(false);
    }
  }

  async function planMission() {
    if (!cid || !instruction.trim() || planning) return;
    setPlanning(true);
    setCreateError("");
    setLaunchError("");
    setPreview(null);
    try {
      const p = await api.post<CeoPreview>("/ceo/missions", {
        company_id: cid,
        instruction: instruction.trim(),
        max_parallel: parallel,
      });
      setPreview(p); // 只是预览：此时还没有任何任务被执行
      missions.refresh(); // 列表里会多一条「规划中」
    } catch (e) {
      setCreateError(describeError(e, "CEO 拆解失败"));
    } finally {
      setPlanning(false);
    }
  }

  async function launchMission() {
    if (!preview || launching) return;
    setLaunching(true);
    setLaunchError("");
    try {
      const res = await api.post<{
        mission_id: string;
        status: string;
        subtask_count: number;
        effective_parallel: number;
        provider_cap: number;
        new_agents: NewAgent[];
        workflow?: WorkflowBrief | null;
      }>(`/ceo/missions/${preview.mission_id}/launch`, { max_parallel: parallel });
      setPreview(null);
      setInstruction("");
      setSelectedId(res.mission_id);
      setDetailTick((n) => n + 1); // 立刻开始盯这一场
      missions.refresh();
      const hired = res.new_agents?.length ?? 0;
      notify(
        "success",
        `已开工：${res.subtask_count} 个子任务，实际并发 ${res.effective_parallel}（供应商上限 ${res.provider_cap}）` +
          (hired > 0 ? `；新招 ${hired} 人已入职` : "") +
          (res.workflow ? `；工作流 v${res.workflow.version} 已生成` : ""),
      );
    } catch (e) {
      setLaunchError(describeError(e, "开工失败"));
    } finally {
      setLaunching(false);
    }
  }

  async function act(kind: "pause" | "resume" | "cancel" | "reclaim") {
    if (!selectedId || acting) return;
    if (kind === "cancel") {
      const ok = await confirmAction({
        title: "取消这场统筹？",
        description: "还在排队 / 未开始的子任务会被取消；已完成的结果与报告会保留。",
        confirmText: "取消统筹",
      });
      if (!ok) return;
    }
    if (kind === "reclaim") {
      const ok = await confirmAction({
        title: "回收补跑？",
        description: `会把没交付或偏离的子任务再补跑一次，然后重新汇总报告。${
          detail ? `这场的自动补跑次数：${detail.retry_count} 次。` : ""
        }`,
        confirmText: "开始补跑",
        danger: false,
      });
      if (!ok) return;
    }
    setActing(kind);
    try {
      const res = await api.post<{ status: string; retry_tasks?: number; cancelled_tasks?: number }>(
        `/ceo/missions/${selectedId}/${kind}`,
      );
      const extra =
        kind === "reclaim" && typeof res.retry_tasks === "number" ? `，补跑 ${res.retry_tasks} 个子任务`
        : kind === "cancel" && typeof res.cancelled_tasks === "number" ? `，取消 ${res.cancelled_tasks} 个子任务`
        : "";
      notify("success", `${ACTION_LABEL[kind]}：${STATUS_META[res.status]?.label ?? res.status}${extra}`);
      setDetailTick((n) => n + 1);
      missions.refresh();
    } catch (e) {
      notify("error", describeError(e, `${ACTION_LABEL[kind]}失败`));
    } finally {
      setActing(null);
    }
  }

  async function appendInstruction() {
    if (!selectedId || !instr.trim() || appending) return;
    setAppending(true);
    setInstrError("");
    try {
      const res = await api.post<{ anchor_version: number; extra_instructions: string[] }>(
        `/ceo/missions/${selectedId}/instruction`,
        { text: instr.trim() },
      );
      // 用响应回填：准绳版本 +1、指令列表可见
      setDetail((d) =>
        d ? { ...d, anchor_version: res.anchor_version, extra_instructions: res.extra_instructions } : d,
      );
      setInstr("");
      notify("success", `已追加指令，准绳升级到 v${res.anchor_version}`);
    } catch (e) {
      setInstrError(describeError(e, "追加指令失败"));
    } finally {
      setAppending(false);
    }
  }

  async function runDigest() {
    if (!cid || digestBusy) return;
    setDigestBusy("run");
    setDigestError("");
    try {
      const res = await api.post<CeoDigest>("/ceo/digest/run");
      digest.mutate(res); // 用返回的日报直接替换卡片内容
      notify("success", "CEO 日报已生成");
    } catch (e) {
      setDigestError(describeError(e, "生成日报失败"));
    } finally {
      setDigestBusy(null);
    }
  }

  async function scheduleDigest() {
    if (!cid || digestBusy) return;
    setDigestBusy("schedule");
    setDigestError("");
    try {
      const res = await api.post<CeoDigestSchedule>("/ceo/digest/schedule", {
        company_id: cid,
        time: "09:00",
      });
      setSchedule(res);
      notify(res.created ? "success" : "info",
        res.created ? "已创建：每天 09:00 推送 CEO 日报" : "已存在，不会重复创建");
    } catch (e) {
      setDigestError(describeError(e, "创建日报定时失败"));
    } finally {
      setDigestBusy(null);
    }
  }

  /* ------ 渲染 ------ */

  const cfg = config.data;
  const cap = capacity.data;
  const activeDetail = detail && detail.mission_id === selectedId ? detail : null;
  const createDisabled = !cid || !cfg?.has_ceo || !instruction.trim() || planning;

  return (
    <div className="p-6 max-w-[1500px] mx-auto animate-fade-up space-y-4">
      {/* 页头 */}
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-[var(--foreground)]">👑 CEO 指挥台</h1>
          <p className="text-xs mt-1 text-[var(--muted)]">
            一句话交给 CEO：拆准绳、分到人、按并发跑、盯偏离、收报告。你只需要在下目标和开工时点头。
          </p>
        </div>
        <button
          type="button"
          onClick={() => { config.refresh(); capacity.refresh(); missions.refresh(); digest.refresh(); }}
          className="btn-ghost text-xs"
          disabled={!cid}
        >
          刷新
        </button>
      </div>

      {cid === null && (
        <div className="rounded-xl border px-3.5 py-2.5 text-sm"
          style={{ borderColor: "rgba(251,191,36,0.45)", background: "rgba(251,191,36,0.1)" }}>
          ⚠️ 你还没有创建 AI 公司，请先到「公司管理」创建。
        </div>
      )}

      {/* ---------- 1. CEO 指派卡 ---------- */}
      <Section
        title="① 先指定 CEO"
        hint="CEO 是这场统筹的负责人：它负责拆解目标、分派子任务、汇总最终报告。没有 CEO 就不能下目标。"
      >
        {config.error && <InlineError>读取 CEO 配置失败：{config.error}</InlineError>}
        {!config.error && !cfg && cid !== null && (
          <p className="text-xs text-[var(--muted)]">正在读取 CEO 配置…</p>
        )}

        {cfg && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 flex-wrap text-sm">
              {cfg.has_ceo ? (
                <>
                  <span className="badge" style={{ borderColor: "rgba(52,211,153,0.5)", color: "#6ee7b7" }}>
                    ✓ 已指定
                  </span>
                  <span className="font-medium text-[var(--foreground)]">{cfg.ceo_agent_name}</span>
                  <span className="text-xs text-[var(--muted)]">当前 CEO · 之后所有统筹都由它负责</span>
                </>
              ) : (
                <>
                  <span className="badge" style={{ borderColor: "rgba(251,191,36,0.5)", color: "#fcd34d" }}>
                    ! 未指定
                  </span>
                  <span className="text-[var(--foreground)]">本公司还没有指定 CEO</span>
                  <span className="text-xs text-[var(--muted)]">先从下面选一个人</span>
                </>
              )}
            </div>

            {cfg.candidates.length === 0 ? (
              <p className="text-xs text-[var(--muted)]">
                这家公司还没有员工。先到「Agent 团队」招人，再回来指定 CEO。
              </p>
            ) : (
              <div className="flex items-center gap-2 flex-wrap">
                <select
                  className="input w-auto min-w-[220px] !py-1.5 text-xs"
                  value={pickAgentId}
                  onChange={(e) => setPickAgentId(e.target.value)}
                >
                  <option value="">{cfg.has_ceo ? "选择要换成谁…" : "选择一位员工…"}</option>
                  {cfg.candidates
                    .filter((a) => a.id !== cfg.ceo_agent_id)
                    .map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}{a.role ? ` · ${a.role}` : ""}
                      </option>
                    ))}
                </select>
                <button
                  type="button"
                  onClick={assignCeo}
                  disabled={!pickAgentId || assigning}
                  className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40"
                >
                  {assigning ? "指派中…" : cfg.has_ceo ? "更换 CEO" : "指定为 CEO"}
                </button>
              </div>
            )}
            <InlineError>{assignError}</InlineError>
          </div>
        )}
      </Section>

      {/* ---------- 2. 并发容量条 ---------- */}
      <Section
        title="② 并发容量"
        hint="实际生效 = 三层取最小：你设置、供应商上限、系统余量。服务端按负责人主模型的供应商档位封顶——你点了 10，也可能只跑 4。"
        aside={<span className="text-xs text-[var(--muted)]">可选范围 3–10（保底 2 不在界面提供）</span>}
      >
        {capacity.error && <InlineError>读取容量失败：{capacity.error}</InlineError>}

        {cap && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              {[
                { k: "你设置", v: cap.requested, hint: `默认 ${cap.default_parallel}` },
                { k: "供应商上限", v: cap.provider_cap, hint: cap.provider_type || "unknown" },
                { k: "系统余量", v: cap.worker_free, hint: "后台 worker 空槽" },
                { k: "实际生效", v: cap.effective, hint: `三层取最小 · 硬上限 ${cap.hard_cap}` },
              ].map((x, i) => (
                <div
                  key={x.k}
                  className="rounded-xl px-3 py-2.5 border"
                  style={{
                    borderColor: i === 3 ? "var(--accent)" : "var(--border)",
                    background: i === 3 ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)",
                  }}
                >
                  <div className="text-[10px] tracking-wide text-[var(--muted)]">
                    {x.k}
                  </div>
                  <div className="text-xl font-bold tabular-nums text-[var(--foreground)]">{x.v}</div>
                  <div className="text-[10px] text-[var(--muted)] truncate">{x.hint}</div>
                </div>
              ))}
            </div>

            <div className="flex items-center gap-3 flex-wrap">
              <input
                type="range"
                min={3}
                max={10}
                step={1}
                value={parallel}
                onChange={(e) => setParallel(Number(e.target.value))}
                aria-label="期望并发数"
                className="w-40 accent-indigo-500"
              />
              <div className="flex gap-1.5">
                {PARALLEL_PRESETS.map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setParallel(n)}
                    className={`px-2.5 py-1 rounded-lg text-xs border transition-colors ${
                      parallel === n ? "bg-indigo-500/20 border-indigo-400 text-indigo-100 font-medium" : ""
                    }`}
                    style={parallel === n ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}
                  >
                    {n}
                  </button>
                ))}
              </div>
              <span className="text-xs text-[var(--muted)]">
                当前 {parallel} 路 → 实际生效 {cap.effective} 路
                {cap.effective < parallel && "（被供应商上限或工作台余量压住了）"}
              </span>
            </div>
          </div>
        )}
      </Section>

      {/* ---------- 3. 一句话下目标 ---------- */}
      <Section
        title="③ 一句话下目标"
        hint="CEO 会先给你看拆解结果（准绳 + 分工），你说「开工」才真正执行。"
        aside={<span className="text-xs text-[var(--muted)]">{instruction.length}/4000</span>}
      >
        {!cfg?.has_ceo && (
          <p className="text-xs mb-2" style={{ color: "#fcd34d" }}>
            还没指定 CEO，暂时不能下目标。
          </p>
        )}
        <textarea
          rows={3}
          maxLength={4000}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="例：把本周的客户回访做完，输出每位客户的风险分层和下周跟进动作"
          className="input resize-y"
          disabled={!cid}
        />
        <div className="mt-2.5 flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={planMission}
            disabled={createDisabled}
            title={!cfg?.has_ceo ? "请先指定 CEO" : undefined}
            className="btn-primary disabled:opacity-40"
          >
            {planning ? "CEO 拆解中…（可能要 1 分钟）" : "让 CEO 拆解"}
          </button>
          <span className="text-xs text-[var(--muted)]">
            拆解用 {parallel} 路并发档位；只预览，不执行。
          </span>
        </div>
        <InlineError>{createError}</InlineError>

        {preview && (
          <div className="mt-4 rounded-xl border p-3.5 space-y-3"
            style={{ borderColor: "rgba(99,102,241,0.4)", background: "rgba(99,102,241,0.06)" }}>
            <div className="flex items-start justify-between gap-2 flex-wrap">
              <div>
                <div className="text-sm font-semibold text-[var(--foreground)]">{preview.title}</div>
                <div className="text-[11px] mt-0.5 text-[var(--muted)]">
                  CEO：{preview.coordinator_agent_name || "—"} · 子任务 {preview.subtask_count} 个 ·
                  实际并发 {preview.effective_parallel}（供应商上限 {preview.provider_cap}
                  {preview.provider_type ? ` · ${preview.provider_type}` : ""}）
                </div>
              </div>
              <StatusPill status={preview.status} />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">目标</div>
                <p className="text-xs leading-5 text-[var(--foreground)] whitespace-pre-wrap">{preview.objective || "—"}</p>
              </div>
              <div>
                <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">交付物</div>
                <p className="text-xs leading-5 text-[var(--foreground)] whitespace-pre-wrap">{preview.deliverable || "—"}</p>
              </div>
              <div>
                <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">红线（不许越界）</div>
                <BulletList items={preview.redlines} tone="danger" />
              </div>
              <div>
                <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">验收标准</div>
                <BulletList items={preview.acceptance} />
              </div>
            </div>

            {Object.keys(preview.terminology || {}).length > 0 && (
              <div>
                <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">术语约定</div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(preview.terminology).map(([k, v]) => (
                    <span key={k} className="text-[11px] px-2 py-0.5 rounded-md"
                      style={{ background: "rgba(255,255,255,0.05)", border: "1px solid var(--border)" }}>
                      <b className="text-[var(--foreground)]">{k}</b>
                      <span className="text-[var(--muted)]">：{v}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div>
              <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">
                分工方案（{preview.subtasks.length} 个子任务）
              </div>
              <div className="space-y-1.5">
                {preview.subtasks.map((s) => (
                  <div key={`${s.index}-${s.title}`}
                    className="flex items-start gap-2 rounded-lg px-2.5 py-1.5 text-xs"
                    style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                    <span className="tabular-nums shrink-0 text-[var(--muted)]">{s.index}.</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-[var(--foreground)]">{s.title}</div>
                      {s.description && (
                        <div className="text-[10px] mt-0.5 text-[var(--muted)] line-clamp-2">{s.description}</div>
                      )}
                    </div>
                    <span className="shrink-0 text-[11px] text-[var(--muted)]">{s.agent_name || "未分配"}</span>
                    {!s.agent_matched && (
                      <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded"
                        style={
                          s.new_agent_index != null
                            ? { background: "rgba(99,102,241,0.14)", color: "#c7d2fe", border: "1px solid rgba(99,102,241,0.4)" }
                            : { background: "rgba(251,191,36,0.14)", color: "#fcd34d", border: "1px solid rgba(251,191,36,0.4)" }
                        }>
                        {s.new_agent_index != null ? "待招新人" : "未精确匹配"}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>

            <NewAgentCards agents={preview.new_agents} hired={false} />

            <div className="flex items-center gap-2 flex-wrap pt-1">
              <button
                type="button"
                onClick={launchMission}
                disabled={launching}
                className="btn-primary disabled:opacity-40"
              >
                {launching ? "开工中…" : "🚀 开工"}
              </button>
              <button
                type="button"
                onClick={() => { setPreview(null); setLaunchError(""); }}
                disabled={launching}
                className="btn-ghost text-xs"
              >
                改一下目标
              </button>
              <span className="text-[11px] text-[var(--muted)]">
                开工后按当前容量 {parallel} 路执行，期间可暂停 / 追加指令。
              </span>
            </div>
            <InlineError>{launchError}</InlineError>
          </div>
        )}
      </Section>

      {/* ---------- 4. 统筹列表 + 作战视图 ---------- */}
      <div className="grid grid-cols-1 lg:grid-cols-[320px_minmax(0,1fr)] gap-4 items-start">
        <section className="card card-static p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-medium text-[var(--foreground)]">④ 统筹列表</h2>
            <span className="badge text-xs" style={{ borderColor: "var(--border)" }}>{missionList.length} 场</span>
          </div>
          {missions.error && <InlineError>列表加载失败：{missions.error}</InlineError>}
          {!missions.error && missions.loading && !missions.data && (
            <p className="text-xs text-[var(--muted)]">正在加载…</p>
          )}
          {!missions.error && !missions.loading && missionList.length === 0 && (
            <p className="text-xs text-[var(--muted)] leading-5">
              还没有统筹。用上面的「让 CEO 拆解」下第一句话。
            </p>
          )}
          <div className="space-y-1.5 lg:max-h-[620px] overflow-y-auto pr-0.5">
            {missionList.map((m) => {
              const on = m.mission_id === selectedId;
              return (
                <button
                  key={m.mission_id}
                  type="button"
                  onClick={() => setSelectedId(m.mission_id)}
                  className="w-full text-left rounded-xl px-3 py-2.5 border transition-colors"
                  style={{
                    borderColor: on ? "var(--accent)" : "var(--border)",
                    background: on ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)",
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium text-[var(--foreground)] line-clamp-2 leading-snug">{m.title}</span>
                    <StatusPill status={m.status} />
                  </div>
                  <div className="mt-1 flex items-center gap-2 flex-wrap text-[10px] text-[var(--muted)]">
                    <span className="tabular-nums">
                      完成 {m.completed_count}/{m.subtask_count}
                    </span>
                    {m.off_track_count > 0 && <span style={{ color: "#fcd34d" }}>偏离 {m.off_track_count}</span>}
                    {m.failed_count > 0 && <span style={{ color: "#fca5a5" }}>失败 {m.failed_count}</span>}
                    <span>并发 {m.effective_parallel}</span>
                    {m.degraded && <span style={{ color: "#fcd34d" }}>已降档</span>}
                    <span className="ml-auto">{fmtTime(m.created_at)}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        {/* 作战视图 */}
        <section className="card card-static p-4">
          {!selectedId && (
            <p className="text-xs text-[var(--muted)] py-10 text-center">在左侧选一场统筹，看它的准绳与执行进度。</p>
          )}

          {selectedId && !activeDetail && !detailError && (
            <p className="text-xs text-[var(--muted)] py-10 text-center">正在读取这场统筹…</p>
          )}

          {detailError && (
            <div className="space-y-2">
              <InlineError>{detailError}</InlineError>
              <button type="button" onClick={() => setDetailTick((n) => n + 1)} className="btn-ghost text-xs">
                重试
              </button>
            </div>
          )}

          {activeDetail && (
            <div className="space-y-4">
              {/* 头部：状态 / 负责人 / 容量 / 降档 */}
              <div className="flex items-start justify-between gap-3 flex-wrap">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h2 className="font-semibold text-[var(--foreground)]">{activeDetail.title}</h2>
                    <StatusPill status={activeDetail.status} />
                    {isActive(activeDetail.status) && (
                      <span className="text-[10px] text-[var(--muted)]">每 3 秒刷新</span>
                    )}
                  </div>
                  <p className="text-[11px] mt-1 text-[var(--muted)]">
                    CEO：{activeDetail.coordinator_agent_name || "—"} · 准绳 v{activeDetail.anchor_version} ·
                    并发 {activeDetail.effective_parallel}/{activeDetail.max_parallel}（供应商上限 {activeDetail.provider_cap}）·
                    完成 {activeDetail.completed_count}/{activeDetail.subtask_count}
                    {activeDetail.off_track_count > 0 && ` · 偏离 ${activeDetail.off_track_count}`}
                    {activeDetail.failed_count > 0 && ` · 失败 ${activeDetail.failed_count}`}
                    {activeDetail.retry_count > 0 && ` · 已补跑 ${activeDetail.retry_count} 次`}
                  </p>
                </div>
                <button type="button" onClick={() => setDetailTick((n) => n + 1)} className="btn-ghost text-xs">
                  ↻ 刷新
                </button>
              </div>

              {activeDetail.degraded && (
                <div className="rounded-lg px-3 py-2 text-xs leading-5"
                  style={{ border: "1px solid rgba(251,191,36,0.45)", background: "rgba(251,191,36,0.1)", color: "#fcd34d" }}>
                  ⚠️ 已自动降档到 {activeDetail.effective_parallel} 路：{activeDetail.degrade_reason || "上游限流或超时"}
                </div>
              )}

              {/* 准绳 */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">目标</div>
                  <p className="text-xs leading-5 text-[var(--foreground)] whitespace-pre-wrap">{activeDetail.objective || "—"}</p>
                </div>
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">交付物</div>
                  <p className="text-xs leading-5 text-[var(--foreground)] whitespace-pre-wrap">{activeDetail.deliverable || "—"}</p>
                </div>
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">红线</div>
                  <BulletList items={activeDetail.redlines} tone="danger" />
                </div>
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">验收标准</div>
                  <BulletList items={activeDetail.acceptance} />
                </div>
              </div>

              {Object.keys(activeDetail.terminology || {}).length > 0 && (
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">术语约定</div>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(activeDetail.terminology).map(([k, v]) => (
                      <span key={k} className="text-[11px] px-2 py-0.5 rounded-md"
                        style={{ background: "rgba(255,255,255,0.05)", border: "1px solid var(--border)" }}>
                        <b className="text-[var(--foreground)]">{k}</b>
                        <span className="text-[var(--muted)]">：{v}</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <NewAgentCards agents={activeDetail.new_agents} hired />

              {/* 工作流：开工时由 CEO 按知识库 + 网络调研生成，收尾按反馈升版 */}
              {activeDetail.workflow_id && (
                <div className="rounded-xl p-3 space-y-2"
                  style={{ border: "1px solid rgba(34,211,238,0.3)", background: "rgba(34,211,238,0.05)" }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[11px] font-medium" style={{ color: "#67e8f9" }}>
                      🧭 工作流（CEO 自动生成）
                    </span>
                    {workflow.data && (
                      <>
                        <span className="text-[10px] px-1.5 py-0.5 rounded"
                          style={{ background: "rgba(34,211,238,0.14)", color: "#a5f3fc" }}>
                          v{workflow.data.version}
                        </span>
                        <span className="text-[10px] text-[var(--muted)]">
                          {WF_SOURCE_LABEL[workflow.data.source] || workflow.data.source}
                        </span>
                        <span className="text-[10px] text-[var(--muted)]">
                          评分 {workflow.data.score == null ? "—" : workflow.data.score.toFixed(1)}
                        </span>
                        <span className="text-[10px] text-[var(--muted)]">
                          跑过 {workflow.data.run_count} 次 · 成功 {workflow.data.success_count} · 失败 {workflow.data.failure_count}
                        </span>
                        <Link
                          href={`/workflow-builder?open=${workflow.data.id}`}
                          className="text-[10px] px-2 py-0.5 rounded ml-auto"
                          style={{ border: "1px solid rgba(34,211,238,0.4)", color: "#a5f3fc" }}
                        >
                          查看流程图 →
                        </Link>
                      </>
                    )}
                  </div>

                  {workflow.error && <InlineError>工作流读取失败：{workflow.error}</InlineError>}
                  {!workflow.error && workflow.loading && !workflow.data && (
                    <p className="text-xs text-[var(--muted)]">正在读取工作流…</p>
                  )}

                  {workflow.data && (
                    <>
                      <p className="text-[11px] leading-5 text-[var(--muted)]">
                        {(workflow.data.reuse_key
                          ? `复用键「${workflow.data.reuse_key}」`
                          : "一次性流程") +
                          (workflow.data.previous_workflow_id ? " · 由上一版反馈升级而来" : "")}
                      </p>

                      {wfOptimized && (
                        <div className="text-[11px] rounded-md px-2 py-1"
                          style={{ background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.35)", color: "#6ee7b7" }}>
                          已根据本次反馈生成 v{wfOptimized.version}：
                          <Link href={`/workflow-builder?open=${wfOptimized.id}`} className="underline ml-1">
                            打开新版 →
                          </Link>
                        </div>
                      )}

                      {isTerminal(activeDetail.status) && (
                        <div className="space-y-1">
                          <div className="text-[10px] text-[var(--muted)]">
                            给这套流程打分（低于 80 分自动升一版，并把经验写进下一版指令）
                          </div>
                          <div className="flex items-center gap-2 flex-wrap">
                            <input
                              type="range"
                              min={0}
                              max={100}
                              step={5}
                              value={wfScore}
                              onChange={(e) => setWfScore(Number(e.target.value))}
                              className="w-36"
                              aria-label="工作流评分"
                            />
                            <span className="tabular-nums text-xs text-[var(--foreground)] w-8">{wfScore}</span>
                            <input
                              value={wfLessons}
                              onChange={(e) => setWfLessons(e.target.value)}
                              placeholder="经验教训，一行一条（可空）"
                              className="input !py-1 text-xs flex-1 min-w-[180px]"
                            />
                            <button
                              type="button"
                              onClick={submitWorkflowFeedback}
                              disabled={wfBusy}
                              className="btn-primary !py-1 !px-3 text-xs disabled:opacity-40 shrink-0"
                            >
                              {wfBusy ? "提交中…" : "提交反馈"}
                            </button>
                          </div>
                          <InlineError>{wfError}</InlineError>
                        </div>
                      )}

                      {workflow.data.feedback.length > 0 && (
                        <div className="space-y-1 pt-0.5">
                          {workflow.data.feedback.slice(0, 3).map((f) => (
                            <div key={f.id} className="text-[10px] rounded-md px-2 py-1"
                              style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                              <span style={{ color: f.success ? "#6ee7b7" : "#fca5a5" }}>
                                {f.score.toFixed(0)} 分
                              </span>
                              <span className="text-[var(--muted)]">
                                {" "}· {f.source === "manual" ? "人工反馈" : "CEO 自动评分"} · {fmtTime(f.created_at)}
                              </span>
                              {f.lessons.length > 0 && (
                                <span className="text-[var(--foreground)]"> — {f.lessons.join("；")}</span>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}

              {/* 追加指令（准绳升级） */}
              {!isTerminal(activeDetail.status) && (
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">
                    追加指令（写入准绳，版本 +1）
                  </div>
                  <div className="flex gap-2 items-start">
                    <input
                      value={instr}
                      maxLength={1000}
                      onChange={(e) => setInstr(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void appendInstruction(); } }}
                      placeholder="例：交付物必须是表格，不要长文"
                      className="input !py-1.5 text-xs flex-1"
                    />
                    <button
                      type="button"
                      onClick={appendInstruction}
                      disabled={!instr.trim() || appending}
                      className="btn-primary !py-1.5 !px-3 text-xs disabled:opacity-40 shrink-0"
                    >
                      {appending ? "追加中…" : "追加"}
                    </button>
                  </div>
                  <InlineError>{instrError}</InlineError>
                  {activeDetail.extra_instructions.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {activeDetail.extra_instructions.map((t, i) => (
                        <div key={`${i}-${t.slice(0, 8)}`} className="text-[11px] rounded-md px-2 py-1"
                          style={{ background: "rgba(99,102,241,0.08)", border: "1px solid rgba(99,102,241,0.25)", color: "#c7d2fe" }}>
                          追加 {i + 1}：{t}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* 作战视图：slots 表格 */}
              <div>
                <div className="text-[11px] font-medium mb-1.5 text-[var(--muted)]">
                  作战视图（{activeDetail.slots.length} 条子任务）
                </div>
                {activeDetail.slots.length === 0 ? (
                  <p className="text-xs text-[var(--muted)] py-4 text-center">
                    {activeDetail.status === "PLANNING"
                      ? "还没开工：这份拆解只是预览，没有正在执行的子任务。"
                      : "暂无子任务记录。"}
                  </p>
                ) : (
                  <div className="overflow-auto max-h-[420px] rounded-lg" style={{ border: "1px solid var(--border)" }}>
                    <table className="w-full text-xs">
                      <thead className="text-[var(--muted)]" style={{ background: "rgba(255,255,255,0.02)" }}>
                        <tr className="text-left">
                          <th className="py-2 pl-2.5 pr-2 font-normal">子任务</th>
                          <th className="py-2 pr-2 font-normal whitespace-nowrap">执行人</th>
                          <th className="py-2 pr-2 font-normal whitespace-nowrap">状态</th>
                          <th className="py-2 pr-2 font-normal">阶段或偏离原因</th>
                          <th className="py-2 pr-2.5 font-normal w-28">进度</th>
                        </tr>
                      </thead>
                      <tbody className="text-[var(--foreground)]">
                        {activeDetail.slots.map((s) => (
                          <tr
                            key={s.task_id}
                            style={{
                              borderTop: "1px solid var(--border)",
                              background: s.off_track ? "rgba(251,191,36,0.1)" : undefined,
                              boxShadow: s.off_track ? "inset 3px 0 0 #fbbf24" : undefined,
                            }}
                          >
                            <td className="py-2 pl-2.5 pr-2 max-w-[260px]">
                              <div className="truncate" title={s.title}>{s.title}</div>
                              {s.off_track && (
                                <span className="text-[10px]" style={{ color: "#fcd34d" }}>⚠ 偏离准绳</span>
                              )}
                            </td>
                            <td className="py-2 pr-2 whitespace-nowrap">{s.agent_name || "未分配"}</td>
                            <td className="py-2 pr-2 whitespace-nowrap">
                              <StatusPill status={s.status} />
                            </td>
                            <td className="py-2 pr-2 max-w-[240px]">
                              <span className="line-clamp-2" style={{ color: s.off_track ? "#fcd34d" : "var(--muted)" }}>
                                {s.stage
                                  ? (s.off_track ? s.stage.replace(/^off_track:\s*/, "") : s.stage)
                                  : "—"}
                              </span>
                            </td>
                            <td className="py-2 pr-2.5">
                              <div className="flex items-center gap-1.5">
                                <div className="flex-1 h-1 rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.06)" }}>
                                  <div
                                    className="h-full rounded-full"
                                    style={{
                                      width: `${Math.min(100, Math.max(0, s.progress * 100))}%`,
                                      background: s.off_track ? "#fbbf24" : "linear-gradient(90deg,#6366f1,#22d3ee)",
                                    }}
                                  />
                                </div>
                                <span className="tabular-nums text-[10px] shrink-0 text-[var(--muted)]">
                                  {Math.round(Math.min(1, Math.max(0, s.progress)) * 100)}%
                                </span>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* 操作 */}
              <div className="flex items-center gap-2 flex-wrap pt-1">
                {(activeDetail.status === "RUNNING" || activeDetail.status === "RECLAIMING") && (
                  <button type="button" onClick={() => act("pause")} disabled={!!acting}
                    className="px-3 py-1.5 rounded-lg text-xs text-amber-300 disabled:opacity-40"
                    style={{ border: "1px solid rgba(251,191,36,0.4)" }}>
                    {acting === "pause" ? "暂停中…" : "⏸ 暂停"}
                  </button>
                )}
                {activeDetail.status === "PAUSED" && (
                  <button type="button" onClick={() => act("resume")} disabled={!!acting}
                    className="px-3 py-1.5 rounded-lg text-xs text-emerald-300 disabled:opacity-40"
                    style={{ border: "1px solid rgba(52,211,153,0.4)" }}>
                    {acting === "resume" ? "恢复中…" : "▶ 继续"}
                  </button>
                )}
                {(activeDetail.status === "COMPLETED" || activeDetail.status === "FAILED") && (
                  <button type="button" onClick={() => act("reclaim")} disabled={!!acting}
                    title={`已自动补跑 ${activeDetail.retry_count} 次`}
                    className="px-3 py-1.5 rounded-lg text-xs text-indigo-300 disabled:opacity-40"
                    style={{ border: "1px solid rgba(99,102,241,0.4)" }}>
                    {acting === "reclaim" ? "提交中…" : `↻ 回收补跑（已补跑 ${activeDetail.retry_count} 次）`}
                  </button>
                )}
                {!isTerminal(activeDetail.status) && (
                  <button type="button" onClick={() => act("cancel")} disabled={!!acting}
                    className="px-3 py-1.5 rounded-lg text-xs text-red-300 disabled:opacity-40"
                    style={{ border: "1px solid rgba(248,113,113,0.4)" }}>
                    {acting === "cancel" ? "取消中…" : "⏹ 取消统筹"}
                  </button>
                )}
                <span className="text-[10px] ml-auto text-[var(--muted)]">
                  开始 {fmtTime(activeDetail.started_at)} · 结束 {fmtTime(activeDetail.finished_at)}
                </span>
              </div>

              {/* ---------- 5. 最终报告 ---------- */}
              {isTerminal(activeDetail.status) && (
                <div className="pt-2" style={{ borderTop: "1px solid var(--border)" }}>
                  <div className="flex items-center justify-between gap-2 flex-wrap mt-2 mb-2">
                    <h3 className="text-sm font-medium text-[var(--foreground)]">⑤ 最终报告</h3>
                    {report.data?.artifact_id && (
                      <Link
                        href={`/artifacts?open=${report.data.artifact_id}`}
                        className="text-[11px] px-2.5 py-1 rounded-lg text-emerald-200"
                        style={{ border: "1px solid rgba(52,211,153,0.4)" }}
                      >
                        去成果中心查看 →
                      </Link>
                    )}
                  </div>

                  {report.error && <InlineError>报告读取失败：{report.error}</InlineError>}
                  {!report.error && !report.data && <p className="text-xs text-[var(--muted)]">正在读取报告…</p>}

                  {report.data && (
                    <div className="space-y-2.5">
                      <div className="flex flex-wrap gap-1.5">
                        {([
                          ["完成", report.data.stats.completed],
                          ["偏离", report.data.stats.off_track],
                          ["失败", report.data.stats.failed],
                          ["补跑", report.data.stats.retried],
                          ["并发", report.data.stats.effective_parallel],
                          ["降档", report.data.stats.degraded ? "是" : "否"],
                        ] as [string, unknown][]).map(([k, v]) => (
                          <span key={k} className="badge text-[10px]"
                            style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
                            {k} <b className="text-[var(--foreground)] tabular-nums">{v == null ? "—" : String(v)}</b>
                          </span>
                        ))}
                        <span className="text-[10px] text-[var(--muted)] self-center">
                          结束于 {fmtTime(report.data.finished_at)}
                        </span>
                      </div>

                      {report.data.report ? (
                        <pre className="whitespace-pre-wrap break-words rounded-xl p-3 text-xs leading-6 text-[var(--foreground)] max-h-[520px] overflow-auto"
                          style={{ background: "rgba(0,0,0,0.28)", border: "1px solid var(--border)" }}>
                          {report.data.report}
                        </pre>
                      ) : (
                        <p className="text-xs text-[var(--muted)]">这场统筹还没有生成报告。</p>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* 未结束但有报告片段：给一个"已接近收尾"的提示，完整报告在结束后出现 */}
              {!isTerminal(activeDetail.status) && activeDetail.report_excerpt && (
                <div>
                  <div className="text-[11px] font-medium mb-1 text-[var(--muted)]">报告摘要（进行中）</div>
                  <pre className="whitespace-pre-wrap break-words rounded-xl p-3 text-xs leading-6 text-[var(--muted)]"
                    style={{ background: "rgba(0,0,0,0.25)", border: "1px solid var(--border)" }}>
                    {activeDetail.report_excerpt}
                  </pre>
                </div>
              )}
            </div>
          )}
        </section>
      </div>

      {/* ---------- 6. CEO 日报 ---------- */}
      <Section
        title="⑥ CEO 日报"
        hint="把最近 24 小时的统筹、执行、成本与异常收成一页；可以手动生成，也可以每天 09:00 自动推送。"
        aside={
          digest.data ? (
            <span className="text-[11px] text-[var(--muted)]">
              生成于 {fmtTime(digest.data.generated_at)} · 近 {digest.data.window_hours} 小时
            </span>
          ) : undefined
        }
      >
        {digest.error && <InlineError>日报读取失败：{digest.error}</InlineError>}

        <div className="flex items-center gap-2 flex-wrap mb-3">
          <button type="button" onClick={runDigest} disabled={!cid || !!digestBusy}
            className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40">
            {digestBusy === "run" ? "生成中…" : "立即生成"}
          </button>
          <button type="button" onClick={scheduleDigest} disabled={!cid || !!digestBusy}
            className="btn-ghost text-xs disabled:opacity-40">
            {digestBusy === "schedule" ? "提交中…" : "每天 09:00 推送"}
          </button>
          {schedule && (
            <span className="text-[11px]" style={{ color: schedule.created ? "#6ee7b7" : "var(--muted)" }}>
              {schedule.created
                ? "已创建每日 09:00 的日报调度"
                : `已存在，不会重复创建（${String(schedule.trigger_config?.time ?? "09:00")}）`}
            </span>
          )}
        </div>
        <InlineError>{digestError}</InlineError>

        {digest.data ? (
          <pre className="whitespace-pre-wrap break-words rounded-xl p-3 text-xs leading-6 text-[var(--foreground)] max-h-[420px] overflow-auto"
            style={{ background: "rgba(0,0,0,0.28)", border: "1px solid var(--border)" }}>
            {digest.data.markdown || "（日报为空）"}
          </pre>
        ) : (
          !digest.error && <p className="text-xs text-[var(--muted)]">正在读取日报…</p>
        )}
      </Section>
    </div>
  );
}
