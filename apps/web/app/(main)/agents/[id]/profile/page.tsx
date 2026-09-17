"use client";
import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useRef } from "react";
import { api } from "@/lib/api";

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
type SkillNode = { id: string; name: string };

const TABS = ["成长", "训练", "技能", "私人知识库", "记忆", "复盘", "考核", "职业"] as const;
const LEVEL_ZH: Record<string, string> = {
  BEGINNER: "入门", JUNIOR: "初级", INTERMEDIATE: "中级", ADVANCED: "高级", EXPERT: "专家",
};
const MEM_TYPE_ZH: Record<string, string> = {
  EPISODIC: "事件", SEMANTIC: "语义", PROCEDURAL: "程序", WORKING: "工作", REFLECTION: "反思",
};

export default function AgentProfilePage() {
  const { id: agentId } = useParams<{ id: string }>();
  const router = useRouter();
  const [agentName, setAgentName] = useState("");
  const [tab, setTab] = useState<(typeof TABS)[number]>("成长");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillNodes, setSkillNodes] = useState<SkillNode[]>([]);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [career, setCareer] = useState<CareerRec[]>([]);
  const [nodes, setNodes] = useState<any[]>([]);
  const [trTitle, setTrTitle] = useState("");
  const [trInput, setTrInput] = useState("");
  const [trUploading, setTrUploading] = useState(false);
  const trFileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [kbDocs, setKbDocs] = useState<any[]>([]);
  const [uploadNote, setUploadNote] = useState("");
  const [msg, setMsg] = useState("");
  // 记忆新增
  const [memContent, setMemContent] = useState("");
  const [memType, setMemType] = useState("EPISODIC");
  const [showAddMem, setShowAddMem] = useState(false);
  // 学习技能
  const [showLearn, setShowLearn] = useState(false);

  const cidRef = useRef("");

  const refreshAll = useCallback(async (aid: string) => {
    api.get(`/learning/agents/${aid}/profile`).then(setProfile).catch(() => {});
    api.get(`/skill-growth/agents/${aid}/matrix`).then(setSkills).catch(() => {});
    api.get(`/reflection/agents/${aid}/lessons`).then(setLessons).catch(() => {});
    api.get(`/reviews/agents/${aid}`).then(setReviews).catch(() => {});
    api.get(`/career/agents/${aid}/timeline`).then(setCareer).catch(() => {});
    // 记忆列表：等 company_id 就绪后再取
    const loadMemories = () =>
      api.get(`/growth-center/agents/${aid}/detail?company_id=${cidRef.current}`)
        .then((d: any) => setMemories(d?.memories || [])).catch(() => {});
    api.get("/companies").then((cs: any[]) => {
      if (cs.length && !cidRef.current) cidRef.current = cs[0].id;
      loadMemories();
    }).catch(loadMemories);
  }, []);

  useEffect(() => {
    if (!agentId) return;
    api.get("/agents").then((as: any[]) => {
      setAgentName(as.find((a: any) => a.id === agentId)?.name || "");
    }).catch(() => {});
    api.get("/kb2/agents/" + agentId + "/documents").then(setKbDocs).catch(() => {});
    api.get("/companies").then((cs: any[]) => {
      if (cs[0]?.id) {
        cidRef.current = cs[0].id;
        api.get(`/skills?company_id=${cs[0].id}`).then((all: any[]) => {
          // 公共技能在前，专属在后；排除已掌握
          setNodes(all);
        }).catch(() => {});
      }
    }).catch(() => {});
    refreshAll(agentId);
  }, [agentId, refreshAll]);

  async function uploadTrainingFile(f: File) {
    if (!agentId || !cidRef.current) return;
    setTrUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/training/upload-material?agent_id=${agentId}&company_id=${cidRef.current}`,
        { method: "POST",
          headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
          body: fd });
      const d = await r.json();
      if (d.ok) {
        setTrInput((prev: string) => (prev ? prev + "\n\n" : "") + `【来自附件 ${f.name} 的内容已入库私人知识库】`);
        setMsg(`✅ 附件 ${f.name} 已转 MD 存入私人知识库（${d.chunks} 块）`);
      } else setMsg("❌ 附件解析失败");
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
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
      refreshAll(agentId);
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
      refreshAll(agentId);
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
    refreshAll(agentId);
    setBusy(false);
  }

  async function removeSkill(asnId: string) {
    if (!confirm("确认移除这项技能？")) return;
    try {
      await api.del(`/skill-growth/agents/${agentId}/skills/${asnId}?company_id=${cidRef.current}`);
      setMsg("✅ 已移除技能");
      refreshAll(agentId);
    } catch (e: any) { setMsg("❌ 移除失败"); }
  }

  async function addMemory() {
    if (!memContent.trim() || !agentId) return;
    try {
      const r = await api.post("/growth-center/memories", {
        agent_id: agentId, company_id: cidRef.current,
        content: memContent.trim(), memory_type: memType,
      });
      if (r.ok) {
        setMemContent("");
        setShowAddMem(false);
        setMsg("✅ 已添加记忆");
        refreshAll(agentId);
      }
    } catch (e: any) { setMsg("❌ 添加失败"); }
  }

  async function removeMemory(memoryId: string) {
    if (!confirm("确认删除这条记忆？")) return;
    try {
      await api.del(`/growth-center/memories/${memoryId}?company_id=${cidRef.current}`);
      setMsg("✅ 已删除记忆");
      refreshAll(agentId);
    } catch (e: any) { setMsg("❌ 删除失败"); }
  }

  async function removeLesson(lessonId: string) {
    if (!confirm("确认删除这条复盘？")) return;
    try {
      await api.del(`/reflection/agents/${agentId}/lessons/${lessonId}`);
      setLessons(prev => prev.filter(l => l.id !== lessonId));
    } catch { /* ignore */ }
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
            <input ref={undefined as any} type="file" multiple className="hidden" />
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
    </div>
  );
}
