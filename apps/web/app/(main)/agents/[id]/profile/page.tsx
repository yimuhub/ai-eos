"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";

/* ============================================================
   综合档案页：成长 / 训练 / 技能 / 私人知识库 / 复盘 / 考核 / 职业
   全部可操作：技能可删除/添加，记忆可删除/添加，复盘可删除
   ============================================================ */

type Profile = {
  profile: { identity: string; capabilities: string[]; version: number };
  stats: { tasks_total: number; tasks_completed: number; tasks_failed: number; meeting_spoke_count: number };
  capability_history: { version: number; capabilities: string[]; reason: string }[];
  growth_events: { type: string; title: string; created_at: string }[];
  learning_records: { id: string; goal: string; status: string; progress: number }[];
};
type Skill = { id: string; skill: string; level: string; experience_count: number; evidence_count: number; source_type: string };
type Lesson = { id: string; lesson: string };
type Review = { id: string; source: string; score: number };
type CareerRec = { title: string; change_type: string; is_current: boolean };
type MemoryItem = { id: string; content: string; type: string; created_at: string };

const TABS = ["成长", "训练", "技能", "私人知识库", "记忆", "复盘", "考核", "职业", "邮箱", "下班学习", "工牌"] as const;
const LEVEL_ZH: Record<string, string> = {
  BEGINNER: "入门", JUNIOR: "初级", INTERMEDIATE: "中级", ADVANCED: "高级", EXPERT: "专家",
};
const MEM_TYPE_ZH: Record<string, string> = {
  EPISODIC: "事件", SEMANTIC: "语义", PROCEDURAL: "程序", WORKING: "工作", REFLECTION: "反思",
};

