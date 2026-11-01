"use client";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import { confirmAction } from "@/components/ConfirmDialog";
import { useActiveCompanyId, useResource } from "@/lib/useResource";

/* ============================================================
   成长中心：按部门聚合成长档案 + 员工档案弹窗 + 学习计划
   UI 与 Agent 团队页一致（深空底/玻璃卡/渐变头像/呼吸点）
   ============================================================ */

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#8b5cf6,#ec4899"];
const LEVEL_ZH: Record<string, string> = {
  NOVICE: "新手", LEARNING: "学习中", PRACTICED: "熟练",
  PROFICIENT: "精通", EXPERT: "专家",
};

type AgentSummary = {
  id: string; name: string; role?: string | null; status: string;
  skill_count: number; memory_count: number; active_plans: number;
};
type DeptGroup = { dept_id: string; dept_name: string; agents: AgentSummary[] };
type Profile = any;
type Plan = {
  id: string; title: string; agent_id: string; agent_name: string;
  target_skills: string[]; knowledge_scope?: string | null;
  period_days: number; status: string; progress: number;
  notes?: string | null; created_at?: string;
};

export default function GrowthCenterPage() {
  // 激活公司可订阅：切公司后档案与计划自动跟随，不再用 `/companies → cs[0]` 快照。
  const cid = useActiveCompanyId() ?? "";
  const [mounted, setMounted] = useState(false);
  // 部门档案与学习计划互不依赖：两个 key 独立缓存/重取，
  // 计划慢或失败不会拖住（或清空）部门档案，与原实现的两段独立请求一致。
  const {
    data: deptBundle, error: loadError, refresh: refreshDepts,
  } = useResource(
    cid ? `growth-center:${cid}` : null,
    async () => {
      const d = await api.get<{ departments?: DeptGroup[] }>(
        `/growth-center/departments-overview?company_id=${cid}`);
      return { cid, depts: d.departments || [] };
    },
  );
  const { data: planBundle, refresh: refreshPlans } = useResource(
    cid ? `growth-center:plans:${cid}` : null,
    async () => {
      const plans = await api.get<Plan[]>(`/growth-center/plans?company_id=${cid}`);
      return { cid, plans: plans || [] };
    },
  );
  // 切公司时旧 bundle 先不渲染，避免档案/计划短暂显示上一家公司的数据。
  const depts = deptBundle && deptBundle.cid === cid ? deptBundle.depts : [];
  const plans = planBundle && planBundle.cid === cid ? planBundle.plans : [];
  /** 写操作后档案与计划一起重取（沿用原 load 的语义） */
  const load = useCallback(() => {
    refreshDepts();
    refreshPlans();
  }, [refreshDepts, refreshPlans]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [pinnedDepts, setPinnedDepts] = useState<string[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      return JSON.parse(localStorage.getItem("aieos_growth_pinned_depts") || "[]");
    } catch { return []; }
  });

  // 置顶持久化：变化时写回 localStorage
  useEffect(() => {
    try {
      localStorage.setItem("aieos_growth_pinned_depts", JSON.stringify(pinnedDepts));
    } catch {}
  }, [pinnedDepts]);

  const [detail, setDetail] = useState<{ agent: AgentSummary; profile: Profile } | null>(null);
  const [detailData, setDetailData] = useState<any>(null);
  const [openSection, setOpenSection] = useState<string | null>(null);
  // 学习计划表单
  const [planPanel, setPlanPanel] = useState(false);
  const [planTitle, setPlanTitle] = useState("");
  const [planSkills, setPlanSkills] = useState("");
  const [planScope, setPlanScope] = useState("");
  const [planAttach, setPlanAttach] = useState<Attachment[]>([]);
  const [planDays, setPlanDays] = useState(7);
  const [planAgents, setPlanAgents] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => { setMounted(true); }, []);

  // 加载失败时沿用原来的顶部提示条（错误文案由数据层归一化）。
  useEffect(() => { if (loadError) setMsg(loadError); }, [loadError]);

  async function openDetail(a: AgentSummary) {
    const profile = await api.get(`/learning/agents/${a.id}/profile`).catch(() => null);
    setDetail({ agent: a, profile });
    setDetailData(null); setOpenSection(null);
    try {
      // 原来这里 catch 是空的：详情拉不到时弹窗里就是一片空白，
      // 用户无法区分"这个人没有记录"和"接口挂了"。
      setDetailData(await api.get(`/growth-center/agents/${a.id}/detail?company_id=${cid}`));
    } catch (e) {
      setMsg(`❌ 读取员工档案失败：${describeError(e, "读取失败")}`);
    }
  }

  async function delItem(kind: "skills" | "memories" | "plans", id: string) {
    if (!cid) return;
    if (!(await confirmAction({
      title: "确认删除该项？",
      description: "删除后该员工将不再拥有这条记录。",
      confirmText: "删除",
    }))) return;
    try {
      // 三类资源各自对应后端不同的路由（/skills/{asn_id}、/memories/{memory_id}、
      // /plans/{plan_id}）。写成 `/growth-center/${kind}/${id}` 虽然运行时能跑，
      // 但契约扫描看不出 kind 的取值范围，等于把这段路径排除在校验之外。
      // 拆成三条字面路径，每一条都能被自动校验到。
      if (kind === "skills") {
        await api.del(`/growth-center/skills/${id}?company_id=${cid}`);
      } else if (kind === "memories") {
        await api.del(`/growth-center/memories/${id}?company_id=${cid}`);
      } else {
        await api.del(`/growth-center/plans/${id}?company_id=${cid}`);
      }
      // 刷新详情数据 + 计数
      if (detail) await openDetail(detail.agent);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "删除失败")}`); }
  }

  async function createPlan() {
    if (!planTitle.trim() || !planAgents.size || !cid) return;
    setBusy(true);
    try {
      await api.post(`/growth-center/plans?company_id=${cid}`, {
        title: planTitle,
        agent_ids: Array.from(planAgents),
        target_skills: planSkills.split(/[,，、]/).map((x) => x.trim()).filter(Boolean),
        knowledge_scope: planScope || null,
        period_days: planDays,
      });
      setMsg("✅ 学习计划已下达");
      setPlanPanel(false); setPlanTitle(""); setPlanSkills(""); setPlanScope("");
      setPlanAgents(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "下达学习计划失败")}`); }
    setBusy(false);
  }

  async function updatePlan(p: Plan, status: string) {
    try {
      // 原来不看返回：改状态失败也一声不响，用户以为已经生效
      await api.patch(`/growth-center/plans/${p.id}?company_id=${cid}&status=${status}`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "更新计划失败")}`); }
  }

  const sortedDepts = [...depts].sort((a, b) => {
    const ai = pinnedDepts.indexOf(a.dept_name), bi = pinnedDepts.indexOf(b.dept_name);
    if (ai !== -1 && bi === -1) return -1;
    if (bi !== -1 && ai === -1) return 1;
    return a.dept_name.localeCompare(b.dept_name, "zh");
  });

  const detailCaps: any[] = detail?.profile?.identity?.capabilities ||
    detail?.profile?.capabilities || [];
  const detailGoals: string[] = detail?.profile?.identity?.learning_goals ||
    detail?.profile?.learning_goals || [];
  const growthEvents: any[] = detail?.profile?.growth_events || [];

  if (!mounted) return null;


  const detailModal = mounted && detail ? createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
      onClick={() => setDetail(null)}
    >
      <div
        className="w-full max-w-3xl rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto animate-fade-up"
        style={{
          background: "linear-gradient(180deg,#15151f,#101018)",
          border: "1px solid rgba(99,102,241,0.35)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border)" }}>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-2xl flex items-center justify-center text-white text-lg font-bold"
              style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
              {detail!.agent.name.slice(0, 1)}
            </div>
            <div>
              <h3 className="font-semibold text-base">{detail!.agent.name}</h3>
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                {detail!.agent.role || "未设岗位"} · 成长档案
              </p>
            </div>
          </div>
          <button onClick={() => setDetail(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
        </div>

        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-cyan-300">{detail!.agent.skill_count}</p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>掌握技能</p>
          </div>
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-purple-300">{detail!.agent.memory_count}</p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>长期记忆</p>
          </div>
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-amber-300">{detail!.agent.active_plans}</p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>进行中计划</p>
          </div>
        </div>

        <div>
          <p className="text-xs font-semibold mb-2">💎 能力状态</p>
          {(detail!.profile?.identity?.capabilities || detail!.profile?.capabilities || []).length ? (
            <div className="space-y-2">
              {(detail!.profile?.identity?.capabilities || detail!.profile?.capabilities || []).map((cap: any, i: number) => {
                const capName = typeof cap === "string" ? cap : cap.name || "能力";
                const level = typeof cap === "object" ? cap.level || "" : "";
                const ev = typeof cap === "object" ? cap.evidence_count ?? cap.evidence : null;
                return (
                  <div key={i} className="rounded-xl p-3 space-y-1.5" style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold">{capName}</span>
                      {level && (
                        <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300">
                          {LEVEL_ZH[level] || level}
                        </span>
                      )}
                    </div>
                    {typeof ev === "number" && (
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-1 rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.06)" }}>
                          <div className="h-full rounded-full"
                            style={{ width: `${Math.min(100, ev * 20)}%`, background: "linear-gradient(90deg,#6366f1,#22d3ee)" }} />
                        </div>
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>证据 ×{ev}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-xs py-3" style={{ color: "var(--muted)" }}>暂无能力评估记录</p>
          )}
        </div>

        <div>
          <p className="text-xs font-semibold mb-2">🎯 学习目标</p>
          {(detail!.profile?.identity?.learning_goals || detail!.profile?.learning_goals || []).length ? (
            <div className="flex flex-wrap gap-1.5">
              {(detail!.profile?.identity?.learning_goals || detail!.profile?.learning_goals || []).map((g2: string, i: number) => (
                <span key={i} className="text-[11px] px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-300">{g2}</span>
              ))}
            </div>
          ) : (
            <p className="text-xs" style={{ color: "var(--muted)" }}>暂无 · 可通过「下达学习计划」设定</p>
          )}
        </div>

        {/* 可展开管理区块：技能 / 记忆 / 计划 */}
        {[
          { key: "skills", icon: "💎", title: `掌握技能（${detailData?.skills?.length ?? detail!.agent.skill_count}）`,
            color: "#22d3ee" },
          { key: "memories", icon: "🧠", title: `长期记忆（${detailData?.memories?.length ?? detail!.agent.memory_count}）`,
            color: "#a78bfa" },
          { key: "plans", icon: "📋", title: `进行中计划（${detailData?.plans?.length ?? detail!.agent.active_plans}）`,
            color: "#fbbf24" },
        ].map((sec) => {
          const open = openSection === sec.key;
          return (
            <div key={sec.key} className="rounded-xl overflow-hidden"
              style={{ background: "var(--surface-2)", border: open ? `1px solid ${sec.color}55` : "1px solid var(--border)" }}>
              <button onClick={() => setOpenSection(open ? null : sec.key)}
                className="w-full flex items-center justify-between px-3 py-2.5 hover:bg-white/[0.03] transition-colors">
                <span className="text-xs font-semibold flex items-center gap-2">
                  <span style={{ color: sec.color }}>{sec.icon}</span>{sec.title}
                </span>
                <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                  {open ? "▲ 收起" : "▼ 展开 · 查看/管理"}
                </span>
              </button>
              {open && (
                <div className="px-3 pb-3 space-y-1.5 max-h-52 overflow-y-auto animate-fade-up">
                  {sec.key === "skills" && (detailData?.skills || []).map((sk: any) => (
                    <div key={sk.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs"
                      style={{ background: "rgba(255,255,255,0.03)" }}>
                      <span className="flex-1 truncate">{sk.name}</span>
                      <span className={`text-[9px] px-1.5 py-0.5 rounded-full shrink-0 ${sk.scope === "public"
                        ? "bg-cyan-500/15 text-cyan-300" : "bg-pink-500/15 text-pink-300"}`}>
                        {sk.scope === "public" ? "公共" : "专属"}
                      </span>
                      <button onClick={() => delItem("skills", sk.id)}
                        className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-md text-red-300 hover:bg-red-500/15">🗑</button>
                    </div>
                  ))}
                  {sec.key === "memories" && (detailData?.memories || []).map((m: any) => (
                    <div key={m.id} className="px-2.5 py-1.5 rounded-lg text-xs space-y-0.5"
                      style={{ background: "rgba(255,255,255,0.03)" }}>
                      <div className="flex items-start gap-2">
                        <span className="flex-1" style={{ color: "var(--foreground)" }}>{m.content}</span>
                        <button onClick={() => delItem("memories", m.id)}
                          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-md text-red-300 hover:bg-red-500/15">🗑</button>
                      </div>
                      <p className="text-[9px]" style={{ color: "var(--muted)" }}>
                        [{m.type}] {m.created_at}
                      </p>
                    </div>
                  ))}
                  {sec.key === "plans" && (detailData?.plans || []).map((pl: any) => (
                    <div key={pl.id} className="px-2.5 py-1.5 rounded-lg text-xs space-y-0.5"
                      style={{ background: "rgba(255,255,255,0.03)" }}>
                      <div className="flex items-center gap-2">
                        <span className="flex-1 truncate">{pl.title} · {Math.round(pl.progress)}%</span>
                        <button onClick={() => delItem("plans", pl.id)}
                          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-md text-red-300 hover:bg-red-500/15">🗑</button>
                      </div>
                      {pl.target_skills?.length ? (
                        <p className="text-[9px]" style={{ color: "var(--muted)" }}>
                          目标：{pl.target_skills.join("、")} · 周期{pl.period_days}天
                        </p>) : null}
                    </div>
                  ))}
                  {((sec.key === "skills" && !(detailData?.skills || []).length) ||
                    (sec.key === "memories" && !(detailData?.memories || []).length) ||
                    (sec.key === "plans" && !(detailData?.plans || []).length)) && (
                    <p className="text-[11px] text-center py-2" style={{ color: "var(--muted)" }}>暂无内容</p>
                  )}
                </div>
              )}
            </div>
          );
        })}

        <div>
          <p className="text-xs font-semibold mb-2">📈 成长轨迹</p>
          {(detail!.profile?.growth_events || []).length ? (
            <div className="space-y-1.5 max-h-40 overflow-y-auto">
              {(detail!.profile?.growth_events || []).slice(0, 8).map((ev: any, i: number) => (
                <div key={i} className="flex gap-2 items-start text-xs">
                  <span className="text-[10px] shrink-0 mt-0.5" style={{ color: "var(--muted)" }}>
                    {String(ev.at || ev.created_at || "").slice(5, 10)}
                  </span>
                  <span>{ev.title || ev.description || ev.event || ""}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs" style={{ color: "var(--muted)" }}>暂无成长事件</p>
          )}
        </div>

        <div className="flex gap-2 pt-2 border-t" style={{ borderColor: "var(--border)" }}>
          <button
            onClick={() => {
              setPlanAgents(new Set([detail!.agent.id]));
              setDetail(null);
              setPlanPanel(true);
            }}
            className="btn-primary text-xs flex-1"
          >
            🎯 为 TA 下达学习计划
          </button>
        </div>
      </div>
    </div>,
    document.body
  ) : null;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{ background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(16,185,129,0.06) 60%, transparent)", border: "1px solid rgba(99,102,241,0.22)" }}>
        <div className="relative flex items-end justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">🌱 成长中心</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              按部门查看员工成长档案 · 能力状态可视化 · 制定学习计划
            </p>
          </div>
          <button onClick={() => setPlanPanel(!planPanel)}
            className={`px-4 py-2 rounded-xl text-xs font-medium transition-all ${planPanel ? "bg-emerald-600 text-white" : ""}`}
            style={planPanel ? {} : { background: "linear-gradient(135deg,#10b981,#059669)", color: "#fff" }}>
            🎯 下达学习计划
          </button>
        </div>
      </div>

      {msg && <p className="mb-3 text-xs text-indigo-300">{msg}</p>}

      {/* 学习计划面板 */}
      {planPanel && (
        <div className="card p-4 space-y-3 mb-6 animate-fade-up"
          style={{ borderColor: "rgba(16,185,129,0.35)" }}>
          <h3 className="font-semibold text-sm">🎯 新学习计划</h3>
          <input value={planTitle} onChange={(e) => setPlanTitle(e.target.value)}
            placeholder="计划标题（如：掌握小红书爆款写作）" className="input w-full !py-2 text-xs" />
          <input value={planSkills} onChange={(e) => setPlanSkills(e.target.value)}
            placeholder="目标技能（逗号分隔：文案写作, 数据分析, 用户洞察…）" className="input w-full !py-2 text-xs" />
          <textarea value={planScope} onChange={(e) => setPlanScope(e.target.value)} rows={2}
            placeholder="知识/知识库范围说明（如：需掌握竞品分析方法论，学习公司产品知识库）"
            className="input w-full !py-2 text-xs resize-none" />
          <AttachmentPicker value={planAttach} onChange={setPlanAttach} label="添加学习资料" />
          <div className="flex items-center gap-2">
            <span className="text-xs shrink-0" style={{ color: "var(--muted)" }}>学习周期</span>
            <input type="number" min={1} max={365} value={planDays}
              onChange={(e) => setPlanDays(Number(e.target.value))}
              className="input w-20 !py-1.5 text-xs text-center" />
            <span className="text-xs" style={{ color: "var(--muted)" }}>天</span>
          </div>
          <div>
            <p className="text-[11px] mb-1.5" style={{ color: "var(--muted)" }}>
              分配给员工（已选 {planAgents.size} 人）
            </p>
            <div className="flex flex-wrap gap-1.5 max-h-32 overflow-y-auto">
              {depts.flatMap((g) => g.agents).map((a) => {
                const on = planAgents.has(a.id);
                return (
                  <button key={a.id}
                    onClick={() => setPlanAgents((prev) => {
                      const n = new Set(prev);
                      if (on) n.delete(a.id); else n.add(a.id);
                      return n;
                    })}
                    className={`px-2.5 py-1 rounded-lg text-[11px] border transition-all ${on ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "hover:bg-white/[0.04]"}`}
                    style={on ? {} : { borderColor: "rgba(99,102,241,0.25)", color: "var(--muted)" }}>
                    {on ? "✓ " : ""}{a.name}
                  </button>
                );
              })}
            </div>
          </div>
          <button onClick={createPlan} disabled={busy || !planTitle.trim() || !planAgents.size}
            className="btn-primary text-xs disabled:opacity-40">
            {busy ? "创建中…" : `下达计划（${planAgents.size} 名员工）`}
          </button>
        </div>
      )}

      {/* 进行中的学习计划列表 */}
      {plans.length > 0 && (
        <div className="mb-8 space-y-2">
          <h2 className="text-sm font-semibold mb-2">📋 学习计划</h2>
          {plans.filter((p) => p.status === "active").map((p) => (
            <div key={p.id} className="card !py-3 px-4 flex items-center gap-4 flex-wrap">
              <div className="min-w-[200px] flex-1">
                <p className="text-sm font-medium">{p.title}</p>
                <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                  👤 {p.agent_name} · 周期 {p.period_days} 天
                  {p.target_skills?.length ? ` · 目标：${p.target_skills.join("、")}` : ""}
                </p>
              </div>
              <div className="flex items-center gap-2 min-w-[180px]">
                <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
                  <div className="h-full rounded-full transition-all"
                    style={{ width: `${p.progress}%`, background: "linear-gradient(90deg,#10b981,#34d399)" }} />
                </div>
                <span className="text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>{Math.round(p.progress)}%</span>
              </div>
              {p.status === "active" ? (
                <>
                  <button onClick={() => updatePlan(p, "completed")}
                    className="text-[11px] px-2 py-1 rounded-md text-emerald-300 hover:bg-emerald-500/15">✓ 完成</button>
                  <button onClick={() => updatePlan(p, "cancelled")}
                    className="text-[11px] px-2 py-1 rounded-md text-red-300 hover:bg-red-500/15">✕ 取消</button>
                </>
              ) : (
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${p.status === "completed"
                  ? "bg-emerald-500/15 text-emerald-300" : "bg-white/[0.06] text-[var(--muted)]"}`}>
                  {p.status === "completed" ? "已完成" : "已取消"}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 部门分组成长档案 */}
      {sortedDepts.map((g) => {
        const gradIdx = sortedDepts.findIndex((x) => x.dept_name === g.dept_name) % AVATAR_COLORS.length;
        const grad = AVATAR_COLORS[gradIdx];
        const isCollapsed = collapsed.has(g.dept_id);
        return (
          <div key={g.dept_id} className="mb-7">
            <div className="flex items-center gap-3 mb-3 group/dept">
              <button onClick={() => setCollapsed((prev) => {
                const n = new Set(prev); if (n.has(g.dept_id)) n.delete(g.dept_id); else n.add(g.dept_id); return n;
              })} className="flex items-center gap-2.5">
                <span className={`text-indigo-300 transition-transform ${isCollapsed ? "-rotate-90" : ""}`}>▾</span>
                <span className="w-8 h-8 rounded-lg flex items-center justify-center text-sm"
                  style={{ background: `linear-gradient(135deg, ${grad})` }}>🏢</span>
                <span className="text-lg font-bold tracking-wide">{g.dept_name}</span>
                <span className="text-xs opacity-60" style={{ color: "var(--muted)" }}>
                  · {g.agents.length} 名员工
                </span>
              </button>
              <button onClick={() => setPinnedDepts((prev) =>
                prev.includes(g.dept_name) ? prev.filter((x) => x !== g.dept_name) : [g.dept_name, ...prev])}
                className={`text-[11px] px-2 py-0.5 rounded-md transition-colors ${pinnedDepts.includes(g.dept_name)
                  ? "text-amber-300 bg-amber-500/15" : "opacity-0 group-hover/dept:opacity-100 hover:bg-white/[0.06]"}`}
                style={pinnedDepts.includes(g.dept_name) ? {} : { color: "var(--muted)" }}>
                📌 {pinnedDepts.includes(g.dept_name) ? "已置顶" : "置顶"}
              </button>
            </div>

            {!isCollapsed && (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
                {g.agents.map((a, i) => {
                  const cIdx = i % AVATAR_COLORS.length;
                  const cg = AVATAR_COLORS[cIdx];
                  return (
                    <button key={a.id} onClick={() => openDetail(a)}
                      className="card group relative overflow-hidden cursor-pointer text-left">
                      <div className="absolute top-0 left-0 w-full h-[2px] opacity-60 group-hover:opacity-100 transition-opacity"
                        style={{ background: `linear-gradient(90deg, ${cg.split(",")[0]}, transparent)` }} />
                      <div className="flex items-start gap-3">
                        <div className="w-10 h-10 rounded-xl flex items-center justify-center text-white font-bold shrink-0"
                          style={{ background: `linear-gradient(135deg, ${cg})` }}>
                          {a.name.slice(0, 1)}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="font-medium text-sm truncate">{a.name}</div>
                          <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                            {a.role || "未设岗位"}
                          </div>
                        </div>
                        {a.active_plans > 0 && (
                          <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 shrink-0">
                            📋{a.active_plans}
                          </span>
                        )}
                      </div>
                      <div className="mt-2.5 grid grid-cols-3 gap-1 text-center">
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-cyan-300">{a.skill_count}</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>技能</p>
                        </div>
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-purple-300">{a.memory_count}</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>记忆</p>
                        </div>
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-amber-300">{a.active_plans}</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>在学</p>
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}

      {!depts.length && (
        <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
          暂无员工数据
        </div>
      )}

      {/* 成长档案弹窗 */}
      {detail && detailModal}
    </div>
  );
}
