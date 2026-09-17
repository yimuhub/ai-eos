"use client";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/components/Toast";

/** 把任意异常转成一句可读的原因，避免界面出现 "[object Object]" */
function describe(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return fallback;
}

/* ============================================================
   复盘中心：按部门聚合所有员工的复盘教训 + 手动触发复盘
   UI 完全对标成长中心（深空底/玻璃卡/渐变头像/呼吸点/展开区块）
   ============================================================ */

const AVATAR_COLORS = ["#f59e0b,#ef4444", "#6366f1,#8b5cf6", "#22d3ee,#3b82f6",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#ec4899"];

type AgentSummary = {
  id: string; name: string; role?: string | null; status: string;
  lesson_count: number;
};
type DeptGroup = { dept_id: string; dept_name: string; agents: AgentSummary[] };
type Lesson = { id: string; lesson: string; lesson_type: string };
type Reflections = { id: string; agent_id: string; agent_name: string; reflections: string[]; lessons: Lesson[]; triggered_at: string };

export default function ReflectionCenterPage() {
  const [mounted, setMounted] = useState(false);
  const [cid, setCid] = useState("");
  const [msg, setMsg] = useState("");
  const [depts, setDepts] = useState<DeptGroup[]>([]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [pinnedDepts, setPinnedDepts] = useState<string[]>(() => {
    if (typeof window === "undefined") return [];
    try { return JSON.parse(localStorage.getItem("aieos_reflection_pinned") || "[]"); } catch { return []; }
  });
  // 置顶持久化
  useEffect(() => {
    try { localStorage.setItem("aieos_reflection_pinned", JSON.stringify(pinnedDepts)); } catch {}
  }, [pinnedDepts]);

  // 部门加载顺序：先 pinned 再 rest
  const sortedDepts = [...depts].sort((a, b) => {
    const pa = pinnedDepts.includes(a.dept_name) ? 0 : 1;
    const pb = pinnedDepts.includes(b.dept_name) ? 0 : 1;
    return pa - pb || a.dept_name.localeCompare(b.dept_name);
  });

  const [detail, setDetail] = useState<{ agent: AgentSummary } | null>(null);
  const [detailData, setDetailData] = useState<Reflections | null>(null);
  const [detailLessons, setDetailLessons] = useState<Lesson[]>([]);
  const [openSection, setOpenSection] = useState<string | null>(null);
  // 触发复盘
  const [reason, setReason] = useState("");
  const [triggering, setTriggering] = useState(false);
  // 删除
  const [busy, setBusy] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    try {
      const cs: any[] = await api.get("/companies");
      if (!cs.length) return;
      setCid(cs[0].id);
      const d = await api.get(`/reflection/agents-overview?company_id=${cs[0].id}`);
      setDepts(d.departments || []);
    } catch (e: any) { setMsg(String(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function openDetail(a: AgentSummary) {
    setDetail({ agent: a });
    setDetailData(null); setDetailLessons([]);
    setOpenSection(null);
    try {
      const r = await api.get(`/reflection/agents/${a.id}/reflections`);
      setDetailData(r);
      // lessons 也单独拉（旧数据可能只有 lessons 端点）
      api.get(`/reflection/agents/${a.id}/lessons`).then(setDetailLessons).catch(() => {});
    } catch {}
  }

  async function triggerReflect(aid: string) {
    if (!reason.trim()) { setMsg("请填写关注点"); return; }
    setTriggering(true);
    try {
      // 通过 /experiences 触发复盘（把关注点写入 summary，outcome=PARTIAL 表示复盘关注）
      await api.post('/reflection/experiences', {
        agent_id: aid, title: '复盘触发', outcome: 'PARTIAL',
        summary: reason.trim(), task_id: null, project_id: null,
        user_feedback: null, execution_output: null, errors: null,
      });
      setMsg(`✅ 已对「${detail?.agent.name}」触发复盘`);
      setReason("");
      openDetail(detail!.agent); // 刷新数据
    } catch (e: any) { setMsg("❌ " + (e.message || String(e))); }
    setTriggering(false);
  }

  async function delLesson(lessonId: string) {
    if (!detail) return;
    try {
      await api.del(`/reflection/agents/${detail.agent.id}/lessons/${lessonId}`);
      setDetailLessons(prev => prev.filter(l => l.id !== lessonId));
      if (detailData) {
        setDetailData({ ...detailData, lessons: detailData.lessons.filter((l: Lesson) => l.id !== lessonId) });
      }
      notify("success", "已删除该条经验教训");
    } catch (e) {
      // 原实现是 `catch {}` 静默吞掉：删除失败时列表毫无变化，
      // 用户不知道是没删掉还是没生效。现在必须说明原因。
      notify("error", describe(e, "删除失败"));
    }
  }

  const detailModal = mounted && detail ? createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
      onClick={() => setDetail(null)}>
      <div className="w-full max-w-3xl rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto animate-fade-up"
        style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(245,158,11,0.4)" }}
        onClick={(e) => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border)" }}>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-2xl flex items-center justify-center text-white text-lg font-bold"
              style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>
              {detail!.agent.name.slice(0, 1)}
            </div>
            <div>
              <h3 className="font-semibold text-base">{detail!.agent.name}</h3>
              <p className="text-xs" style={{ color: "var(--muted)" }}>
                {detail!.agent.role || "未设岗位"} · 复盘档案
              </p>
            </div>
          </div>
          <button onClick={() => setDetail(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
        </div>

        {/* KPI 小计 */}
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-amber-300">
              {detailData?.lessons?.length ?? detailLessons.length ?? detail!.agent.lesson_count ?? 0}
            </p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>经验教训</p>
          </div>
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-orange-300">
              {detailData?.reflections?.length ?? 0}
            </p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>复盘轮次</p>
          </div>
          <div className="rounded-xl py-2.5" style={{ background: "var(--surface-2)" }}>
            <p className="text-xl font-bold text-red-300">
              {detailData?.reflections?.reduce((a: number, r: any) => a + (r.lessons?.length || 0), 0) ?? 0}
            </p>
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>本轮新收获</p>
          </div>
        </div>

        {/* 触发复盘 */}
        <div className="rounded-xl p-3.5 space-y-2.5" style={{ background: "rgba(245,158,11,0.06)", border: "1px solid rgba(245,158,11,0.2)" }}>
          <p className="text-xs font-semibold text-amber-300">🔥 触发复盘（调用 LLM）</p>
          <input value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="关注点（如：上次任务失败原因、需改进的沟通方式）"
            className="input !py-2 text-xs" />
          <button onClick={() => triggerReflect(detail!.agent.id)}
            disabled={triggering || !reason.trim()}
            className="btn-primary disabled:opacity-40 text-xs">
            {triggering ? "LLM 处理中…" : "开始复盘"}
          </button>
        </div>

        {/* 可展开区块 */}
        {[
          { key: "lessons", icon: "💡", title: `经验教训库（${detailLessons.length || detailData?.lessons?.length || 0} 条）`, color: "#fbbf24" },
          { key: "reflections", icon: "🔄", title: `复盘记录（${detailData?.reflections?.length || 0} 轮）`, color: "#fb923c" },
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
                <div className="px-3 pb-3 space-y-1.5 max-h-64 overflow-y-auto animate-fade-up">
                  {sec.key === "lessons" && (detailLessons.length ? detailLessons.map((l: Lesson) => (
                    <div key={l.id} className="flex items-start gap-2 px-2.5 py-1.5 rounded-lg text-xs"
                      style={{ background: "rgba(255,255,255,0.03)" }}>
                      <span className="flex-1 leading-relaxed">{l.lesson}</span>
                      <button onClick={() => delLesson(l.id)}
                        className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-md text-red-300 hover:bg-red-500/15">🗑</button>
                    </div>
                  )) : (
                    detailData?.lessons?.length ? detailData.lessons.map((l: Lesson) => (
                      <div key={l.id} className="flex items-start gap-2 px-2.5 py-1.5 rounded-lg text-xs"
                        style={{ background: "rgba(255,255,255,0.03)" }}>
                        <span className="flex-1 leading-relaxed">{l.lesson}</span>
                        <button onClick={() => delLesson(l.id)}
                          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-md text-red-300 hover:bg-red-500/15">🗑</button>
                      </div>
                    )) : <p className="text-[11px] text-center py-2" style={{ color: "var(--muted)" }}>暂无经验教训</p>
                  ))}

                  {sec.key === "reflections" && detailData?.reflections?.length ? detailData.reflections.map((r: any, idx: number) => (
                    <div key={r.id || idx} className="space-y-1.5">
                      <p className="text-[10px] font-semibold" style={{ color: sec.color }}>
                        📅 {(r.triggered_at || "").slice(0, 16)}{r.agent_id === detail.agent.id ? "" : ` · ${r.agent_name}`}
                      </p>
                      {(r.lessons || r.reflections || [r.review]).map((item: string, i: number) => (
                        <div key={i} className="px-2.5 py-1.5 rounded-lg text-xs"
                          style={{ background: "rgba(255,255,255,0.03)", color: "var(--foreground)" }}>
                          {item}
                        </div>
                      ))}
                    </div>
                  )) : (
                    <p className="text-[11px] text-center py-2" style={{ color: "var(--muted)" }}>暂无复盘记录 · 先触发复盘</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>, document.body
  ) : null;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(245,158,11,0.12) 0%, rgba(239,68,68,0.08) 100%)", border: "1px solid rgba(245,158,11,0.25)" }}>
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>
            🔄
          </div>
          <div>
            <h1 className="text-xl font-bold text-[var(--foreground)]">复盘中心</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              按部门查看员工复盘教训 · 手动触发 LLM 复盘
            </p>
          </div>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(245,158,11,0.1)", color: "#fcd34d", border: "1px solid rgba(245,158,11,0.25)" }}>
          {msg}
        </div>
      )}

      {/* 部门分组复盘档案 */}
      {sortedDepts.map((g, deptIdx) => {
        const grad = AVATAR_COLORS[deptIdx % AVATAR_COLORS.length];
        const isCollapsed = collapsed.has(g.dept_id);
        return (
          <div key={g.dept_id} className="mb-7">
            <div className="flex items-center gap-3 mb-3 group/dept">
              <button onClick={() => setCollapsed((prev) => {
                const n = new Set(prev); n.has(g.dept_id) ? n.delete(g.dept_id) : n.add(g.dept_id); return n;
              })} className="flex items-center gap-2.5">
                <span className={`text-orange-300 transition-transform ${isCollapsed ? "-rotate-90" : ""}`}>▾</span>
                <span className="w-8 h-8 rounded-lg flex items-center justify-center text-sm"
                  style={{ background: `linear-gradient(135deg, ${grad})` }}>🔥</span>
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
                  const cg = AVATAR_COLORS[i % AVATAR_COLORS.length];
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
                      </div>
                      <div className="mt-2.5 grid grid-cols-3 gap-1 text-center">
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-amber-300">{a.lesson_count}</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>教训</p>
                        </div>
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-orange-300">🔄</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>复盘</p>
                        </div>
                        <div className="rounded-lg py-1" style={{ background: "var(--surface-2)" }}>
                          <p className="text-sm font-semibold text-red-300">⚡</p>
                          <p className="text-[9px]" style={{ color: "var(--muted)" }}>触发</p>
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

      {detail && detailModal}
    </div>
  );
}
