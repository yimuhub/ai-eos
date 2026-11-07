"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { formatLocalStamp } from "@/lib/remoteDevices";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";
import AgentChat from "@/components/AgentChat";
import InfoEditPanel from "@/components/InfoEditPanel";

/* ============================================================
   Agent 详情中心：暗色主题 + 切换模型 + 批量学习技能
   ============================================================ */

type Agent = { id: string; name: string; role?: string; status?: string };
type Profile = {
  profile: { identity: string; capabilities: string[]; preferences: Record<string, unknown>; version: number };
  stats: { tasks_total: number; tasks_completed: number; tasks_failed: number; meeting_spoke_count: number };
  capability_history: { version: number; capabilities: string[]; reason: string }[];
  growth_events: { type: string; title: string; created_at: string }[];
  learning_records: { id: string; goal: string; status: string; progress: number }[];
};
type Skill = { skill: string; level: string; experience_count: number; evidence_count: number };
type Lesson = { id: string; lesson: string };
type Review = { id: string; source: string; score: number };
type CareerRec = { title: string; change_type: string; is_current: boolean };
type ModelRow = { id: string; name: string; provider_type: string; model_id: string; base_url?: string };

// 对话为主界面；其余标签归入两个菜单
const INFO_TABS = ["总览"] as const;            // 员工信息
const PROFILE_TABS = ["成长", "训练", "技能", "复盘", "考核", "职业"] as const;  // 综合档案
type MenuKey = "none" | "info" | "profile";
const LEVEL_ZH: Record<string, string> = {
  BEGINNER: "入门", JUNIOR: "初级", INTERMEDIATE: "中级", ADVANCED: "高级", EXPERT: "专家",
};

