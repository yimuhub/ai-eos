"use client";
import { useMemo, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";
import { notify } from "@/components/Toast";

type TriggerType = "TIME" | "EVENT" | "FILE" | "PROJECT" | "TASK" | "AGENT" | "WEBHOOK" | "HUMAN" | "MANUAL";
type Mode = "TEST" | "SIMULATION" | "PRODUCTION";
type Status = "DRAFT" | "ENABLED" | "PAUSED" | "ARCHIVED";

type Automation = {
  id: string; name: string; trigger_type: TriggerType;
  /** 任务模板复用自动化定义：kind=TASK_TEMPLATE 时列表显示 📋 徽标 */
  trigger_config?: { kind?: string; [key: string]: unknown };
  steps_count: number; mode: Mode; status: Status;
};
type RunRecord = {
  id: string; status: string; mode: string;
  steps: any[]; error?: string; created_at: string;
};

const TRIGGER_LABELS: Record<TriggerType, string> = {
  TIME: "⏱ 定时触发", EVENT: "📌 事件触发", FILE: "📁 文件触发",
  PROJECT: "📂 项目事件", TASK: "✅ 任务完成", AGENT: "🤖 Agent动作",
  WEBHOOK: "🔗 Webhook", HUMAN: "👤 人工触发", MANUAL: "🖱 手动",
};
const MODE_LABELS: Record<Mode, string> = {
  TEST: "测试", SIMULATION: "仿真", PRODUCTION: "生产",
};
const STATUS_STYLES: Record<Status, string> = {
  DRAFT: "bg-white/10 text-[var(--muted)] border border-[var(--border)]",
  ENABLED: "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30",
  PAUSED: "bg-amber-500/20 text-amber-300 border border-amber-500/30",
  ARCHIVED: "bg-rose-500/10 text-rose-400 border border-rose-500/20",
};
const STATUS_LABELS: Record<Status, string> = {
  DRAFT: "● 草稿", ENABLED: "● 运行中", PAUSED: "○ 已暂停", ARCHIVED: "△ 已归档",
};
const RUN_STATUS_BADGE = (s: string) =>
  s === "COMPLETED" ? "bg-emerald-500/20 text-emerald-300" :
  s === "RUNNING" ? "bg-blue-500/30 text-blue-300 animate-pulse" :
  s === "WAITING_APPROVAL" ? "bg-amber-500/30 text-amber-300" :
  s === "FAILED" ? "bg-rose-500/20 text-rose-300" :
  s === "ABORTED" ? "bg-white/10 text-[var(--muted)]" : "bg-white/5 text-[var(--muted)]";

export default function AutomationsPage() {
  // 公司上下文统一由数据层提供：切公司自动跟随，不再一次性取 cs[0]
  const cid = useActiveCompanyId();
  const [filter, setFilter] = useState<Status | "ALL">("ALL");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [runHistory, setRunHistory] = useState<Record<string, RunRecord[]>>({});
  const [msg, setMsg] = useState("");

  // Create form
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState<TriggerType>("MANUAL");
  const [mode, setMode] = useState<Mode>("TEST");
  const [steps, setSteps] = useState("");
  const [maxRuns, setMaxRuns] = useState("10");
  const [attachments, setAttachments] = useState<Attachment[]>([]);

  // 任务模板 → 定时任务（「⏰ 定时」内联表单）
  const [schedFor, setSchedFor] = useState<string | null>(null);
  const [schedMode, setSchedMode] = useState<"interval" | "daily">("interval");
  const [schedMinutes, setSchedMinutes] = useState("30");
  const [schedTime, setSchedTime] = useState("09:00");
  const [schedBusy, setSchedBusy] = useState(false);

  /**
   * 主列表交给统一数据层：缓存首屏立即渲染、切公司自动重取、
   * 30 秒后回窗自动刷新。key 传 null（还没确定公司）时不发请求。
   */
  const { data, refresh: reload } = useResource<Automation[]>(
    cid ? `automations:${cid}` : null,
    () => api.get<Automation[]>(`/automations?company_id=${cid}`),
  );
  const list = useMemo<Automation[]>(() => data ?? [], [data]);

  const filtered = useMemo(
    () => filter === "ALL" ? list : list.filter(a => a.status === filter),
    [list, filter]
  );

  const triggerGroups: TriggerType[] = ["TIME", "EVENT", "FILE", "PROJECT", "TASK", "AGENT", "WEBHOOK", "HUMAN", "MANUAL"];
  const statusFilters: (Status | "ALL")[] = ["ALL", "DRAFT", "ENABLED", "PAUSED", "ARCHIVED"];

  const create = async () => {
    if (!name || !cid) return;
    try {
      const parsed = steps.trim()
        ? steps.split("\n").map(l => {
            const t = l.indexOf(":");
            if (t < 0) return { action: l.trim(), config: {} };
            const action = l.slice(0, t).trim();
            const raw = l.slice(t + 1).trim();
            try { return { action, config: JSON.parse(raw) }; } catch { return { action, config: {}, _raw: raw }; }
          })
        : [{ action: "log", config: { message: name + " 执行" } }];
      await api.post(`/automations?company_id=${cid}`, {
        name, trigger_type: trigger, mode, steps: parsed,
        max_runs_per_day: Number(maxRuns) || 10,
        attachments: attachments.length ? attachments : undefined,
      });
      setName(""); setSteps(""); setAttachments([]); setMsg("✅ 自动化规则已创建"); reload();
    } catch (e) { setMsg(String(e)); }
  };

  const updateStatus = async (a: Automation, action: "pause" | "resume" | "archive") => {
    try {
      await api.patch(`/automations/${a.id}`, { action });
      reload();
    } catch (e) { setMsg(String(e)); }
  };

  const runOnce = async (a: Automation) => {
    try {
      const r = await api.post(`/automations/${a.id}/run`) as any;
      setMsg(`▶ 已触发执行，状态: ${r.status}`);
      if (a.id) loadRuns(a.id);
      reload();
    } catch (e) { setMsg(String(e)); }
  };

  /** 复制定义（副本强制 TEST + 暂停，不带走运行历史） */
  const cloneAutomation = async (a: Automation) => {
    try {
      const r = await api.post<{ id: string; name: string }>(
        `/automations/${a.id}/clone`);
      notify("success", `已复制为「${r.name}」（测试模式 · 已暂停）`);
      reload();
    } catch (e) {
      notify("error", describeError(e, "复制失败"));
    }
  };

  const isTemplate = (a: Automation) => a.trigger_config?.kind === "TASK_TEMPLATE";

  const openScheduler = (a: Automation) => {
    if (schedFor === a.id) { setSchedFor(null); return; }
    setSchedFor(a.id);
    setSchedMode("interval");
    setSchedMinutes("30");
    setSchedTime("09:00");
  };

  /** 从任务模板创建真实定时任务（后端 action: create_task_from_template）。 */
  const createTemplateSchedule = async (a: Automation) => {
    if (!cid) return;
    const base = {
      name: `定时：${a.name}`,
      action_type: "create_task_from_template",
      action_params: { template_id: a.id },
    };
    const payload = schedMode === "interval"
      ? {
          ...base, trigger_type: "interval",
          // 界面上限 1 分钟；API 要求 >=10 秒，统一按整分钟发送
          seconds: Math.round(Math.max(1, Number(schedMinutes) || 30) * 60),
        }
      : { ...base, trigger_type: "daily", time: schedTime || "09:00" };
    setSchedBusy(true);
    try {
      await api.post(`/schedules?company_id=${cid}`, payload);
      notify("success", `已创建${payload.name}`);
      setSchedFor(null);
    } catch (e) {
      notify("error", describeError(e, "创建定时任务失败"));
    } finally {
      setSchedBusy(false);
    }
  };

  const loadRuns = async (aid: string) => {
    try {
      const r = await api.get(`/automations/${aid}/runs?limit=10`) as RunRecord[];
      setRunHistory(p => ({ ...p, [aid]: r }));
    } catch { /* ignore */ }
  };

  const approveContinue = async (rid: string, aid: string) => {
    try {
      const r = await api.post(`/automations/runs/${rid}/approve-continue`) as any;
      setMsg("✅ 已审批并通过断点继续执行");
      loadRuns(aid);
    } catch (e) { setMsg(String(e)); }
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-1">🤖 自动化规则</h1>
      <p className="text-sm text-[var(--muted)] mb-6">定义触发条件与执行步骤，让 Agent 自动完成重复工作</p>

      {/* 新增表单 */}
      <div className="card p-4 mb-6 space-y-3">
        <div className="flex gap-2 flex-wrap">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="规则名称（如：每日绩效快照）"
            className="input-glow flex-1 min-w-[200px]" />
          <select value={trigger} onChange={e => setTrigger(e.target.value as TriggerType)} className="input-glow w-40">
            {triggerGroups.map(t => <option key={t} value={t}>{TRIGGER_LABELS[t]}</option>)}
          </select>
          <select value={mode} onChange={e => setMode(e.target.value as Mode)} className="input-glow w-36">
            {(["TEST", "SIMULATION", "PRODUCTION"] as Mode[]).map(m =>
              <option key={m} value={m}>{MODE_LABELS[m]}</option>)}
          </select>
          <input value={maxRuns} onChange={e => setMaxRuns(e.target.value)} placeholder="每日上限"
            type="number" min={1} className="input-glow w-28" />
        </div>
        <textarea value={steps} onChange={e => setSteps(e.target.value)}
          placeholder={`执行步骤（每行一条，格式：动作:配置JSON，示例）\nlog:{"message":"执行开始"}\nchat:{"agent_id":"xxx","message":"请汇报"}\narchive:{"task_id":"xxx"}`}
          rows={4} className="input-glow w-full font-mono text-xs resize-y" />
        <div className="flex items-center gap-3">
          <AttachmentPicker value={attachments} onChange={setAttachments} label="📎 关联附件（规则说明 / 触发数据）" />
          <button onClick={create} disabled={!name} className="btn-primary disabled:opacity-40">
            ➕ 创建规则
          </button>
          <span className="text-xs text-[var(--muted)]">
            提示：TEST 模式只做 DryRun，不真正执行
          </span>
        </div>
      </div>

      {msg && <div className="mb-4 text-sm text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 rounded p-2">{msg}</div>}

      {/* 状态筛选 Tabs */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {statusFilters.map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1 rounded-full text-xs border transition-all ${filter === s ? "bg-indigo-500/30 border-indigo-400/50 text-white" : "border-[var(--border)] text-[var(--muted)] hover:text-white"}`}>
            {s === "ALL" ? "全部" : STATUS_LABELS[s]}
          </button>
        ))}
      </div>

      {/* 卡片列表 */}
      <div className="grid gap-3">
        {filtered.map(a => (
          <div key={a.id} className={`card p-4 transition-all hover:translate-y-[-2px] hover:shadow-[0_0_20px_rgba(99,102,241,0.15)] border border-[var(--border)] ${a.status === "ARCHIVED" ? "opacity-50" : a.status === "PAUSED" ? "opacity-75" : ""}`}>
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <b className="text-sm truncate">{a.name}</b>
                  <span className={`text-xs px-2 py-0.5 rounded-full border ${STATUS_STYLES[a.status]}`}>
                    {STATUS_LABELS[a.status]}
                  </span>
                  <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                    {TRIGGER_LABELS[a.trigger_type]}
                  </span>
                  {isTemplate(a) && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-sky-500/20 text-sky-300 border border-sky-500/30"
                      title="由已完成任务沉淀的任务模板">
                      📋 模板
                    </span>
                  )}
                  <span className="text-xs px-2 py-0.5 rounded-full bg-white/5 text-[var(--muted)] border border-[var(--border)]">
                    {MODE_LABELS[a.mode]} · {a.steps_count} 步
                  </span>
                </div>
                <button onClick={() => { setExpanded(expanded === a.id ? null : a.id); if (!runHistory[a.id]) loadRuns(a.id); }}
                  className="mt-2 text-xs text-indigo-400 hover:text-indigo-300 underline">
                  {expanded === a.id ? "▲ 收起执行历史" : "▼ 查看执行历史"}
                </button>
              </div>
              <div className="flex gap-2 shrink-0">
                {isTemplate(a) ? (
                  <button onClick={() => openScheduler(a)} title="用此模板创建定时任务"
                    className="px-2 py-1 rounded text-xs border border-sky-500/30 text-sky-300 hover:bg-sky-500/20">
                    ⏰ 定时
                  </button>
                ) : a.status !== "ARCHIVED" && (
                  <button onClick={() => runOnce(a)} title="立即执行一次"
                    className="px-2 py-1 rounded text-xs border border-indigo-500/30 text-indigo-300 hover:bg-indigo-500/20">
                    ▶ 执行
                  </button>
                )}
                <button onClick={() => cloneAutomation(a)} title="复制定义（测试模式、默认暂停，不复制运行历史）"
                  className="px-2 py-1 rounded text-xs border border-cyan-500/30 text-cyan-300 hover:bg-cyan-500/20">
                  ⧉ 复制
                </button>
                {a.status === "ENABLED" && (
                  <button onClick={() => updateStatus(a, "pause")}
                    className="px-2 py-1 rounded text-xs border border-amber-500/30 text-amber-300 hover:bg-amber-500/20">
                    暂停
                  </button>
                )}
                {a.status === "PAUSED" && (
                  <button onClick={() => updateStatus(a, "resume")}
                    className="px-2 py-1 rounded text-xs border border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/20">
                    启用
                  </button>
                )}
                {a.status !== "ARCHIVED" && (
                  <button onClick={() => updateStatus(a, "archive")} title="归档"
                    className="px-2 py-1 rounded text-xs border border-rose-500/30 text-rose-300 hover:bg-rose-500/20">
                    🗑
                  </button>
                )}
              </div>
            </div>

            {/* 任务模板 → 定时任务：内联表单，不引入弹窗依赖 */}
            {isTemplate(a) && schedFor === a.id && (
              <div className="mt-3 pt-3 border-t border-[var(--border)] flex flex-wrap items-center gap-2 text-xs">
                <span className="text-sky-300">⏰ 定时创建任务</span>
                <select value={schedMode} onChange={e => setSchedMode(e.target.value as "interval" | "daily")}
                  className="input-glow w-36">
                  <option value="interval">每隔 N 分钟</option>
                  <option value="daily">每天 HH:MM</option>
                </select>
                {schedMode === "interval" ? (
                  <label className="flex items-center gap-1">
                    每隔
                    <input type="number" min={1} value={schedMinutes}
                      onChange={e => setSchedMinutes(e.target.value)}
                      className="input-glow w-20" />
                    分钟
                  </label>
                ) : (
                  <input type="time" value={schedTime}
                    onChange={e => setSchedTime(e.target.value)}
                    className="input-glow w-32" />
                )}
                <button onClick={() => createTemplateSchedule(a)} disabled={schedBusy}
                  className="btn-primary disabled:opacity-40">
                  {schedBusy ? "创建中..." : "创建定时任务"}
                </button>
                <button onClick={() => setSchedFor(null)} disabled={schedBusy}
                  className="px-2 py-1 rounded border border-[var(--border)] text-[var(--muted)] hover:text-white">
                  取消
                </button>
                <span className="text-[var(--muted)]">到点自动从模板生成一条待办任务</span>
              </div>
            )}

            {/* 执行历史展开区 */}
            {expanded === a.id && (
              <div className="mt-3 pt-3 border-t border-[var(--border)] space-y-2">
                {!runHistory[a.id] ? (
                  <p className="text-xs text-[var(--muted)]">加载中...</p>
                ) : runHistory[a.id].length === 0 ? (
                  <p className="text-xs text-[var(--muted)]">尚未执行过</p>
                ) : runHistory[a.id].map(r => (
                  <div key={r.id} className="flex items-center gap-3 text-xs p-2 rounded bg-white/[0.02] border border-[var(--border)]">
                    <span className={`px-2 py-0.5 rounded-full ${RUN_STATUS_BADGE(r.status)}`}>{r.status}</span>
                    <span className="text-[var(--muted)]">{new Date(r.created_at).toLocaleString()}</span>
                    <span className="text-[var(--muted)]">{r.mode}</span>
                    {r.error && <span className="text-rose-400 truncate">{r.error}</span>}
                    {r.status === "WAITING_APPROVAL" && (
                      <button onClick={() => approveContinue(r.id, a.id)}
                        className="ml-auto px-2 py-1 rounded bg-emerald-500/30 text-emerald-300 text-xs hover:bg-emerald-500/40">
                        ✅ 审批通过
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {!filtered.length && (
          <div className="text-center py-12 text-[var(--muted)] text-sm border border-dashed border-[var(--border)] rounded">
            暂无规则（{filter !== "ALL" ? STATUS_LABELS[filter] : ""}）
          </div>
        )}
      </div>
    </div>
  );
}
