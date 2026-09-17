"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { TASK_STATUS_ZH, EVENT_TYPE_ZH, EXEC_STATUS_ZH, zh } from "@/lib/i18n";
import { DeliverablesPanel, TaskLogsPanel } from "@/components/DeliverablesLogs";
import { createPortal } from "react-dom";
import { notify } from "@/components/Toast";
import type { Company } from "@/lib/types";

/* ============================================================
   任务详情 v2（仿项目详情）：
   渐变横幅 + 生命周期操作 + 进度
   AI 聊天面板：换Agent / 换模型 / 加技能 / 下达指令 / 上传资料
   ============================================================ */

type Detail = {
  id: string; title: string; description: string | null; status: string;
  priority: string; result: string | null; assigned_agent_id: string | null;
  project_id: string | null; company_id?: string;
  subtasks: string[]; executions: any[]; events: any[]; interventions: any[];
};
type Agent = { id: string; name: string; role: string | null };
type ModelRow = { id: string; name: string; provider_type: string; model_id: string };
type SkillRow = { id: string; name: string };

const STATUS_STYLE: Record<string, { label: string; cls: string; dot: string }> = {
  TODO: { label: "待办", cls: "text-[var(--muted)]", dot: "bg-gray-500" },
  PLANNING: { label: "规划中", cls: "text-purple-300", dot: "bg-purple-400" },
  RUNNING: { label: "执行中", cls: "text-cyan-300", dot: "bg-cyan-400" },
  WAITING: { label: "等待中", cls: "text-yellow-300", dot: "bg-yellow-400" },
  PAUSED: { label: "已暂停", cls: "text-amber-300", dot: "bg-amber-400" },
  REVIEW: { label: "评审中", cls: "text-blue-300", dot: "bg-blue-400" },
  COMPLETED: { label: "已完成", cls: "text-emerald-300", dot: "bg-emerald-400" },
  FAILED: { label: "失败", cls: "text-red-300", dot: "bg-red-400" },
  CANCELLED: { label: "已停止", cls: "text-[var(--muted)]", dot: "bg-gray-600" },
};
const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  LOW: { label: "低", cls: "text-[var(--muted)]" },
  NORMAL: { label: "普通", cls: "text-sky-300" },
  HIGH: { label: "高", cls: "text-amber-300" },
  URGENT: { label: "紧急", cls: "text-red-300" },
};
const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

/** 任务群聊的一条消息。
 *
 * 注意 role **不能用** `"user" | "assistant"` 这种窄联合：员工消息的 role
 * 直接就是员工姓名，界面靠 isBoss / isAgent 决定气泡方向与配色。
 * 写成窄联合的后果是渲染处只能写 `(m: any)`，等于把类型全丢了。
 */
type ChatMsg = {
  role: string;
  text: string;
  isBoss?: boolean;
  isAgent?: boolean;
  agent?: string;
  search?: boolean;
};

/** `GET /group-chat/{scope}/{id}/history` 里的一条记录 */
type GroupChatMessage = {
  role: "boss" | "agent" | string;
  text?: string;
  agent_name?: string;
  agent_id?: string;
  search_used?: boolean;
};