export default function AgentDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const urlId = params?.id || "";
  const cid = useActiveCompanyId();
  const [agentId, setAgentId] = useState<string>(urlId);
  const [tab, setTab] = useState<string>("");
  const [menu, setMenu] = useState<MenuKey>("none");
  // 训练表单
  const [trTitle, setTrTitle] = useState("");
  const [trInput, setTrInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  // 切换模型
  const [newModelId, setNewModelId] = useState("");
  const [switching, setSwitching] = useState(false);

  /* ---------- 数据（统一交给 useResource：去重、缓存、切公司/回窗重取） ---------- */
  const { data: agentRows, error: agentsError } = useResource<Agent[]>(
    "agents:all",
    () => api.get<Agent[]>("/agents"),
  );
  const agents = useMemo<Agent[]>(() => agentRows ?? [], [agentRows]);

  const aid = agentId || null;
  const { data: profile, refresh: refreshProfile } = useResource<Profile>(
    aid ? `agent:profile:${aid}` : null,
    () => api.get<Profile>(`/learning/agents/${aid}/profile`),
  );
  const { data: skillRows, refresh: refreshSkills } = useResource<Skill[]>(
    aid ? `agent:skills:${aid}` : null,
    () => api.get<Skill[]>(`/skill-growth/agents/${aid}/matrix`),
  );
  const skills = useMemo<Skill[]>(() => skillRows ?? [], [skillRows]);
  const { data: lessonRows, refresh: refreshLessons } = useResource<Lesson[]>(
    aid ? `agent:lessons:${aid}` : null,
    () => api.get<Lesson[]>(`/reflection/agents/${aid}/lessons`),
  );
  const lessons = useMemo<Lesson[]>(() => lessonRows ?? [], [lessonRows]);
  const { data: reflectionRows, refresh: refreshReflections } = useResource<any[]>(
    aid ? `agent:reflections:${aid}` : null,
    () => api.get<any[]>(`/reflection/agents/${aid}/reflections`),
  );
  const reflections = useMemo<any[]>(() => reflectionRows ?? [], [reflectionRows]);
  const { data: reviewRows, refresh: refreshReviews } = useResource<Review[]>(
    aid ? `agent:reviews:${aid}` : null,
    () => api.get<Review[]>(`/reviews/agents/${aid}`),
  );
  const reviews = useMemo<Review[]>(() => reviewRows ?? [], [reviewRows]);
  const { data: careerRows, refresh: refreshCareer } = useResource<CareerRec[]>(
    aid ? `agent:career:${aid}` : null,
    () => api.get<CareerRec[]>(`/career/agents/${aid}/timeline`),
  );
  const career = useMemo<CareerRec[]>(() => careerRows ?? [], [careerRows]);
  const { data: nodeRows } = useResource<any[]>(
    cid ? `skill-growth:tree:${cid}` : null,
    () => api.get<any[]>(`/skill-growth/tree?company_id=${cid}`),
  );
  const nodes = useMemo<any[]>(() => nodeRows ?? [], [nodeRows]);
  const { data: modelRows } = useResource<ModelRow[]>(
    "models:options",
    () => api.get<ModelRow[]>("/models"),
  );
  const allModels = useMemo<ModelRow[]>(() => modelRows ?? [], [modelRows]);
  // 当前绑定模型：驱动「切换驱动模型」下拉的初始值
  const { data: bindingRows, refresh: refreshBindings } = useResource<any[]>(
    aid ? `agent:model-bindings:${aid}` : null,
    () => api.get<any[]>(`/agents/${aid}/models`),
  );
  useEffect(() => {
    if (bindingRows?.length) setNewModelId(bindingRows[0].model_id);
  }, [bindingRows]);

  // 以 URL 为准；URL 无效时才回退到第一个
  useEffect(() => {
    if (!agentRows) return;
    const target = agentRows.some((x) => x.id === urlId) ? urlId : (agentRows[0]?.id || "");
    if (target) setAgentId(target);
    else notify("warning", "还没有数字员工，请先创建或生成团队");
  }, [agentRows, urlId]);

  // 原实现 catch(() => {}) 会把"加载失败"渲染成永远的空页面
  useEffect(() => {
    if (agentsError) notify("error", agentsError);
  }, [agentsError]);

  // 写操作后刷新该员工的档案类数据（原 refreshAll 的职责）
  const refreshAll = useCallback(() => {
    refreshProfile();
    refreshSkills();
    refreshLessons();
    refreshReflections();
    refreshReviews();
    refreshCareer();
    refreshBindings();
  }, [refreshProfile, refreshSkills, refreshLessons, refreshReflections,
      refreshReviews, refreshCareer, refreshBindings]);

  async function createTraining() {
    if (!trTitle || !agentId) return;
    setBusy(true);
    try {
      const s = await api.post(
        `/training/sessions?agent_id=${agentId}`,
        { training_type: "CONVERSATION", title: trTitle, raw_input: trInput },
      );
      setMsg(`训练完成，学习点 ${s.points} 个已提取，待确认`);
      refreshAll();
      setTrTitle("");
      setTrInput("");
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
    setBusy(false);
  }

  async function switchModel() {
    if (!agentId || !newModelId) return;
    setSwitching(true);
    try {
      // 后端实际注册的是 PUT /agents/{id}/switch-model，原代码用 PATCH
      // → 405 Method Not Allowed，切换模型永远失败（且错误提示只有一句 HTTP 405）。
      await api.put(`/agents/${agentId}/switch-model`, { model_id: newModelId });
      setMsg(`✅ 已切换到模型: ${allModels.find((m) => m.id === newModelId)?.name || newModelId}`);
      refreshAll();
    } catch (e: any) {
      setMsg(`❌ ${e.message || String(e)}`);
    }
    setSwitching(false);
  }

  async function acquireOne(nodeId: string, nodeName: string) {
    try {
      await api.post(`/skill-growth/agents/${agentId}/acquire`, {
        skill_node_id: nodeId,
        source_type: "assignment",
        evidence: [{ type: "assignment" }],
      });
      setMsg(`✅ ${nodeName} 已添加`);
      refreshAll();
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
  }

  async function acquireAll() {
    const available = nodes.filter((n: any) => !skills.some((s: Skill) => s.skill === n.name));
    if (!available.length) { setMsg("所有技能已学完"); return; }
    // 学习技能是"增加能力"而非破坏，因此不做危险样式，也不要求输入确认词
    if (!(await confirmAction({
      title: `确定要学习全部 ${available.length} 个技能？`,
      description: "学习后会占用额外的模型调用额度，但不会删除任何现有能力。",
      danger: false,
      confirmText: "全部学习",
    }))) return;
    try {
      const result = await api.post(`/skill-growth/agents/${agentId}/acquire-batch`, {
        skill_node_ids: available.map((n: any) => n.id),
        source_type: "assignment",
      });
      setMsg(`✅ 已学习 ${result.added.length} 个技能${result.skipped.length ? `，${result.skipped.length} 个已存在或前置不满足` : ""}`);
      refreshAll();
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
  }

  const agentName = agents.find((a) => a.id === agentId)?.name || "";
  const available = nodes.filter((n: any) => !skills.some((s: Skill) => s.skill === n.name));

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {!agentId ? (
        <p className="text-[var(--muted)]">请选择一个 Agent</p>
      ) : (
        <>
          {/* 顶部栏：左标题 + 右上角两个菜单按钮 */}
          <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
            <h1 className="text-xl font-bold tracking-tight text-[var(--foreground)]">{agentName} · 工作对话</h1>
            <div className="flex gap-2">
              <button onClick={() => { setMenu(menu === "info" ? "none" : "info"); }}
                className={`px-3.5 py-1.5 rounded-full text-xs font-medium transition-all border ${
                  menu === "info" ? "border-indigo-500 bg-indigo-500/15 text-indigo-200"
                                  : "hover:bg-white/[0.05]"}`}
                style={menu === "info" ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                👤 员工信息
              </button>
              <button onClick={() => router.push(`/agents/${agentId}/profile`)}
                className={`px-3.5 py-1.5 rounded-full text-xs font-medium transition-all border ${
                  menu === "profile" ? "border-cyan-500 bg-cyan-500/15 text-cyan-200"
                                     : "hover:bg-white/[0.05]"}`}
                style={menu === "profile" ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                📚 综合档案 →
              </button>
            </div>
          </div>

          {/* 员工信息编辑弹窗（复用原列表页弹窗模式） */}
          {menu === "info" && agentId && (
            <InfoEditPanel agentId={agentId} onClose={() => setMenu("none")} />
          )}

          {/* 对话主界面 */}
          <AgentChat agentId={agentId} />

          {menu === "info" && tab === "总览" && profile && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
                {[
                  ["任务总数", profile.stats.tasks_total],
                  ["已完成", profile.stats.tasks_completed],
                  ["失败", profile.stats.tasks_failed],
                  ["会议发言", profile.stats.meeting_spoke_count],
                  ["档案版本", `v${profile.profile.version}`],
                ].map(([k, v]) => (
                  <div key={k as string} className="card card-static p-3">
                    <div className="text-xs text-[var(--muted)]">{k}</div>
                    <div className="text-lg font-bold text-[var(--foreground)]">{v}</div>
                  </div>
                ))}
              </div>
              <div className="card card-static text-sm mb-3 text-[var(--foreground)]">
                <b className="text-[var(--foreground)]">身份：</b>{profile.profile.identity}<br />
                <b className="text-[var(--foreground)]">能力：</b>{profile.profile.capabilities.join("、") || "未设置"}
              </div>
              <h4 className="text-sm font-medium text-[var(--foreground)] mb-1">最近动态</h4>
              <div className="space-y-1 text-sm max-h-60 overflow-auto">
                {profile.growth_events.slice(0, 8).map((e, i) => (
                  <div key={i} className="border-l-2 border-blue-600 pl-3 py-1 card text-[var(--foreground)]">
                    <span className="inline-block px-1.5 py-0.5 text-xs bg-blue-900/40 text-blue-300 rounded mr-2">
                      {e.type}
                    </span>
                    {e.title}
                  </div>
                ))}
              </div>
            </>
          )}

          {/* ---- 成长 ---- */}
          {menu === "profile" && tab === "成长" && profile && (
            <div className="space-y-3">
              <div className="card card-static p-3 text-sm text-[var(--foreground)]">
                <b className="text-[var(--foreground)]">能力清单：</b>{profile.profile.capabilities.join("、") || "未设置"}
              </div>
              <h4 className="text-sm font-medium text-[var(--foreground)]">能力版本历史（不可覆盖）</h4>
              {profile.capability_history.map((c) => (
                <div key={c.version} className="card card-static !py-2 text-sm text-[var(--foreground)]">
                  <b className="text-[var(--foreground)]">v{c.version}</b> — {(c.capabilities || []).join("、")}
                  <br />
                  <span className="text-xs text-[var(--muted)]">{c.reason}</span>
                </div>
              ))}
              <h4 className="text-sm font-medium text-[var(--foreground)] mt-3">学习计划</h4>
              {profile.learning_records.map((l) => (
                <div key={l.id} className="flex items-center gap-2 text-sm text-[var(--foreground)]">
                  <span className="flex-1">{l.goal}</span>
                  <div className="w-24 h-2 bg-white/[0.04] rounded-full overflow-hidden">
                    <div className="h-2 bg-blue-500 rounded-full" style={{ width: `${l.progress}%` }} />
                  </div>
                  <span className="text-xs text-[var(--muted)] w-16 text-right">{l.status}</span>
                </div>
              ))}
            </div>
          )}

          {/* ---- 训练 ---- */}
          {menu === "profile" && tab === "训练" && (
            <div className="space-y-3">
              <div className="card card-static p-3 space-y-2">
                <input
                  value={trTitle}
                  onChange={(e) => setTrTitle(e.target.value)}
                  placeholder="训练标题"
                  className="input"
                />
                <textarea
                  value={trInput}
                  onChange={(e) => setTrInput(e.target.value)}
                  placeholder="要教给 TA 的内容、规范、案例……"
                  className="input h-24"
                />
                <button
                  onClick={createTraining}
                  disabled={busy || !trTitle}
                  className="btn-primary disabled:opacity-40"
                >
                  {busy ? "AI 提取中…" : "发起训练（提取后需确认）"}
                </button>
              </div>
              {msg && <p className="text-sm text-blue-300">{msg}</p>}
            </div>
          )}

          {/* ---- 技能 ---- */}
          {menu === "profile" && tab === "技能" && (
            <div className="space-y-3">
              <div className="flex justify-between items-center">
                <h4 className="text-sm font-medium text-[var(--foreground)]">已掌握技能</h4>
                {available.length > 0 && (
                  <button onClick={acquireAll}
                    className="px-3 py-1 bg-green-700 hover:bg-green-600 text-white rounded text-xs">
                    学全部技能 ({available.length})
                  </button>
                )}
              </div>
              {skills.map((s, i) => (
                <div key={i} className="card card-static !py-2 flex justify-between items-center text-sm text-[var(--foreground)]">
                  <span>
                    <b className="text-[var(--foreground)]">{s.skill}</b> — {LEVEL_ZH[s.level] || s.level}
                  </span>
                  <span className="text-xs text-[var(--muted)]">
                    证据{s.evidence_count} · 经验{s.experience_count}
                  </span>
                </div>
              ))}
              {!skills.length && <p className="text-[var(--muted)] text-sm">暂无掌握技能</p>}

              {available.length > 0 && (
                <>
                  <h4 className="text-sm font-medium text-[var(--foreground)] mt-3 mb-1">可学习</h4>
                  <div className="flex gap-2 flex-wrap">
                    {available.map((n: any) => (
                      <button
                        key={n.id}
                        onClick={() => acquireOne(n.id, n.name)}
                        className="px-3 py-1 border border-[var(--border)] rounded text-xs text-[var(--foreground)] hover:bg-white/[0.04] hover:text-[var(--foreground)]"
                      >
                        + {n.name}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {/* ---- 复盘 ---- */}
          {menu === "profile" && tab === "复盘" && (
            <div className="space-y-3">
              <h4 className="text-sm font-medium text-[var(--foreground)]">经验教训库</h4>
              {lessons.map((l) => (
                <div key={l.id} className="card card-static !py-2 text-sm text-[var(--foreground)]">
                  💡 {l.lesson}
                </div>
              ))}
              {!lessons.length && <p className="text-[var(--muted)] text-sm">暂无教训记录</p>}
              <h4 className="text-sm font-medium text-[var(--foreground)] mt-3">复盘历史</h4>
              {reflections.map((r) => (
                <div key={r.id} className="card card-static !py-2 text-sm text-[var(--foreground)]">
                  <span className="text-xs text-[var(--muted)]">
                    {formatLocalStamp(r.created_at)}
                  </span>
                  {r.went_well?.map((w: string, i: number) => (
                    <div key={"w" + i}>✅ {w}</div>
                  ))}
                  {r.what_failed?.map((f: string, i: number) => (
                    <div key={"f" + i}>⚠️ {f}</div>
                  ))}
                </div>
              ))}
            </div>
          )}

          {/* ---- 考核 ---- */}
          {menu === "profile" && tab === "考核" && (
            <div className="space-y-1 text-sm text-[var(--foreground)]">
              {reviews.map((r) => (
                <div key={r.id} className="card card-static !py-2 flex justify-between">
                  <span>{r.source} 考核</span>
                  <b className="text-[var(--foreground)]">{r.score} 分</b>
                </div>
              ))}
              {!reviews.length && <p className="text-[var(--muted)] text-sm">暂无考核记录</p>}
            </div>
          )}

          {/* ---- 职业 ---- */}
          {menu === "profile" && tab === "职业" && (
            <div className="space-y-1 text-sm text-[var(--foreground)]">
              {career.map((c, i) => (
                <div key={i} className="border-l-2 border-purple-500 pl-3 py-1">
                  {c.change_type}: <b className="text-[var(--foreground)]">{c.title}</b>{" "}
                  {c.is_current && <span className="text-green-400 text-xs">(现任)</span>}
                </div>
              ))}
              {!career.length && <p className="text-[var(--muted)] text-sm">暂无任职记录</p>}
            </div>
          )}

          {/* ---- 切换模型（固定在底部）---- */}
          <div className="mt-6 card card-static p-3">
            <h4 className="text-sm font-medium text-[var(--foreground)] mb-2">切换驱动模型</h4>
            <div className="flex gap-2 items-center">
              <select value={newModelId} onChange={(e) => setNewModelId(e.target.value)}
                className="flex-1 bg-white/[0.04] border border-[var(--border)] rounded px-3 py-1.5 text-sm text-[var(--foreground)]">
                <option value="">选择模型</option>
                {allModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} ({m.provider_type} · {m.model_id})
                  </option>
                ))}
              </select>
              <button onClick={switchModel} disabled={switching || !newModelId}
                className="btn-primary disabled:opacity-40">
                {switching ? "切换中…" : "确认切换"}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