export default function AgentProfilePage() {
  const { id: agentId } = useParams<{ id: string }>();
  const router = useRouter();
  // 激活公司：可订阅 —— 切公司后技能库 / 记忆自动换数据，不再一次性取 cs[0]
  const cid = useActiveCompanyId();
  const [agentName, setAgentName] = useState("");
  const [tab, setTab] = useState<(typeof TABS)[number]>("成长");

  /* ---------- 数据（统一交给 useResource：去重、缓存、回窗重取） ---------- */
  // 员工列表（只取名字）与工牌
  const { data: agentRows } = useResource<{ id: string; name: string }[]>(
    "agents:all",
    () => api.get<{ id: string; name: string }[]>("/agents"),
  );
  useEffect(() => {
    setAgentName(agentRows?.find((a) => a.id === agentId)?.name || "");
  }, [agentRows, agentId]);

  // V5.8 ⑨ Agent Card（员工工牌）：能力清单 + needs-auth 态
  const { data: card } = useResource<any>(
    agentId ? `agent:card:${agentId}` : null,
    () => api.get(`/agents/${agentId}/card`),
  );

  // V5.7 ⑥⑦ 下班学习：工作日志列表 + 写日志 + 老板审批（成长也要点头）
  const { data: worklogRows, refresh: refreshWorklogs } = useResource<any[]>(
    agentId ? `agent:worklogs:${agentId}` : null,
    async () => {
      const r = await api.get<any[]>(`/worklogs?agent_id=${agentId}`);
      return Array.isArray(r) ? r : [];
    },
  );
  const worklogs = useMemo<any[]>(() => worklogRows ?? [], [worklogRows]);
  const [wlContent, setWlContent] = useState("");
  const [wlLessons, setWlLessons] = useState("");
  const [wlMsg, setWlMsg] = useState("");

  async function postWorklog() {
    if (!wlContent.trim()) { notify("warning", "先写点日志内容"); return; }
    try {
      const lessons = wlLessons.trim() ? wlLessons.split("\n").map(s => s.trim()).filter(Boolean) : null;
      await api.post(`/worklogs?agent_id=${agentId}`, {
        agent_id: agentId, content: wlContent.trim(),
        lessons, period: "daily",
      });
      setWlContent(""); setWlLessons("");
      setWlMsg("✅ 日志已提交，等老板审批后沉淀为高置信记忆");
      refreshWorklogs();
    } catch (e: any) { setWlMsg(`❌ ${e.message || "提交失败"}`); }
  }

  async function decideWorklog(wl: any, decision: "approve" | "reject") {
    try {
      await api.post(`/worklogs/${wl.id}/decide?agent_id=${agentId}`,
        { decision, comment: decision === "reject" ? "老板否决" : null });
      refreshWorklogs();
      notify("success", decision === "approve" ? "已批准，已沉淀为高置信记忆（0.95）" : "已否决");
    } catch (e: any) { notify("error", e.message || "决策失败"); }
  }

  // 档案类资源：参数只依赖路由 id（记忆额外依赖公司）
  const { data: profile, refresh: refreshProfile } = useResource<Profile>(
    agentId ? `agent:profile:${agentId}` : null,
    () => api.get<Profile>(`/learning/agents/${agentId}/profile`),
  );
  const { data: skillRows, refresh: refreshSkills } = useResource<Skill[]>(
    agentId ? `agent:skills:${agentId}` : null,
    () => api.get<Skill[]>(`/skill-growth/agents/${agentId}/matrix`),
  );
  const skills = useMemo<Skill[]>(() => skillRows ?? [], [skillRows]);
  const { data: lessonRows, refresh: refreshLessons, mutate: mutateLessons } = useResource<Lesson[]>(
    agentId ? `agent:lessons:${agentId}` : null,
    () => api.get<Lesson[]>(`/reflection/agents/${agentId}/lessons`),
  );
  const lessons = useMemo<Lesson[]>(() => lessonRows ?? [], [lessonRows]);
  const { data: reviewRows, refresh: refreshReviews } = useResource<Review[]>(
    agentId ? `agent:reviews:${agentId}` : null,
    () => api.get<Review[]>(`/reviews/agents/${agentId}`),
  );
  const reviews = useMemo<Review[]>(() => reviewRows ?? [], [reviewRows]);
  const { data: careerRows, refresh: refreshCareer } = useResource<CareerRec[]>(
    agentId ? `agent:career:${agentId}` : null,
    () => api.get<CareerRec[]>(`/career/agents/${agentId}/timeline`),
  );
  const career = useMemo<CareerRec[]>(() => careerRows ?? [], [careerRows]);
  // V5.5 C3：Agent 专属邮箱（收件箱 / 代答 / 别名）
  const { data: mailboxRows, refresh: refreshMailbox } = useResource<any[]>(
    agentId ? `agent:mailboxes:${agentId}` : null,
    () => api.get<any[]>(`/mailboxes?agent_id=${agentId}`),
  );
  const mailbox = mailboxRows?.[0] || null;
  // 收件箱依赖 mailbox.id：mailbox 未就绪时 key 为 null，不发请求
  const { data: messageRows, refresh: refreshMessages } = useResource<any[]>(
    mailbox ? `mailbox:messages:${mailbox.id}` : null,
    () => api.get<any[]>(`/mailboxes/messages?mailbox_id=${mailbox.id}`),
  );
  const messages = useMemo<any[]>(() => messageRows ?? [], [messageRows]);
  const [aliasInput, setAliasInput] = useState("");
  const [replyBody, setReplyBody] = useState("");
  const [replyTo, setReplyTo] = useState<string | null>(null);
  const [inSubject, setInSubject] = useState("");
  const [inBody, setInBody] = useState("");

  const createMailbox = async () => {
    if (!aliasInput.trim()) { notify("warning", "⚠️ 先填邮箱别名"); return; }
    try {
      const r = await api.post(`/mailboxes`, { agent_id: agentId, alias: aliasInput.trim() });
      notify("success", `✅ 已开通 ${r.alias}`);
      setAliasInput("");
      refreshMailbox();
      refreshMessages();
    } catch (e: any) { notify("error", `❌ ${describeError(e)}`); }
  };
  const sendInboxMsg = async () => {
    if (!mailbox) return;
    try {
      await api.post(`/mailboxes/messages`, { mailbox_id: mailbox.id, subject: inSubject || "老板来信", body: inBody, from_addr: "boss@aieos.local" });
      setInSubject(""); setInBody("");
      refreshMessages();
      notify("success", "✅ 已写入收件箱");
    } catch (e: any) { notify("error", `❌ ${describeError(e)}`); }
  };
  const doReply = async (mid: string) => {
    if (!mailbox) return;
    try {
      await api.post(`/mailboxes/messages/${mid}/reply`, { body: replyBody || "（代答）收到，谢谢。" });
      setReplyBody(""); setReplyTo(null);
      refreshMessages();
      notify("success", "✅ 代答已发出");
    } catch (e: any) { notify("error", `❌ ${describeError(e)}`); }
  };
  const { data: nodeRows } = useResource<any[]>(
    cid ? `agent:skill-nodes:${cid}` : null,
    () => api.get<any[]>(`/skills?company_id=${cid}`),
  );
  const nodes = useMemo<any[]>(() => nodeRows ?? [], [nodeRows]);
  const [trTitle, setTrTitle] = useState("");
  const [trInput, setTrInput] = useState("");
  const [trUploading, setTrUploading] = useState(false);
  const trFileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const { data: kbDocRows, refresh: kbRefresh } = useResource<any[]>(
    agentId ? `agent:kbdocs:${agentId}` : null,
    () => api.get<any[]>(`/kb2/agents/${agentId}/documents`),
  );
  const kbDocs = useMemo<any[]>(() => kbDocRows ?? [], [kbDocRows]);
  const kbFileRef = useRef<HTMLInputElement>(null);
  const [uploadNote, setUploadNote] = useState("");
  const [msg, setMsg] = useState("");
  // 记忆新增
  const [memContent, setMemContent] = useState("");
  const [memType, setMemType] = useState("EPISODIC");
  const [showAddMem, setShowAddMem] = useState(false);
  // 学习技能
  const [showLearn, setShowLearn] = useState(false);

  // 员工长期记忆（来自 growth-center 详情，按公司隔离）
  const { data: memoryRows, refresh: refreshMemories } = useResource<MemoryItem[]>(
    agentId && cid ? `agent:memories:${cid}:${agentId}` : null,
    async () => {
      const d = await api.get<{ memories?: MemoryItem[] }>(
        `/growth-center/agents/${agentId}/detail?company_id=${cid}`);
      return d?.memories || [];
    },
  );
  const memories = useMemo<MemoryItem[]>(() => memoryRows ?? [], [memoryRows]);

  // 写操作后刷新档案类数据（原 refreshAll 的职责）
  const refreshAll = useCallback(() => {
    refreshProfile();
    refreshSkills();
    refreshLessons();
    refreshReviews();
    refreshCareer();
    refreshMemories();
  }, [refreshProfile, refreshSkills, refreshLessons, refreshReviews, refreshCareer, refreshMemories]);

  async function uploadKbFile(f: File) {
    if (!agentId) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const d = await api.upload<{ chunks?: number }>(
        `/kb2/agents/${agentId}/upload`, fd);
      notify("success", `✅ ${f.name} 已入私人知识库（${d.chunks ?? 0} 块索引）`);
      kbRefresh();
    } catch (e) {
      notify("error", `❌ ${describeError(e, "上传失败")}`);
    }
    setBusy(false);
  }

  async function uploadTrainingFile(f: File) {
    if (!agentId || !cid) return;
    setTrUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const d = await api.upload<{ ok?: boolean; chunks?: number }>(
        `/training/upload-material?agent_id=${agentId}&company_id=${cid}`, fd);
      if (d.ok) {
        setTrInput((prev: string) => (prev ? prev + "\n\n" : "") + `【来自附件 ${f.name} 的内容已入库私人知识库】`);
        setMsg(`✅ 附件 ${f.name} 已转 MD 存入私人知识库（${d.chunks} 块）`);
      } else setMsg("❌ 附件解析失败：未能从该文件提取到文本");
    } catch (e) { setMsg(`❌ ${describeError(e, "附件上传失败")}`); }
    setTrUploading(false);
  }

  async function createTraining() {
    if (!trTitle || !agentId) return;
    if (!trInput.trim()) { setMsg("请输入训练材料或上传附件"); return; }
    setBusy(true);
    try {
      await api.post(`/training/sessions?agent_id=${agentId}`,
        { training_type: "CONVERSATION", title: trTitle, raw_input: trInput });
      setMsg("✅ 训练完成");
      refreshAll();
      setTrTitle(""); setTrInput("");
    } catch (e: any) { setMsg("❌ " + (e.message || String(e))); }
    setBusy(false);
  }

  async function acquireOne(slug: string, nodeName: string) {
    if (!agentId) return;
    setBusy(true);
    try {
      await api.post(`/skills/${slug}/install`, { agent_id: agentId });
      setMsg(`✅ 已添加「${nodeName}」`);
      refreshAll();
    } catch (e: any) { setMsg("❌ " + (e.message || String(e))); }
    setBusy(false);
  }

  async function acquireAll() {
    if (!agentId) return;
    const available = availableNodes();
    if (!available.length) { setMsg("所有技能已学完"); return; }
    setBusy(true);
    let ok = 0, fail = 0;
    for (const n of available) {
      try {
        await api.post(`/skills/${n.slug}/install`, { agent_id: agentId });
        ok++;
      } catch { fail++; }
    }
    setMsg(`✅ 已添加 ${ok} 个技能${fail ? `，${fail} 个失败` : ""}`);
    refreshAll();
    setBusy(false);
  }

  async function removeSkill(asnId: string) {
    if (!(await confirmAction({
      title: "确认移除这项技能？",
      description: "移除后该员工将无法再执行依赖这项技能的任务，可稍后重新学习。",
      confirmText: "移除",
    }))) return;
    if (!cid) return;
    try {
      await api.del(`/skill-growth/agents/${agentId}/skills/${asnId}?company_id=${cid}`);
      setMsg("✅ 已移除技能");
      refreshAll();
    } catch (e: any) { setMsg("❌ 移除失败"); }
  }

  async function addMemory() {
    if (!memContent.trim() || !agentId || !cid) return;
    try {
      const r = await api.post("/growth-center/memories", {
        agent_id: agentId, company_id: cid,
        content: memContent.trim(), memory_type: memType,
      });
      if (r.ok) {
        setMemContent("");
        setShowAddMem(false);
        setMsg("✅ 已添加记忆");
        refreshAll();
      }
    } catch (e: any) { setMsg("❌ 添加失败"); }
  }

  async function removeMemory(memoryId: string) {
    if (!(await confirmAction({
      title: "确认删除这条记忆？",
      description: "员工将不再记得这条事实，后续任务中不会再引用。",
      confirmText: "删除",
    }))) return;
    if (!cid) return;
    try {
      await api.del(`/growth-center/memories/${memoryId}?company_id=${cid}`);
      setMsg("✅ 已删除记忆");
      refreshAll();
    } catch (e: any) { setMsg("❌ 删除失败"); }
  }

  async function removeLesson(lessonId: string) {
    if (!(await confirmAction({
      title: "确认删除这条复盘？",
      description: "这条经验教训将不再参与该员工后续的决策。",
      confirmText: "删除",
    }))) return;
    try {
      await api.del(`/reflection/agents/${agentId}/lessons/${lessonId}`);
      mutateLessons(lessons.filter((l) => l.id !== lessonId));
      notify("success", "已删除该条复盘");
    } catch (e) {
      // 原实现 `catch { /* ignore */ }` 让删除失败完全无声
      notify("error", e instanceof Error ? e.message : "删除失败");
    }
  }

  const availableNodes = () => {
    const owned = new Set(skills.map((s: Skill) => s.skill));
    return nodes
      .filter((n: any) => !owned.has(n.name))
      .sort((a: any, b: any) => (a.scope === "public" ? -1 : 1) - (b.scope === "public" ? -1 : 1));
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(99,102,241,0.12) 0%, rgba(168,85,247,0.08) 100%)", border: "1px solid rgba(99,102,241,0.25)" }}>
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg, #6366f1, #a855f7)" }}>
            👤
          </div>
          <div>
            <h1 className="text-xl font-bold text-[var(--foreground)]">{agentName || "员工"}</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              综合档案 · 技能 / 记忆 / 复盘 · 全部可操作
            </p>
          </div>
          <div className="ml-auto flex gap-2">
            <button onClick={() => router.push(`/agents/${agentId}`)}
              className="px-3 py-1.5 rounded-full text-xs border transition-all hover:bg-white/5"
              style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
              ← 返回对话
            </button>
          </div>
        </div>
      </div>

      {/* 标签导航 */}
      <div className="flex gap-1 mb-5 overflow-x-auto pb-1"
        style={{ borderBottom: "1px solid var(--border)" }}>
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap ${
              tab === t ? "text-white" : "text-[var(--muted)] hover:text-gray-200"
            }`}
            style={tab === t
              ? { background: "linear-gradient(135deg, rgba(99,102,241,0.3), rgba(168,85,247,0.2))", borderBottom: "2px solid #6366f1" }
              : {}}>
            {t}
          </button>
        ))}
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.2)" }}>
          {msg}
        </div>
      )}

      {/* ---- 成长 ---- */}
      {tab === "成长" && profile && (
        <div className="space-y-3">
          <div className="card card-static text-sm text-gray-200 p-4">
            <b className="text-[var(--foreground)]">身份</b>：{profile.profile.identity}<br />
            <b className="text-[var(--foreground)]">能力</b>：{profile.profile.capabilities.join("、") || "未设置"}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {([
              ["任务总数", profile.stats.tasks_total],
              ["已完成", profile.stats.tasks_completed],
              ["失败", profile.stats.tasks_failed],
              ["会议发言", profile.stats.meeting_spoke_count],
              ["档案版本", `v${profile.profile.version}`],
            ] as const).map(([k, v]) => (
              <div key={k} className="card card-static p-3 text-center">
                <div className="text-xs" style={{ color: "var(--muted)" }}>{k}</div>
                <div className="text-lg font-bold text-[var(--foreground)] mt-1">{v}</div>
              </div>
            ))}
          </div>
          <h4 className="text-sm font-medium text-gray-200">能力版本历史</h4>
          {profile.capability_history.map((c) => (
            <div key={c.version} className="card card-static !py-2 text-sm text-gray-200 px-3">
              <b className="text-[var(--foreground)]">v{c.version}</b> — {(c.capabilities || []).join("、")}
              <br /><span className="text-xs" style={{ color: "var(--muted)" }}>{c.reason}</span>
            </div>
          ))}
          <h4 className="text-sm font-medium text-gray-200 mt-3">学习计划</h4>
          {profile.learning_records.map((l) => (
            <div key={l.id} className="card card-static !py-2 text-sm text-gray-200 px-3 flex items-center gap-3">
              <span className="flex-1">{l.goal}</span>
              <div className="w-20 h-1.5 bg-white/[0.08] rounded-full overflow-hidden">
                <div className="h-1.5 bg-indigo-500 rounded-full" style={{ width: `${l.progress}%` }} />
              </div>
              <span className="text-xs" style={{ color: "var(--muted)" }}>{l.status} {l.progress}%</span>
            </div>
          ))}
        </div>
      )}

      {/* ---- 训练（含附件上传）---- */}
      {tab === "训练" && (
        <div className="space-y-3 max-w-2xl">
          <div className="card card-static p-4 space-y-2.5">
            <input value={trTitle} onChange={(e) => setTrTitle(e.target.value)}
              placeholder="训练标题" className="input" />
            <textarea value={trInput} onChange={(e) => setTrInput(e.target.value)}
              placeholder="输入训练材料…（也可点下方 📎 上传附件，自动转 MD 入私人知识库）"
              className="input" rows={4} />
            {/* 附件上传 */}
            <input ref={trFileRef} type="file" className="hidden"
              accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls,.pptx"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadTrainingFile(f);
                e.target.value = "";
              }} />
            <div className="flex items-center gap-2 flex-wrap">
              <button onClick={() => trFileRef.current?.click()} disabled={trUploading}
                className="px-3 py-1.5 rounded-lg text-xs font-medium transition-all disabled:opacity-40"
                style={{ background: "rgba(99,102,241,0.12)", border: "1px solid rgba(99,102,241,0.4)", color: "#a5b4fc" }}>
                {trUploading ? "⏳ 解析并入库中…" : "📎 上传训练资料附件"}
              </button>
              <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                支持 docx/pdf/md/txt/xlsx/pptx → 自动转 Markdown
              </span>
            </div>
            <button onClick={createTraining}
              disabled={busy || !trTitle || !trInput.trim()}
              className="btn-primary disabled:opacity-40">开始训练</button>
          </div>
        </div>
      )}

      {/* ---- 技能（可删除 + 可添加）---- */}
      {tab === "技能" && (
        <div className="space-y-4 max-w-3xl">
          {/* 工具栏 */}
          <div className="flex items-center gap-3">
            <button onClick={() => setShowLearn(!showLearn)}
              className="px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
              style={{ background: showLearn ? "rgba(99,102,241,0.2)" : "rgba(255,255,255,0.05)",
                border: "1px solid rgba(99,102,241,0.4)", color: "#a5b4fc" }}>
              {showLearn ? "▲ 收起可学技能" : `+ 添加技能 (${availableNodes().length})`}
            </button>
            {availableNodes().length > 0 && (
              <button onClick={acquireAll} disabled={busy}
                className="px-3 py-1.5 rounded-lg text-xs bg-emerald-700 hover:bg-emerald-600 text-white transition-colors disabled:opacity-40">
                学全部 ({availableNodes().length})
              </button>
            )}
          </div>

          {/* 已掌握技能 */}
          <div>
            <p className="text-[11px] font-medium mb-2" style={{ color: "var(--muted)" }}>
              💎 已掌握技能（{skills.length} 项 · 点击 🗑 可移除）
            </p>
            {skills.map((s) => (
              <div key={s.id} className="card card-static !py-2.5 mb-1.5 flex items-center gap-3 text-sm"
                style={{ borderColor: "rgba(236,72,153,0.25)" }}>
                <span className="flex-1 text-[var(--foreground)]">{s.skill}</span>
                <span className="text-xs px-2 py-0.5 rounded-full"
                  style={{ background: "rgba(99,102,241,0.12)", color: "#a5b4fc" }}>
                  {LEVEL_ZH[s.level] || s.level}
                </span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>
                  证据{s.evidence_count} · 经验{s.experience_count}
                </span>
                <button onClick={() => removeSkill(s.id)}
                  className="text-red-400/70 hover:text-red-300 text-xs px-2 py-0.5 rounded-md hover:bg-red-500/10 transition-colors"
                  title="移除这项技能">
                  🗑
                </button>
              </div>
            ))}
            {!skills.length && (
              <p className="text-sm py-6 text-center" style={{ color: "var(--muted)" }}>
                暂无掌握技能 · 点击下方按钮学习
              </p>
            )}
          </div>

          {/* 可学技能 */}
          {showLearn && (
            <div>
              <p className="text-[11px] font-medium mb-2" style={{ color: "var(--muted)" }}>
                🌐 可学习技能（点击添加）
              </p>
              {availableNodes().length === 0 ? (
                <p className="text-sm py-4 text-center" style={{ color: "var(--muted)" }}>所有技能已学完 ✅</p>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {availableNodes().map((n: any) => (
                    <button key={n.slug} onClick={() => acquireOne(n.slug, n.name)}
                      disabled={busy}
                      className="card card-static !py-2 text-sm text-left hover:scale-[1.02] transition-all disabled:opacity-40 flex items-center gap-2"
                      style={{ border: "1px solid rgba(99,102,241,0.2)" }}>
                      <span>{n.scope === "public" ? "🌐" : "💎"}</span>
                      <span className="text-[var(--foreground)] flex-1 truncate">+ {n.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ---- 私人知识库 ---- */}
      {tab === "私人知识库" && (
        <div className="space-y-3 max-w-3xl">
          <div className="card card-static !py-3 flex items-center gap-3">
            {/* 修复：此处曾是一个没有触发按钮的死文件输入（ref 被强制传
                undefined）——文案承诺"上传资料后仅此 Agent 可检索"，实际
                无任何可点的东西。现在接通 /kb2/agents/{id}/upload。 */}
            <input ref={kbFileRef} type="file" multiple className="hidden"
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                e.target.value = "";
                files.forEach(uploadKbFile);
              }} />
            <button onClick={() => kbFileRef.current?.click()} disabled={busy}
              className="btn-primary !py-1 text-xs disabled:opacity-40">
              {busy ? "上传中…" : "📎 上传资料"}
            </button>
            <p className="text-[11px]" style={{ color: "var(--muted)" }}>
              上传资料后仅此 Agent 可检索使用
            </p>
          </div>
          {kbDocs.map((d: any) => (
            <div key={d.id} className="card card-static !py-2.5 flex items-center gap-3 text-sm">
              <span className="truncate flex-1">📄 {d.title}</span>
              <span className="text-xs" style={{ color: "var(--muted)" }}>{d.chunk_count} 块</span>
            </div>
          ))}
          {!kbDocs.length && (
            <p className="text-sm py-6 text-center" style={{ color: "var(--muted)" }}>私人知识库为空</p>
          )}
        </div>
      )}

      {/* ---- 记忆（可删除 + 可添加）---- */}
      {tab === "记忆" && (
        <div className="space-y-4 max-w-3xl">
          {/* 添加记忆 */}
          <div className="card card-static p-4">
            <div className="flex items-center gap-2 mb-3">
              <button onClick={() => setShowAddMem(!showAddMem)}
                className="px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
                style={{ background: showAddMem ? "rgba(99,102,241,0.2)" : "rgba(255,255,255,0.05)",
                  border: "1px solid rgba(99,102,241,0.4)", color: "#a5b4fc" }}>
                {showAddMem ? "▲ 收起添加框" : "+ 添加记忆"}
              </button>
            </div>
            {showAddMem && (
              <div className="space-y-2">
                <select value={memType} onChange={(e) => setMemType(e.target.value)}
                  className="input">
                  {Object.entries(MEM_TYPE_ZH).map(([k, v]) => (
                    <option key={k} value={k}>{v}（{k}）</option>
                  ))}
                </select>
                <textarea value={memContent} onChange={(e) => setMemContent(e.target.value)}
                  placeholder="输入记忆内容……" className="input h-20" />
                <button onClick={addMemory} disabled={!memContent.trim()}
                  className="btn-primary text-xs disabled:opacity-40">
                  保存记忆
                </button>
              </div>
            )}
          </div>

          {/* 记忆列表 */}
          <div>
            <p className="text-[11px] font-medium mb-2" style={{ color: "var(--muted)" }}>
              🧠 长期记忆（{memories.length} 条 · 点击 🗑 可删除）
            </p>
            {memories.map((m) => (
              <div key={m.id} className="card card-static !py-2.5 mb-1.5 flex items-start gap-3 text-sm">
                <span className="flex-1 text-gray-200">{m.content}</span>
                <span className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0"
                  style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc" }}>
                  {MEM_TYPE_ZH[m.type] || m.type}
                </span>
                <span className="text-xs" style={{ color: "var(--muted)" }}>{m.created_at}</span>
                <button onClick={() => removeMemory(m.id)}
                  className="text-red-400/70 hover:text-red-300 text-xs px-1.5 py-0.5 rounded-md hover:bg-red-500/10 transition-colors shrink-0"
                  title="删除这条记忆">
                  🗑
                </button>
              </div>
            ))}
            {!memories.length && (
              <p className="text-sm py-6 text-center" style={{ color: "var(--muted)" }}>暂无记忆</p>
            )}
          </div>
        </div>
      )}

      {/* ---- 复盘 ---- */}
      {tab === "复盘" && (
        <div className="space-y-2 max-w-2xl">
          {(lessons || []).map((l) => (
            <div key={l.id} className="card card-static !py-2.5 flex items-center gap-3 text-sm">
              <span className="flex-1">💡 {l.lesson}</span>
              <button onClick={() => removeLesson(l.id)}
                className="text-red-400/70 hover:text-red-300 text-xs px-2 py-0.5 rounded-md hover:bg-red-500/10 transition-colors">
                🗑
              </button>
            </div>
          ))}
          {!lessons.length && (
            <p className="text-sm py-8 text-center" style={{ color: "var(--muted)" }}>暂无复盘记录</p>
          )}
        </div>
      )}

      {/* ---- 考核 ---- */}
      {tab === "考核" && (
        <div className="space-y-2 max-w-2xl">
          {(reviews || []).map((r) => (
            <div key={r.id} className="card card-static !py-2.5 flex items-center justify-between text-sm">
              <span>{r.source}</span>
              <span className="font-bold text-gradient">{r.score} 分</span>
            </div>
          ))}
          {!reviews.length && (
            <p className="text-sm py-8 text-center" style={{ color: "var(--muted)" }}>暂无考核记录</p>
          )}
        </div>
      )}

      {/* ---- 邮箱（V5.5 C3）---- */}
      {tab === "邮箱" && (
        <div className="space-y-4 max-w-3xl">
          {!mailbox && (
            <div className="card card-static p-4 space-y-2">
              <div className="text-sm font-medium">📧 为该员工开通专属邮箱</div>
              <p className="text-xs text-[var(--muted)]">
                开通后，外部来信（站内 / 局域网通道）进这个收件箱，老板可代答；配 SMTP 后可真实收发
              </p>
              <div className="flex gap-2">
                <input value={aliasInput} onChange={e => setAliasInput(e.target.value)}
                  placeholder="别名，如 chen / zhang@yourcompany.aieos"
                  className="input-glow flex-1" />
                <button onClick={createMailbox} className="btn-primary">开通</button>
              </div>
            </div>
          )}
          {mailbox && (
            <>
              <div className="card card-static p-3 flex items-center gap-3 text-sm">
                <span className="font-medium">📧 {mailbox.alias}</span>
                <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300">已开通</span>
                <span className="text-xs text-[var(--muted)] ml-auto">自动回复：{mailbox.auto_reply ? "开" : "关"}</span>
                <button onClick={() => { refreshMailbox(); refreshMessages(); }} className="text-xs text-[var(--muted)] hover:text-[var(--accent)]">⟳ 刷新</button>
              </div>
              {/* 模拟来信（站内通道演示） */}
              <div className="card card-static p-3 space-y-2">
                <div className="text-xs font-medium text-[var(--muted)]">✍️ 模拟来信（写入收件箱，演示 C3 全流程）</div>
                <input value={inSubject} onChange={e => setInSubject(e.target.value)} placeholder="主题（可选）" className="input-glow w-full" />
                <textarea value={inBody} onChange={e => setInBody(e.target.value)} rows={2} placeholder="来信内容" className="input-glow w-full" />
                <button onClick={sendInboxMsg} className="btn-ghost text-xs">写入收件箱</button>
              </div>
              {/* 收件箱 */}
              <div className="space-y-2">
                <div className="text-xs font-medium text-[var(--muted)]">📥 收件箱（{messages.length}）</div>
                {messages.length === 0 && <p className="text-sm py-4 text-center text-[var(--muted)]">暂无来信</p>}
                {messages.map((m: any) => (
                  <div key={m.id} className="card card-static p-3 space-y-1.5">
                    <div className="flex items-center gap-2 text-sm">
                      <span>{m.direction === "in" ? "📥" : "📤"}</span>
                      <span className="font-medium">{m.subject || "(无主题)"}</span>
                      <span className="text-xs text-[var(--muted)] ml-auto">{m.from} → {m.to}</span>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${m.status === "unread" ? "bg-amber-500/15 text-amber-300" : "bg-white/5 text-[var(--muted)]"}`}>{m.status}</span>
                    </div>
                    <p className="text-xs text-[var(--muted)] whitespace-pre-wrap">{m.body}</p>
                    {m.direction === "in" && m.status !== "replied" && (
                      replyTo === m.id ? (
                        <div className="flex gap-2 pt-1">
                          <textarea value={replyBody} onChange={e => setReplyBody(e.target.value)} rows={1}
                            placeholder="代答内容（留空则自动简短回复）" className="input-glow flex-1" />
                          <button onClick={() => doReply(m.id)} className="btn-primary text-xs">发出</button>
                          <button onClick={() => { setReplyTo(null); setReplyBody(""); }} className="btn-ghost text-xs">取消</button>
                        </div>
                      ) : (
                        <button onClick={() => { setReplyTo(m.id); setReplyBody(""); }}
                          className="text-xs text-indigo-400 hover:text-indigo-300">✍️ 代答</button>
                      )
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {/* ---- 职业 ---- */}
      {tab === "职业" && (
        <div className="space-y-2 max-w-2xl">
          {(career || []).map((c, i) => (
            <div key={i} className="card card-static !py-2.5 flex items-center gap-2 text-sm">
              {c.is_current ? "📍" : "—"}
              <span>{c.title}</span>
              <span className="ml-auto text-xs" style={{ color: "var(--muted)" }}>{c.change_type}</span>
            </div>
          ))}
          {!career.length && (
            <p className="text-sm py-8 text-center" style={{ color: "var(--muted)" }}>暂无职业轨迹</p>
          )}
        </div>
      )}

      {/* ---- 下班学习（V5.7 ⑥⑦）---- */}
      {tab === "下班学习" && (
        <div className="space-y-4 max-w-3xl">
          <div className="card card-static p-4 space-y-2">
            <div className="text-sm font-medium">📓 今天学到了什么（员工自写 · 学 Letta）</div>
            <p className="text-xs text-[var(--muted)]">
              经验点每行一条。提交后走老板审批门——批准才沉淀为高置信记忆（importance 0.95），否决保留记录。
            </p>
            <textarea value={wlContent} onChange={e => setWlContent(e.target.value)}
              placeholder="例：处理发票任务时，先用 dry_run 预检坐标，点击失败率降了一半…"
              rows={3} className="input-glow w-full text-xs" />
            <textarea value={wlLessons} onChange={e => setWlLessons(e.target.value)}
              placeholder={"经验点（每行一条）\n预检先行，点击成功率更高"}
              rows={2} className="input-glow w-full text-xs" />
            <div className="flex items-center gap-3">
              <button onClick={postWorklog} className="btn-primary">提交日志</button>
              {wlMsg && <span className="text-xs" style={{ color: "var(--muted)" }}>{wlMsg}</span>}
            </div>
          </div>

          <div className="space-y-2">
            {worklogs.length === 0 && (
              <p className="text-xs py-4 text-center" style={{ color: "var(--muted)" }}>暂无日志</p>
            )}
            {worklogs.map((w: any) => (
              <div key={w.id} className="card card-static p-3 space-y-1">
                <div className="flex items-center gap-2 flex-wrap text-xs">
                  <span className="font-medium">{w.title || "工作日志"}</span>
                  <span className={w.status === "APPROVED" ? "text-emerald-400"
                    : w.status === "REJECTED" ? "text-red-400" : "text-amber-400"}>
                    {w.status === "APPROVED" ? "✓ 已沉淀" : w.status === "REJECTED" ? "✗ 已否决" : "⏳ 待审批"}
                  </span>
                  {w.memory_ref && <span style={{ color: "var(--muted)" }}>→ 记忆 {w.memory_ref.slice(0, 8)}…</span>}
                  <span className="ml-auto" style={{ color: "var(--muted)" }}>
                    {w.created_at ? new Date(w.created_at).toLocaleString("zh-CN", { hour12: false }) : ""}
                  </span>
                </div>
                <p className="text-xs whitespace-pre-wrap" style={{ color: "var(--foreground)" }}>{w.content}</p>
                {Array.isArray(w.lessons) && w.lessons.length > 0 && (
                  <ul className="text-xs pl-4 list-disc" style={{ color: "var(--muted)" }}>
                    {w.lessons.map((l: string, i: number) => <li key={i}>{l}</li>)}
                  </ul>
                )}
                {w.status === "PENDING" && (
                  <div className="flex gap-2 pt-1">
                    <button onClick={() => decideWorklog(w, "approve")}
                      className="text-xs px-2.5 py-1 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-white">✓ 批准并沉淀</button>
                    <button onClick={() => decideWorklog(w, "reject")}
                      className="text-xs px-2.5 py-1 rounded-lg bg-red-800/60 hover:bg-red-700 text-red-200">✕ 否决</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---- 工牌（V5.8 ⑨ Agent Card / needs-auth）---- */}
      {tab === "工牌" && (
        <div className="space-y-3 max-w-2xl">
          {!card ? (
            <p className="text-xs py-4 text-center" style={{ color: "var(--muted)" }}>加载中…</p>
          ) : (
            <>
              <div className="card card-static p-5">
                <div className="flex items-center gap-3 mb-3">
                  <div className="w-12 h-12 rounded-xl flex items-center justify-center text-xl"
                    style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>🪪</div>
                  <div>
                    <h3 className="font-semibold text-base">{card.name}</h3>
                    <p className="text-xs" style={{ color: "var(--muted)" }}>{card.role || "未设岗位"} · 状态 {card.status}</p>
                  </div>
                </div>
                <div className="text-xs space-y-1.5">
                  {card.description && <p style={{ color: "var(--muted)" }}>{card.description}</p>}
                  <div>
                    <span className="font-medium">技能清单：</span>{" "}
                    {Array.isArray(card.capabilities) && card.capabilities.length
                      ? card.capabilities.map((s: string) => (
                        <span key={s} className="inline-block px-1.5 py-0.5 mr-1 rounded bg-emerald-500/10 text-emerald-300">{s}</span>
                      ))
                      : <span style={{ color: "var(--muted)" }}>暂无</span>}
                  </div>
                  <div>
                    <span className="font-medium">电脑操作授权：</span>{" "}
                    {Array.isArray(card.computer_capabilities) && card.computer_capabilities.length
                      ? card.computer_capabilities.map((s: string) => (
                        <span key={s} className="inline-block px-1.5 py-0.5 mr-1 rounded bg-cyan-500/10 text-cyan-300">{s}</span>
                      ))
                      : <span style={{ color: "var(--muted)" }}>未授权</span>}
                  </div>
                </div>
              </div>

              <div className="card card-static p-4">
                <h4 className="text-sm font-medium mb-2">🔐 needs-auth（A2A 任务状态机）</h4>
                <p className="text-xs mb-2" style={{ color: "var(--muted)" }}>
                  调用方按工牌可预知哪些动作要先找老板批。挂起中的审批单：
                </p>
                {Array.isArray(card.needs_auth?.pending) && card.needs_auth.pending.length ? (
                  <div className="space-y-1">
                    {card.needs_auth.pending.map((p: any) => (
                      <div key={p.id} className="text-xs flex items-center gap-2">
                        <span className="text-amber-400">⏳</span>
                        <span className="font-medium">{p.action}</span>
                        <span style={{ color: "var(--muted)" }}>{p.since ? new Date(p.since).toLocaleString("zh-CN", { hour12: false }) : ""}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-emerald-400">✓ 无挂起审批（可自由调用）</p>
                )}
                <div className="text-xs mt-2" style={{ color: "var(--muted)" }}>
                  高危动作：{card.needs_auth?.high_risk_actions?.join(", ") || "—"}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