export default function TaskDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [d, setD] = useState<Detail | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [allModels, setAllModels] = useState<ModelRow[]>([]);
  const [availableSkills, setAvailableSkills] = useState<SkillRow[]>([]);
  const [mounted, setMounted] = useState(false);
  const [assignPanelOpen, setAssignPanelOpen] = useState(false);
  const [pickAgent, setPickAgent] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [cid, setCid] = useState("");

  // 聊天面板
  const [chatOpen, setChatOpen] = useState(true);
  const [manualMode, setManualMode] = useState(false);
  const [manualTab, setManualTab] = useState<"agent" | "model" | "skill">("agent");
  const [selAgent, setSelAgent] = useState<string>("");
  const [selModel, setSelModel] = useState<string>("");
  const [selSkills, setSelSkills] = useState<Set<string>>(new Set());
  const [chatInput, setChatInput] = useState("");
  const [chatLog, setChatLog] = useState<ChatMsg[]>([
    { role: "system",
      text: "💬 任务群聊已启动 — 直接输入文字即可下达指令给执行中的 Agent", },
  ]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setD(await api.get(`/tasks/${id}`));
  }, [id]);

  const [chatHist, setChatHist] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderFiles, setFolderFiles] = useState<any[]>([]);
  useEffect(() => {
    load();
    api.get("/agents").then(setAgents).catch(() => {});
    // 注：群聊历史在此处曾有一份重复实现（与下方 useEffect 完全同款请求）。
    // 两份都靠 chatHist 布尔量互斥，谁先跑谁生效——行为上无害，但等于
    // 同一段逻辑维护两遍，且让本 effect 的依赖数组失真。
    // 现统一归到下方"cid 就绪后触发一次"的 effect。
    api.get("/models").then(setAllModels).catch(() => {});
    api.get("/skills").then(setAvailableSkills).catch(() => {});
    api.get<Company[]>("/companies").then(async (cs) => {
      if (cs.length) setCid(cs[0].id);
    }).catch(() => {});
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  // 群聊历史加载（cid 就绪后触发一次）
  useEffect(() => {
    if (!cid || !id || chatHist) return;
    setChatHist(true);
    api.get<{ messages?: GroupChatMessage[] }>(`/group-chat/task/${id}/history?company_id=${cid}`)
      .then((d) => {
        if (d.messages?.length) {
          setChatLog(d.messages.map((m) => ({
            role: m.role === "boss" ? "user" : m.agent_name || "agent",
            text: m.text || "", isAgent: m.role === "agent",
            agent: m.agent_name, search: m.search_used,
          })));
        }
      })
      .catch(() => {});
  }, [cid, id, chatHist]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatLog]);

  async function control(action: "pause" | "resume" | "cancel" | "start") {
    if (!id) return;
    setBusy(true);
    const zhLabel: Record<string, string> = {
      pause: "已暂停（进度保留）", resume: "已恢复执行", cancel: "已停止（成果保留）",
    };
    try {
      await api.post(`/tasks/${id}/${action}`);
      setMsg(`✅ ${zhLabel[action]}`);
      await load();
    } catch (e: any) { setMsg(`❌ ${e.message || e}`); }
    setBusy(false);
  }

  /* ---------- 聊天指令 ---------- */
  async function sendChat() {
    const text = chatInput.trim();
    if (!text || !id) return;
    if (!cid) { setChatLog((prev) => [...prev, { role: "assistant", text: "正在初始化，请稍候1秒再发送…" }]); return; }
    setChatLog((prev) => [...prev, { role: "user", text, isBoss: true }]);
    setChatInput("");
    try {
      const data = await api.post<{ replies?: {
        agent_id?: string; agent_name?: string; text?: string; search_used?: boolean;
      }[] }>(`/group-chat/task/${id}?company_id=${cid}`, { message: text },
        // 群里多位员工依次发言，耗时较长——给足上限但不再是无限等待
        { timeoutMs: 300_000 });
      const replies = data.replies || [];
      setChatLog((prev) => prev.concat(
        replies.map((rep) => ({ role: rep.agent_name || rep.agent_id || "agent",
                                text: rep.text || "", isAgent: true,
                                agent: rep.agent_name, search: rep.search_used || false }))));
      await load();
    } catch (e) {
      setChatLog((prev) => [...prev, { role: "assistant", text: `出错了：${describeError(e, "发送失败")}`, isBoss: false }]);
    }
  }

  /* ---------- 更换 Agent / 模型 / 加技能 ---------- */
  const currentAgentId = d?.assigned_agent_id;

  async function applyChange(newAgentId: string, newModelId: string, skillIds: Set<string>) {
    if (!id || !d) return;
    setBusy(true);
    try {
      // 1. 更换执行 Agent
      if (newAgentId && newAgentId !== d.assigned_agent_id) {
        await api.patch(`/tasks/${id}`, { assigned_agent_id: newAgentId });
        setMsg("✅ 执行 Agent 已更换");
        // 新 Agent 绑定所选模型
        if (newModelId) {
          // 原来是裸 fetch 且不看返回：模型没切成功也会提示"已更换"
          await api.put(`/agents/${newAgentId}/switch-model`, { model_id: newModelId });
        }
        // 新 Agent 获得勾选的技能
        for (const sk of Array.from(skillIds)) {
          await api.post(`/skill-growth/agents/${newAgentId}/acquire`, {
            skill_node_id: sk,
            source_type: "assignment",
            evidence: [{ type: "assignment" }],
          }).catch(() => {});
        }
      }
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "更换失败")}`); }
    setBusy(false);
  }

  /* ---------- 上传参考材料 ---------- */
  async function uploadFile(file: File) {
    setChatLog((prev) => [...prev, { role: "user", text: `📎 上传了参考资料：${file.name}` }]);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api.upload("/knowledge/documents", fd);
      setChatLog((prev) => [...prev, {
        role: "assistant",
        text: `✅ 参考资料「${file.name}」已存入知识库。`,
      }]);
    } catch (e) {
      setChatLog((prev) => [...prev, {
        role: "assistant", text: `上传失败：${describeError(e, "上传失败")}` }]);
    }
  }

  if (!d) {
    return <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <p className="text-sm text-center py-20" style={{ color: "var(--muted)" }}>加载中…</p>
    </div>;
  }

  const st = STATUS_STYLE[d.status] || STATUS_STYLE.TODO;
  const prio = PRIORITY_META[d.priority];
  const executor = agents.find((a) => a.id === d.assigned_agent_id);
  const canPause = d.status === "RUNNING";
  const canResume = d.status === "PAUSED" || d.status === "TODO";
  const canStop = !["COMPLETED", "CANCELLED"].includes(d.status);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头横幅 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative">
          <button onClick={() => router.push("/tasks")} className="btn-ghost text-xs mb-3">← 返回任务中心</button>
          <div className="flex items-start justify-between flex-wrap gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-3 flex-wrap">
                <h1 className="text-2xl font-bold tracking-tight">{d.title}</h1>
                <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full`}
                  style={{ background: "rgba(255,255,255,0.05)", border: "1px solid var(--border)" }}>
                  <span className={`w-1.5 h-1.5 rounded-full ${st.dot} ${d.status === "RUNNING" ? "pulse-dot" : ""}`} />
                  <span className={st.cls}>{st.label}</span>
                </span>
                {prio && (
                  <span className={`text-xs px-2 py-0.5 rounded-md ${prio.cls}`}
                    style={{ background: "rgba(255,255,255,0.04)" }}>
                    {prio.label}优先级
                  </span>
                )}
              </div>
              <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
                {d.description || "暂无描述"}
              </p>
            </div>

            {/* 生命周期按钮组 */}
            <div className="flex gap-2 flex-wrap">
              {canResume && (
                <button onClick={() => control(d.status === "PAUSED" ? "resume" : "start")} disabled={busy}
                  className="px-4 py-2 rounded-lg text-sm font-medium transition-all hover:bg-emerald-500/15 text-emerald-300 disabled:opacity-40"
                  style={{ background: "rgba(16,185,129,0.12)", border: "1px solid rgba(16,185,129,0.35)" }}>
                  ▶ {d.status === "PAUSED" ? "恢复执行" : "启动"}
                </button>
              )}
              {canPause && (
                <button onClick={() => control("pause")} disabled={busy}
                  className="btn-ghost disabled:opacity-40">⏸ 暂停</button>
              )}
              {canStop && (
                <button onClick={() => control("cancel")} disabled={busy}
                  className="btn-ghost disabled:opacity-40">⏹ 停止</button>
              )}
            </div>
          </div>
        </div>
      </div>

      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {/* 主内容两栏 */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-5 items-start">
        {/* 左栏 */}
        <div className="space-y-5 min-w-0">
          {/* 任务负责人：换 / 清空 */}
          <div className="card card-static">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-sm">任务负责人</h2>
              <button onClick={() => setAssignPanelOpen(!assignPanelOpen)}
                className="btn-ghost !py-1 !px-2.5 text-[11px]">
                {assignPanelOpen ? "收起" : d.assigned_agent_id ? "更换负责人" : "＋ 指派负责人"}
              </button>
            </div>

            {/* 选择面板 */}
            {assignPanelOpen && (
              <div className="mb-3 rounded-xl p-3 animate-fade-up"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <div className="max-h-44 overflow-y-auto space-y-1">
                  {agents.map((a, i) => {
                    const isCurrent = a.id === d.assigned_agent_id;
                    return (
                      <button key={a.id}
                        onClick={() => setPickAgent(a.id)}
                        className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                          pickAgent === a.id ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                        style={pickAgent === a.id ? {} : { borderColor: "transparent" }}>
                        <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                          style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[i % AVATAR_GRADS.length]})` }}>
                          {a.name.slice(0, 1)}
                        </div>
                        <span className="truncate flex-1">{a.name}</span>
                        {isCurrent && <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-300 shrink-0">当前</span>}
                        {a.role && !isCurrent && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{a.role}</span>}
                        {pickAgent === a.id && <span className="text-indigo-300 shrink-0">✓</span>}
                      </button>
                    );
                  })}
                </div>
                <div className="flex gap-2 mt-2">
                  <button onClick={async () => {
                    if (!pickAgent) return;
                    await applyChange(pickAgent, "", new Set());
                    const nm = agents.find((a) => a.id === pickAgent)?.name;
                    setChatLog((prev) => [...prev, {
                      role: "assistant", text: `✅ 任务负责人已更换为「${nm}」。`,
                    }]);
                    setPickAgent("");
                    setAssignPanelOpen(false);
                  }} disabled={!pickAgent}
                    className="btn-primary !py-1.5 text-xs flex-1 disabled:opacity-40">
                    确认指派
                  </button>
                  {d.assigned_agent_id && (
                    <button onClick={async () => {
                      await api.patch(`/tasks/${id}`, { assigned_agent_id: null });
                      setMsg("已清空负责人");
                      setAssignPanelOpen(false);
                      load();
                    }} className="btn-ghost text-xs">清空负责人</button>
                  )}
                </div>
              </div>
            )}

            {/* 当前负责人展示 */}
            <div className="flex items-center gap-3 min-h-10">
              {executor ? (
                <>
                  <div className="w-9 h-9 rounded-xl flex items-center justify-center font-bold text-white text-sm shrink-0"
                    style={{
                      background: `linear-gradient(135deg, ${AVATAR_GRADS[0]})`,
                      boxShadow: `0 4px 14px -6px ${AVATAR_GRADS[0].split(",")[0]}66`,
                    }}>
                    {executor.name.slice(0, 1)}
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">{executor.name}</div>
                    {executor.role && (
                      <div className="text-[11px]" style={{ color: "var(--muted)" }}>{executor.role}</div>
                    )}
                  </div>
                  <span className="ml-auto text-[10px] px-2 py-1 rounded-full bg-amber-500/12 text-amber-300 shrink-0">
                    ★ 负责人
                  </span>
                </>
              ) : (
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  暂未指派 · 点击右上角「＋ 指派负责人」选择执行 Agent
                </p>
              )}
            </div>
          </div>

          {d.project_id && <DeliverablesPanel projectId={d.project_id} />}

          {/* 最终成果 */}
          {d.result && (
            <div className="card card-static">
              <h2 className="font-semibold text-sm mb-2">📋 最终成果报告</h2>
              <pre className="text-xs whitespace-pre-wrap max-h-80 overflow-auto leading-relaxed"
                style={{ color: "var(--foreground)" }}>{d.result}</pre>
            </div>
          )}

          {/* 员工执行记录 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">员工执行记录（{d.executions.length}）</h2>
            <div className="space-y-2">
              {d.executions.map((e) => {
                const ea = agents.find((a) => a.id === e.agent_id);
                return (
                  <details key={e.id} className="rounded-xl p-3 transition-colors"
                    style={{ background: "var(--surface-2)" }}>
                    <summary className="text-xs cursor-pointer flex items-center gap-2">
                      <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                        style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[0]})` }}>
                        {(ea?.name || "?").slice(0, 1)}
                      </div>
                      <span className="font-medium">{ea?.name || e.agent_id.slice(0, 8)}</span>
                      <span className={
                        e.status === "completed" ? "text-emerald-400"
                        : e.status === "failed" ? "text-red-400" : "text-cyan-400"}>
                        {zh(EXEC_STATUS_ZH, e.status)}
                      </span>
                    </summary>
                    <pre className="mt-2 text-[11px] whitespace-pre-wrap overflow-auto max-h-56"
                      style={{ color: "var(--muted)" }}>{e.output || e.error}</pre>
                  </details>
                );
              })}
              {!d.executions.length && (
                <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>尚无执行记录</p>
              )}
            </div>
          </div>

          <TaskLogsPanel taskId={id} />

          {/* 事件流 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">事件流</h2>
            <div className="divide-y max-h-64 overflow-y-auto" style={{ borderColor: "var(--border)" }}>
              {d.events.slice().reverse().map((e, i) => (
                <div key={i} className="py-2 text-xs flex justify-between items-center gap-2">
                  <span className="flex items-center gap-2 min-w-0">
                    <span className="w-1 h-1 rounded-full bg-indigo-400/60 shrink-0" />
                    <span className="truncate">{zh(EVENT_TYPE_ZH, e.type)}</span>
                  </span>
                  <span className="tabular-nums shrink-0" style={{ color: "var(--muted)" }}>
                    {new Date(e.at).toLocaleTimeString()}
                  </span>
                </div>
              ))}
              {!d.events.length && (
                <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>暂无事件</p>
              )}
            </div>
          </div>

          {/* 人工介入记录 */}
          {d.interventions.length > 0 && (
            <div className="card card-static">
              <h2 className="font-semibold text-sm mb-3">人工介入记录</h2>
              <div className="space-y-1.5">
                {d.interventions.map((iv) => (
                  <div key={iv.id} className="flex items-start gap-2 px-3 py-2 rounded-xl text-xs"
                    style={{ background: "var(--surface-2)" }}>
                    <span className="shrink-0">
                      {iv.type === "INSTRUCTION" ? "📣" : iv.type === "MESSAGE" ? "💬" : "🎛"}
                    </span>
                    <span className="truncate">{iv.content}</span>
                    <span className="ml-auto shrink-0" style={{ color: "var(--muted)" }}>{iv.status}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* 右栏：AI 聊天面板 */}
        {chatOpen && (
          <div className="card flex flex-col lg:sticky lg:top-4"
            style={{ height: "calc(100vh - 220px)", minHeight: 480 }}>
            <div className="flex items-center justify-between pb-2.5 border-b" style={{ borderColor: "var(--border)" }}>
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-xl flex items-center justify-center text-white text-sm"
                  style={{ background: "linear-gradient(135deg,#6366f1,#a78bfa)" }}>🤖</div>
                <div>
                  <h2 className="font-semibold text-sm">任务助手</h2>
                  <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                    换Agent · 换模型 · 加技能 · 下达指令
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button title="任务交付文件"
                  onClick={async () => {
                    setFolderOpen(!folderOpen);
                    if (!folderOpen && cid) {
                      try {
                        const data = await api.get<{ files?: { id?: string; name: string; size?: number }[] }>(
                          `/delivery/by-scope?company_id=${cid}&scope=task&scope_id=${id}`);
                        setFolderFiles(data.files || []);
                      } catch {
                        // 交付文件拉不到不该影响对话，静默留空即可
                        setFolderFiles([]);
                      }
                    }
                  }}
                  className={`w-7 h-7 rounded-lg flex items-center justify-center text-xs transition-colors ${folderOpen ? "bg-indigo-500/20 text-indigo-300" : "hover:bg-white/[0.06]"}`}
                  style={folderOpen ? {} : { color: "var(--muted)" }}>📁</button>
                <button onClick={() => setChatOpen(false)} className="btn-ghost !px-2 !py-1 text-xs">—</button>
              </div>
            </div>

            {/* 手动选择面板 */}
            {manualMode && (
              <div className="my-2 rounded-xl p-3 space-y-2.5 animate-fade-up"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <div className="flex gap-1.5">
                  {([["agent", "🤖 换Agent"], ["model", "🧬 换模型"], ["skill", "⚡ 加技能"]] as const).map(([v, label]) => (
                    <button key={v} onClick={() => setManualTab(v)}
                      className={`px-2.5 py-1 rounded-full text-[11px] transition-all border ${
                        manualTab === v ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                                        : "hover:bg-white/[0.04]"}`}
                      style={manualTab === v ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                      {label}
                    </button>
                  ))}
                </div>

                <div className="max-h-44 overflow-y-auto space-y-1">
                  {manualTab === "agent" && agents.map((a, i) => (
                    <button key={a.id}
                      onClick={() => { setSelAgent(a.id); setSelModel(""); }}
                      className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                        selAgent === a.id ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                      style={selAgent === a.id ? {} : { borderColor: "transparent" }}>
                      <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                        style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[i % AVATAR_GRADS.length]})` }}>
                        {a.name.slice(0, 1)}
                      </div>
                      <span className="truncate flex-1">{a.name}</span>
                      {a.role && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{a.role}</span>}
                      {selAgent === a.id && <span className="text-indigo-300">✓</span>}
                    </button>
                  ))}
                  {manualTab === "model" && allModels.map((m) => (
                    <button key={m.id}
                      onClick={() => setSelModel(m.id)}
                      className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                        selModel === m.id ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                      style={selModel === m.id ? {} : { borderColor: "transparent" }}>
                      🧬 <span className="truncate flex-1">{m.name}</span>
                      <span className="text-[10px]" style={{ color: "var(--muted)" }}>{m.provider_type}</span>
                      {selModel === m.id && <span className="text-indigo-300">✓</span>}
                    </button>
                  ))}
                  {manualTab === "skill" && availableSkills.map((sk) => {
                    const on = selSkills.has(sk.id);
                    return (
                      <button key={sk.id}
                        onClick={() => setSelSkills((prev) => {
                          const n = new Set(prev); if (n.has(sk.id)) n.delete(sk.id); else n.add(sk.id); return n;
                        })}
                        className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                          on ? "border-emerald-500/50 bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "transparent" }}>
                        ⚡ <span className="truncate flex-1">{sk.name}</span>
                        {on && <span className="text-emerald-300">✓</span>}
                      </button>
                    );
                  })}
                </div>

                <button
                  onClick={async () => {
                    if (!selAgent && !selModel && !selSkills.size) return;
                    await applyChange(selAgent, selModel, selSkills);
                    setChatLog((prev) => [...prev, {
                      role: "assistant",
                      text: `✅ 已应用调整${selAgent ? `\n· 执行 Agent 已更换` : ""}${selModel ? "\n· 驱动模型已切换" : ""}${selSkills.size ? `\n· 已附加 ${selSkills.size} 项技能` : ""}`,
                    }]);
                    setManualMode(false);
                    setSelSkills(new Set());
                  }}
                  disabled={!selAgent && !selModel && !selSkills.size}
                  className="btn-primary !py-1.5 text-xs w-full disabled:opacity-40">
                  应用更改
                </button>
              </div>
            )}

            {folderOpen && (
              <div className="my-2 rounded-xl p-3 animate-fade-up overflow-y-auto max-h-40 space-y-1 shrink-0"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <p className="text-[11px] font-medium mb-1">📁 任务交付文件（{folderFiles.length}）</p>
                {folderFiles.map((f: any) => (
                  <div key={f.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs"
                    style={{ background: "rgba(255,255,255,0.03)" }}>
                    <span className="truncate flex-1">{f.name}</span>
                    <button onClick={async () => {
                      try {
                        downloadURL(await fetchBlobURL(`/delivery/download/${f.id}`), f.name);
                      } catch (e: any) {
                        notify("error", `下载失败：${describeError(e)}`);
                      }
                    }} className="text-indigo-300 shrink-0 text-[10px]">⬇</button>
                  </div>
                ))}
                {!folderFiles.length && (
                  <p className="text-[11px] text-center py-2" style={{ color: "var(--muted)" }}>
                    暂无交付文件 · 对话中让 Agent 生成文档即可
                  </p>
                )}
              </div>
            )}

            {/* 消息流 */}
            <div className="flex-1 overflow-y-auto py-3 space-y-2.5">
              {chatLog.map((m, i) => {
                const isBoss = m.isBoss === true;
                const isAgent = m.isAgent === true;
                const isErr = !isAgent && !isBoss && m.role === "assistant";
                if (m.role === "system") return null;
                if (isErr) return (
                  <div key={i} className="flex justify-start">
                    <div className="max-w-[82%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap"
                      style={{ background: "rgba(239,68,68,0.12)" }}>⚠️ {m.text}</div>
                  </div>
                );
                return (
                  <div key={i} className={`flex ${isBoss ? "justify-end" : "justify-start"}`}>
                    <div className="max-w-[82%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap leading-relaxed"
                      style={isBoss ? { background: "linear-gradient(135deg,#4f46e5,#7c3aed)", color: "#fff" }
                        : { background: "var(--surface-2)", color: "var(--foreground)" }}>
                      {isAgent && (
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="w-5 h-5 rounded-full inline-flex items-center justify-center text-[10px]"
                            style={{ background: "rgba(99,102,241,0.25)" }}>🤖</span>
                          <span className="font-semibold text-[11px]" style={{ color: "var(--indigo, #818cf8)" }}>{m.agent}</span>
                          {m.search && <span className="text-[9px]" style={{ color: "#22d3ee" }}>🔍搜索</span>}
                        </div>
                      )}
                      {m.text}
                    </div>
                  </div>
                );
              })}
              <div ref={chatEndRef} />
            </div>

            {/* 输入区 */}
            <div className="pt-2.5 border-t flex items-center gap-2" style={{ borderColor: "var(--border)" }}>
              {!manualMode && (
                <button title="手动选择 Agent / 模型 / 技能"
                  onClick={() => setManualMode(true)}
                  className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-indigo-500/15 text-indigo-300 shrink-0"
                  style={{ border: "1px solid rgba(99,102,241,0.3)" }}>⚙</button>
              )}
              <input ref={fileRef} type="file" className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) uploadFile(f);
                  e.target.value = "";
                }} />
              <button title="上传参考资料" onClick={() => fileRef.current?.click()}
                className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-white/[0.06] shrink-0"
                style={{ color: "var(--muted)" }}>📎</button>
              <input value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && sendChat()}
                placeholder="向执行 Agent 下达指令…"
                className="input flex-1 !py-2" />
              <button onClick={sendChat} disabled={!chatInput.trim()}
                className="btn-primary !px-3 !py-2 shrink-0 disabled:opacity-40">发送</button>
            </div>
          </div>
        )}
        {!chatOpen && (
          <button onClick={() => setChatOpen(true)}
            className="fixed bottom-6 right-6 z-40 w-14 h-14 rounded-full shadow-2xl flex items-center justify-center text-xl animate-fade-up"
            style={{
              background: "linear-gradient(135deg,#6366f1,#a78bfa)",
              boxShadow: "0 8px 24px -4px rgba(99,102,241,0.55)",
            }}
            title="打开任务助手">🤖</button>
        )}
      </div>
    </div>
  );
}
