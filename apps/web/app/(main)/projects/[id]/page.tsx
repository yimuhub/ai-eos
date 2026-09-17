"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";
import { TASK_STATUS_ZH, zh } from "@/lib/i18n";
import { DeliverablesPanel, ProjectLogsPanel } from "@/components/DeliverablesLogs";
import type { FolderFile } from "@/components/DeliverablesLogs";
import type { Company } from "@/lib/types";

/* ============================================================
   项目详情 v2：生命周期操作（暂停/恢复/停止/删除）
   + AI 聊天面板（换Agent/部门承接/加技能/传资料）
   ============================================================ */

/** 项目群聊消息。role 不能收窄成 "user"|"assistant"：员工消息的 role 就是
 *  员工名字，界面靠 isBoss / isAgent 判断气泡方向。写成窄联合只会逼出 any。 */
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

type Detail = {
  id: string; name: string; description: string | null; status: string;
  priority: string; target_date: string | null; manager_agent_id?: string | null;
  milestones: any[]; members: any[]; tasks: any[]; recent_assets: any[];
};
type Agent = { id: string; name: string; role: string | null };
type SkillRow = { id: string; name: string };

const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];
const STATUS_ZH: Record<string, { label: string; cls: string; dot: string }> = {
  DRAFT: { label: "草稿", cls: "text-[var(--muted)]", dot: "bg-gray-500" },
  PLANNING: { label: "规划中", cls: "text-purple-300", dot: "bg-purple-400" },
  ACTIVE: { label: "执行中", cls: "text-cyan-300", dot: "bg-cyan-400" },
  PAUSED: { label: "已暂停", cls: "text-amber-300", dot: "bg-amber-400" },
  REVIEW: { label: "评审中", cls: "text-blue-300", dot: "bg-blue-400" },
  COMPLETED: { label: "已完成", cls: "text-emerald-300", dot: "bg-emerald-400" },
  ARCHIVED: { label: "已归档", cls: "text-[var(--muted)]", dot: "bg-gray-600" },
  CANCELLED: { label: "已停止", cls: "text-red-300", dot: "bg-red-400" },
};

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [d, setD] = useState<Detail | null>(null);
  const [progress, setProgress] = useState<any>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [depts, setDepts] = useState<{ id: string; name: string }[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [memberPanelOpen, setMemberPanelOpen] = useState(false);
  const [cid, setCid] = useState("");
  const [memberPick, setMemberPick] = useState("");
  const [milestoneName, setMilestoneName] = useState("");

  // 聊天面板
  const [chatOpen, setChatOpen] = useState(true);
  const [chatInput, setChatInput] = useState("");
  const [chatLog, setChatLog] = useState<ChatMsg[]>([
    { role: "assistant",
      text: "我是项目助手 🤖 可以帮你：\n· 更换执行 Agent —— 例「让战略分析师来负责」\n· 部门承接 —— 例「让市场部整个团队承接」\n· 上传参考资料请点左下角 📎\n直接输入指令即可。" },
  ]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // 手动选择模式
  const [manualMode, setManualMode] = useState(false);
  const [manualTab, setManualTab] = useState<"agent" | "dept" | "skill">("agent");
  const [selAgents, setSelAgents] = useState<Set<string>>(new Set());
  const [selDepts, setSelDepts] = useState<Set<string>>(new Set());
  const [selSkills, setSelSkills] = useState<Set<string>>(new Set());
  const [manualSending, setManualSending] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    setD(await api.get(`/projects/${id}`));
    try { setProgress(await api.get(`/projects/${id}/progress`)); } catch {}
  }, [id]);

  const [chatHist, setChatHist] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderFiles, setFolderFiles] = useState<FolderFile[]>([]);
  useEffect(() => {
    load();
    api.get("/agents").then(setAgents).catch(() => {});
    api.get("/skills").then(setSkills).catch(() => {});
    api.get<Company[]>("/companies").then(async (cs) => {
      if (cs.length) {
        setCid(cs[0].id);
        try { setDepts(await api.get(`/companies/${cs[0].id}/departments`)); } catch {}
      }
    }).catch(() => {});
  }, [load]);

  // 群聊历史加载（cid 就绪后触发一次）
  useEffect(() => {
    if (!cid || !id || chatHist) return;
    setChatHist(true);
    api.get<{ messages?: GroupChatMessage[] }>(`/group-chat/project/${id}/history?company_id=${cid}`)
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

  async function lifecycle(action: "pause" | "resume" | "stop") {
    if (!d) return;
    setBusy(true);
    try {
      // 原来不看返回就重新加载：暂停/停止失败时界面上毫无痕迹
      await api.post(`/projects/${id}/lifecycle`, { action });
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
    setBusy(false);
  }

  async function deleteProject() {
    if (!(await confirmAction({
      title: `彻底删除项目「${d?.name}」？`,
      description: "这是系统中影响面最大的删除操作，建议先确认项目里的成果已导出。",
      bullets: [
        "该项目的全部任务会被删除",
        "全部相关记忆、成果与执行记录一并清除",
        "此操作不可恢复",
      ],
      // 影响面最大的一处：要求逐字输入项目名，与 GitHub 删仓库的做法一致。
      // 名字本身就是最后一次"你真的选对了吗"的确认。
      confirmPhrase: d?.name || "删除",
      confirmText: "删除项目",
    }))) return;
    setBusy(true);
    try {
      await api.post(`/projects/${id}/lifecycle`, { action: "delete" });
      router.push("/projects");
    } catch (e) { setMsg(`❌ ${describeError(e, "删除项目失败")}`); }
    setBusy(false);
  }

  async function addMilestone() {
    if (!milestoneName.trim()) return;
    await api.post(`/projects/${id}/milestones`, { name: milestoneName });
    setMilestoneName("");
    load();
  }

  async function addMember(agentId: string) {
    if (!agentId) return;
    await api.post(`/projects/${id}/members`, { member_type: "agent", member_id: agentId });
    load();
  }

  async function toggleMilestone(m: any) {
    const next = m.status === "COMPLETED" ? "IN_PROGRESS" : "COMPLETED";
    await api.patch(`/milestones/${m.id}`, { status: next });
    load();
  }

  /* ---------- 聊天指令 ---------- */
  async function sendChat() {
    const text = chatInput.trim();
    if (!text || !d) return;
    if (!cid) { setChatLog((prev) => [...prev, { role: "assistant", text: "正在初始化，请稍候1秒再发送…" }]); return; }
    setChatLog((prev) => [...prev, { role: "user", text, isBoss: true }]);
    setChatInput("");
    try {
      const data = await api.post<{ replies?: {
        agent_id?: string; agent_name?: string; text?: string; search_used?: boolean;
      }[] }>(`/group-chat/project/${id}?company_id=${cid}`, { message: text },
        { timeoutMs: 300_000 });
      const replies = data.replies || [];
      setChatLog((prev) => prev.concat(
        replies.map((rep) => ({ role: rep.agent_name || rep.agent_id || 'agent', text: rep.text || '',
                                 isAgent: true, agent: rep.agent_name, search: rep.search_used || false }))));
      await load();
    } catch (e) {
      setChatLog((prev) => [...prev, { role: "assistant", text: `出错了：${describeError(e, "发送失败")}` }]);
    }
  }

  /* ---------- 手动选择提交 ---------- */
  async function submitManual() {
    if (!d) return;
    if (!selAgents.size && !selDepts.size && !selSkills.size) return;
    setManualSending(true);
    const parts: string[] = [];
    if (selAgents.size) {
      parts.push(agents.filter((a) => selAgents.has(a.id)).map((a) => a.name).join("、"));
    }
    if (selDepts.size) {
      parts.push(deptList.filter((x) => selDepts.has(x.id)).map((x) => x.name).join("、"));
    }
    // 先在日志里显示用户操作
    const summary = `⚙ 手动选择：${[
      selAgents.size ? `Agent：${agents.filter((a) => selAgents.has(a.id)).map((a) => a.name).join("、")}` : "",
      selDepts.size ? `部门：${deptList.filter((x) => selDepts.has(x.id)).map((x) => x.name).join("、")}` : "",
      selSkills.size ? `技能 ×${selSkills.size}` : "",
    ].filter(Boolean).join(" | ")}`;
    setChatLog((prev) => [...prev, { role: "user", text: summary }]);
    try {
      const data = await api.post<{ replies?: {
        agent_id?: string; agent_name?: string; text?: string; search_used?: boolean;
      }[] }>(`/group-chat/project/${id}?company_id=${cid}`, {
        message: "__manual__",
        agent_ids: Array.from(selAgents),
        department_ids: Array.from(selDepts),
        skill_ids: Array.from(selSkills),
      }, { timeoutMs: 300_000 });
      const replies = data.replies || [];
      setChatLog((prev) => prev.concat(
        replies.map((rep) => ({ role: rep.agent_name || rep.agent_id || 'agent', text: rep.text || '',
                                 isAgent: true, agent: rep.agent_name, search: rep.search_used || false }))));
      setSelAgents(new Set()); setSelDepts(new Set()); setSelSkills(new Set());
      setManualMode(false);
      await load();
    } catch (e) {
      setChatLog((prev) => [...prev, { role: "assistant", text: `出错了：${describeError(e, "发送失败")}` }]);
    }
    setManualSending(false);
  }

  // 部门列表（从 agents 推导或直接用）
  const deptList = useMemo(() => depts.map((d) => ({ id: d.id, name: d.name })), [depts]);

  /* ---------- 聊天内上传参考材料 ---------- */
  async function uploadFile(file: File) {
    setChatLog((prev) => [...prev, { role: "user", text: `📎 上传了参考资料：${file.name}` }]);
    try {
      // 存入知识库
      const fd = new FormData();
      fd.append("file", file);
      await api.upload("/knowledge/documents", fd);
      setChatLog((prev) => [...prev, {
        role: "assistant",
        text: `✅ 参考资料「${file.name}」已存入知识库并关联到本项目。`,
      }]);
      await load();
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

  const st = STATUS_ZH[d.status] || STATUS_ZH.DRAFT;
  const pct = progress?.percent ?? 0;
  const canPause = d.status === "ACTIVE";
  const canResume = d.status === "PAUSED";
  const canStop = !["COMPLETED", "CANCELLED", "ARCHIVED"].includes(d.status);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头横幅 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative">
          <div className="flex items-center gap-2 mb-3">
            <button onClick={() => router.push("/projects")} className="btn-ghost text-xs">← 返回项目列表</button>
            <button onClick={() => router.push(`/projects/${id}/warroom`)}
              className="text-xs px-3 py-1.5 rounded-lg font-medium text-white transition-all"
              style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
              🎯 项目作战室
            </button>
          </div>
          <div className="flex items-start justify-between flex-wrap gap-4">
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold tracking-tight">{d.name}</h1>
                <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full`}
                  style={{ background: "rgba(255,255,255,0.05)", border: "1px solid var(--border)" }}>
                  <span className={`w-1.5 h-1.5 rounded-full ${st.dot} ${d.status === "ACTIVE" ? "pulse-dot" : ""}`} />
                  <span className={st.cls}>{st.label}</span>
                </span>
              </div>
              <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
                {d.description || "暂无描述"}
              </p>
            </div>
            {/* 生命周期按钮组 */}
            <div className="flex gap-2 flex-wrap">
              {canResume && (
                <button onClick={() => lifecycle("resume")} disabled={busy}
                  className="px-4 py-2 rounded-lg text-sm font-medium transition-all hover:bg-emerald-500/15 text-emerald-300 disabled:opacity-40"
                  style={{ background: "rgba(16,185,129,0.12)", border: "1px solid rgba(16,185,129,0.35)" }}>
                  ▶ 恢复执行
                </button>
              )}
              {canPause && (
                <button onClick={() => lifecycle("pause")} disabled={busy}
                  className="btn-ghost disabled:opacity-40">⏸ 暂停</button>
              )}
              {canStop && (
                <button onClick={() => lifecycle("stop")} disabled={busy}
                  className="btn-ghost disabled:opacity-40">⏹ 停止</button>
              )}
              <button onClick={deleteProject} disabled={busy}
                className="text-sm px-4 py-2 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors disabled:opacity-40"
                style={{ border: "1px solid rgba(248,113,113,0.3)" }}>🗑 删除</button>
            </div>
          </div>

          {/* 进度条 */}
          <div className="relative mt-5 max-w-md">
            <div className="flex justify-between items-center mb-1.5">
              <span className="text-[11px]" style={{ color: "var(--muted)" }}>项目进度</span>
              <span className="text-sm font-bold tabular-nums text-gradient">{pct}%</span>
            </div>
            <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
              <div className="h-full rounded-full transition-all duration-500"
                style={{ width: `${pct}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }} />
            </div>
            {progress && (
              <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                任务 {progress.completed}/{progress.total_tasks} · 运行中 {progress.running} · 里程碑 {progress.milestones_done}/{progress.milestones_total}
              </p>
            )}
          </div>
        </div>
      </div>

      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {/* 主内容两栏：左侧信息，右侧聊天面板 */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-5 items-start">
        {/* 左栏 */}
        <div className="space-y-5 min-w-0">
          {/* 任务列表 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">任务（{d.tasks?.length ?? 0}）</h2>
            <div className="space-y-1.5 max-h-64 overflow-y-auto">
              {(d.tasks || []).map((t) => (
                <Link key={t.id} href={`/tasks/${t.id}`}
                  className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs transition-colors hover:bg-white/[0.04]"
                  style={{ background: "var(--surface-2)" }}>
                  <span className="truncate flex-1">{t.title}</span>
                  <span className="shrink-0" style={{ color: "var(--muted)" }}>{TASK_STATUS_ZH[t.status] || t.status}</span>
                </Link>
              ))}
              {!d.tasks?.length && (
                <p className="text-xs text-center py-4" style={{ color: "var(--muted)" }}>暂无任务</p>
              )}
            </div>
          </div>

          {/* 里程碑 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">里程碑</h2>
            <div className="flex gap-2 mb-3">
              <input value={milestoneName} onChange={(e) => setMilestoneName(e.target.value)}
                placeholder="新里程碑名称" className="input" />
              <button onClick={addMilestone} className="btn-primary shrink-0">添加</button>
            </div>
            <div className="space-y-1.5">
              {(d.milestones || []).map((m) => (
                <button key={m.id} onClick={() => toggleMilestone(m)}
                  className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-xs transition-colors hover:bg-white/[0.04]"
                  style={{ background: "var(--surface-2)" }}>
                  <span>{m.status === "COMPLETED" ? "✅" : m.status === "IN_PROGRESS" ? "🔄" : "⏳"}</span>
                  <span className="truncate">{m.name}</span>
                  <span className="ml-auto shrink-0" style={{ color: "var(--muted)" }}>
                    {m.status === "COMPLETED" ? "已完成" : m.status === "IN_PROGRESS" ? "进行中" : "待开始"}
                  </span>
                </button>
              ))}
              {!d.milestones?.length && (
                <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>暂无里程碑</p>
              )}
            </div>
          </div>

          <DeliverablesPanel projectId={id} />
          <ProjectLogsPanel projectId={id} />

          {/* 团队成员：增 / 减 / 换 */}
          <div className="card card-static">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-sm">项目团队（{d.members?.length ?? 0}）</h2>
              <button onClick={() => setMemberPanelOpen(!memberPanelOpen)}
                className="btn-ghost !py-1 !px-2.5 text-[11px]">
                {memberPanelOpen ? "收起 ＋" : "＋ 添加成员"}
              </button>
            </div>

            {/* 手动添加面板 */}
            {memberPanelOpen && (
              <div className="mb-3 rounded-xl p-3 animate-fade-up"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <div className="max-h-40 overflow-y-auto space-y-1">
                  {agents
                    .filter((a) => !(d.members || []).some((m) => m.member_id === a.id))
                    .map((a, i) => (
                    <button key={a.id}
                      onClick={() => setMemberPick(a.id)}
                      className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                        memberPick === a.id ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                      style={memberPick === a.id ? {} : { borderColor: "transparent" }}>
                      <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                        style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[i % AVATAR_GRADS.length]})` }}>
                        {a.name.slice(0, 1)}
                      </div>
                      <span className="truncate flex-1">{a.name}</span>
                      {a.role && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{a.role}</span>}
                      {memberPick === a.id && <span className="text-indigo-300">✓</span>}
                    </button>
                  ))}
                </div>
                <button onClick={async () => {
                  if (!memberPick) return;
                  await addMember(memberPick);
                  setChatLog((prev) => [...prev, {
                    role: "assistant",
                    text: `✅ 已将「${agents.find((a) => a.id === memberPick)?.name}」加入项目团队。`,
                  }]);
                  setMemberPick("");
                  setMemberPanelOpen(false);
                }} disabled={!memberPick}
                  className="btn-primary !py-1.5 text-xs w-full mt-2 disabled:opacity-40">
                  确认添加
                </button>
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              {(d.members || []).map((mem) => {
                const a = agents.find((x) => x.id === mem.member_id);
                const isManager = d.manager_agent_id === mem.member_id;
                return (
                  <span key={mem.member_id}
                    className="group inline-flex items-center gap-1.5 text-xs pl-2 pr-1 py-1 rounded-full transition-colors"
                    style={{ background: "var(--surface-2)" }}>
                    <span className={`w-1.5 h-1.5 rounded-full ${isManager ? "bg-amber-400" : "bg-indigo-400"}`} />
                    {a?.name || mem.member_id.slice(0, 8)}
                    {isManager && <span className="text-[9px] text-amber-300">负责人</span>}

                    {/* 设为负责人 */}
                    {!isManager && (
                      <button title="设为负责人"
                        onClick={async () => {
                          await api.patch(`/projects/${id}`, { manager_agent_id: mem.member_id });
                          load();
                        }}
                        className="w-4 h-4 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity hover:bg-amber-500/20 text-[9px]"
                        style={{ color: "#fbbf24" }}>★</button>
                    )}

                    {/* 移除成员 */}
                    <button title="移出团队"
                      onClick={async () => {
                        if (!(await confirmAction({
                          title: `将「${a?.name || mem.member_id.slice(0, 8)}」移出项目团队？`,
                          description: "该员工本人与其记忆、成果都不会被删除，只是不再参与本项目。",
                          confirmText: "移出",
                        }))) return;
                        await api.del(`/projects/${id}/members/${mem.member_id}`);
                        if (isManager) {
                          await api.patch(`/projects/${id}`, { manager_agent_id: null });
                        }
                        setMsg(`已移除 ${a?.name || "成员"}`);
                        load();
                      }}
                      className="w-4 h-4 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity hover:bg-red-500/20 text-red-400 text-[10px]">✕</button>
                  </span>
                );
              })}
              {!d.members?.length && (
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  暂无成员 · 可在右侧聊天面板说「让XX来负责」快速指派
                </p>
              )}
            </div>
          </div>
        </div>

        {/* 右栏：AI 聊天面板（复用全站聊天样式） */}
        {chatOpen && (
          <div className="card flex flex-col lg:sticky lg:top-4"
            style={{ height: "calc(100vh - 220px)", minHeight: 480 }}>
            <div className="flex items-center justify-between pb-2.5 border-b" style={{ borderColor: "var(--border)" }}>
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-xl flex items-center justify-center text-white text-sm"
                  style={{ background: "linear-gradient(135deg,#6366f1,#a78bfa)" }}>🤖</div>
                <div>
                  <h2 className="font-semibold text-sm">项目助手</h2>
                  <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                    换Agent · 部门承接 · 加技能 · 传资料
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button title="项目交付文件"
                  onClick={async () => {
                    setFolderOpen(!folderOpen);
                    if (!folderOpen && cid) {
                      try {
                        const data = await api.get<{ files?: FolderFile[] }>(
                          `/delivery/by-scope?company_id=${cid}&scope=project&scope_id=${id}`);
                        setFolderFiles(data.files || []);
                      } catch {
                        // 交付文件拉不到不影响对话
                        setFolderFiles([]);
                      }
                    }
                  }}
                  className={`w-7 h-7 rounded-lg flex items-center justify-center text-xs transition-colors ${folderOpen ? "bg-indigo-500/20 text-indigo-300" : "hover:bg-white/[0.06]"}`}
                  style={folderOpen ? {} : { color: "var(--muted)" }}>📁</button>
                <button onClick={() => setChatOpen(false)} className="btn-ghost !px-2 !py-1 text-xs">—</button>
              </div>
            </div>

            {/* 交付文件夹面板 */}
            {folderOpen && (
              <div className="my-2 rounded-xl p-3 animate-fade-up overflow-y-auto max-h-40 space-y-1 shrink-0"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <p className="text-[11px] font-medium mb-1">📁 项目交付文件（{folderFiles.length}）</p>
                {folderFiles.map((f) => (
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
                const isErr = !m.isAgent && !isBoss && m.role === "assistant";
                const isAgent = m.isAgent === true;
                if (isErr) return (
                  <div key={i} className="p-2.5 rounded-xl text-xs" style={{ background: "rgba(239,68,68,0.10)" }}>
                    ⚠️ {m.text}
                  </div>
                );
                return (
                  <div key={i} className={`flex ${isBoss ? "justify-end" : "justify-start"}`}>
                    <div className={`max-w-[82%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap` + (isBoss
                      ? " text-white" : "")}
                      style={isBoss ? { background: "linear-gradient(135deg,#4f46e5,#7c3aed)" }
                        : { background: "var(--surface-2)", color: "var(--foreground)" }}>
                      {isAgent && (
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="w-5 h-5 rounded-full inline-flex items-center justify-center text-[10px]"
                            style={{ background: "rgba(99,102,241,0.25)" }}>🤖</span>
                          <span className="font-semibold text-indigo-200 text-[10px]">{m.agent}</span>
                          {m.search && <span className="text-[9px] text-cyan-300" title="使用了搜索">🔍搜索</span>}
                        </div>
                      )}
                      {m.text}
                    </div>
                  </div>
                );
              })}
              <div ref={chatEndRef} />
            </div>

            {/* 手动选择面板 */}
            {manualMode && (
              <div className="mb-2 rounded-xl p-3 space-y-2.5 animate-fade-up"
                style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
                <div className="flex gap-1.5">
                  {([["agent", "🤖 Agent"], ["dept", "📁 部门"], ["skill", "⚡ 技能"]] as const).map(([v, label]) => (
                    <button key={v} onClick={() => setManualTab(v)}
                      className={`px-2.5 py-1 rounded-full text-[11px] transition-all border ${
                        manualTab === v ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                                        : "hover:bg-white/[0.04]"}`}
                      style={manualTab === v ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                      {label}
                    </button>
                  ))}
                  <span className="ml-auto text-[10px] self-center" style={{ color: "var(--muted)" }}>
                    已选 {selAgents.size + selDepts.size + selSkills.size}
                  </span>
                </div>

                <div className="max-h-40 overflow-y-auto space-y-1">
                  {manualTab === "agent" && agents.map((a, i) => {
                    const on = selAgents.has(a.id);
                    return (
                      <button key={a.id}
                        onClick={() => setSelAgents((prev) => {
                          const n = new Set(prev); if (n.has(a.id)) n.delete(a.id); else n.add(a.id); return n;
                        })}
                        className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                          on ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "transparent" }}>
                        <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                          style={{ background: `linear-gradient(135deg, ${AVATAR_GRADS[i % AVATAR_GRADS.length]})` }}>
                          {a.name.slice(0, 1)}
                        </div>
                        <span className="truncate flex-1">{a.name}</span>
                        {a.role && <span className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{a.role}</span>}
                        {on && <span className="text-indigo-300 shrink-0">✓</span>}
                      </button>
                    );
                  })}
                  {manualTab === "dept" && deptList.map((dp) => {
                    const on = selDepts.has(dp.id);
                    return (
                      <button key={dp.id}
                        onClick={() => setSelDepts((prev) => {
                          const n = new Set(prev); if (n.has(dp.id)) n.delete(dp.id); else n.add(dp.id); return n;
                        })}
                        className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-all border ${
                          on ? "border-cyan-500/50 bg-cyan-500/10" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "transparent" }}>
                        📁 <span className="truncate flex-1">{dp.name}</span>
                        {on && <span className="text-cyan-300 shrink-0">✓</span>}
                      </button>
                    );
                  })}
                  {manualTab === "skill" && skills.map((sk: any) => {
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
                        {on && <span className="text-emerald-300 shrink-0">✓</span>}
                      </button>
                    );
                  })}
                </div>

                <div className="flex gap-2">
                  <button onClick={submitManual}
                    disabled={manualSending || (!selAgents.size && !selDepts.size && !selSkills.size)}
                    className="btn-primary !py-1.5 text-xs flex-1 disabled:opacity-40">
                    {manualSending ? "执行中…" : `确认加入（${selAgents.size + selDepts.size + selSkills.size} 项）`}
                  </button>
                  <button onClick={() => { setManualMode(false);
                    setSelAgents(new Set()); setSelDepts(new Set()); setSelSkills(new Set()); }}
                    className="btn-ghost text-xs">取消</button>
                </div>
              </div>
            )}

            {/* 输入区 */}
            <div className="pt-2.5 border-t flex items-center gap-2" style={{ borderColor: "var(--border)" }}>
              {!manualMode && (
                <button title="手动选择 Agent / 部门 / 技能"
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
                placeholder="输入指令，如「让战略分析师负责」…"
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
            title="打开项目助手">🤖</button>
        )}
      </div>
    </div>
  );
}
