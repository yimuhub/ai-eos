"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import AttachmentPicker from "@/components/AttachmentPicker";
import type { Attachment } from "@/components/AttachmentPicker";

type Expert = {
  id: string; source: string; name: string; emoji?: string | null;
  color?: string | null; department: string; tags: string[];
  description: string; deliverables: string[]; enabled: boolean;
  hired_count: number; prompt_chars: number;
};
type ExpertDetail = Expert & { raw_md?: string };
type DeptRow = { department: string; count: number };
type JobBrief = { job_id: string; template: Expert; task: string };

const DEPT_LABELS: Record<string, string> = {
  company: "公司高管", engineering: "工程", design: "设计", marketing: "营销",
  "paid-media": "付费媒体", sales: "销售", finance: "金融", hr: "人力资源",
  legal: "法务", "supply-chain": "供应链", product: "产品",
  "project-management": "项目管理", testing: "测试", support: "支持",
  specialized: "专项", "spatial-computing": "空间计算",
  "game-development": "游戏开发", academic: "学术", gis: "GIS",
  security: "安全", strategy: "战略", integrations: "集成",
};

const COLOR_MAP: Record<string, string> = {
  purple: "from-violet-500 to-purple-600", pink: "from-pink-500 to-rose-600",
  blue: "from-blue-500 to-indigo-600", green: "from-emerald-500 to-teal-600",
  orange: "from-amber-500 to-orange-600", red: "from-rose-500 to-red-600",
};

export default function ExpertsPage() {
  const [list, setList] = useState<Expert[]>([]);
  const [depts, setDepts] = useState<DeptRow[]>([]);
  const [dept, setDept] = useState("all");
  const [q, setQ] = useState("");
  const [importing, setImporting] = useState(false);
  const [msg, setMsg] = useState("");

  // P1 找专家
  const [searchQ, setSearchQ] = useState("");
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchRes, setSearchRes] = useState<{ exp?: Expert; reason?: string }[] | null>(null);

  // P2 组队
  const [teamGoal, setTeamGoal] = useState("");
  const [teamBusy, setTeamBusy] = useState(false);
  const [teamRes, setTeamRes] = useState<JobBrief[] | null>(null);

  const [attachments, setAttachments] = useState<Attachment[]>([]);

  // 详情弹窗
  const [detail, setDetail] = useState<ExpertDetail | null>(null);
  const [hiring, setHiring] = useState(false);

  const load = useCallback(async () => {
    try {
      const [e, d] = await Promise.all([
        api.get("/experts"),
        api.get("/experts/departments"),
      ]);
      setList(e as Expert[]);
      setDepts(d as DeptRow[]);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    let out = list;
    if (dept !== "all") out = out.filter(e => e.department === dept);
    if (q.trim()) {
      const s = q.toLowerCase();
      out = out.filter(e =>
        e.name.toLowerCase().includes(s) ||
        (e.description || "").toLowerCase().includes(s) ||
        (e.tags || []).join(",").toLowerCase().includes(s));
    }
    return out;
  }, [list, dept, q]);

  const doImport = async () => {
    setImporting(true); setMsg("");
    try {
      const r = await api.post("/experts/import") as any;
      setMsg(`✅ 导入完成：新增/更新 ${r.imported_or_updated} 个专家（跳过 ${r.skipped}）`);
      await load();
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setImporting(false);
  };

  const doSearch = async () => {
    if (!searchQ.trim()) return;
    setSearchBusy(true); setSearchRes(null);
    try {
      const r = await api.post("/experts/search", { query: searchQ }) as any[];
      setSearchRes((r || []).filter((x: any) => x?.id)
        .map((x: any) => ({ exp: x, reason: x.reason })));
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setSearchBusy(false);
  };

  const doTeam = async () => {
    if (!teamGoal.trim()) return;
    setTeamBusy(true); setTeamRes(null);
    try {
      const r = await api.post("/experts/team", { goal: teamGoal }) as any;
      setTeamRes(r.jobs || []);
      setMsg(`✅ 已创建 ${r.team_size} 份团队蓝图，去工厂页审查批准`);
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setTeamBusy(false);
  };

  const hire = async (id: string) => {
    setHiring(true);
    try {
      const r = await api.post(`/experts/${id}/to-blueprint`) as any;
      setMsg(`✅ 蓝图已创建（${r.job_id.slice(0, 8)}），请到「Agent 工厂」审查批准后上岗`);
      setDetail(null);
      load();
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setHiring(false);
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="flex items-end justify-between mb-1">
        <h1 className="text-2xl font-bold">📚 专家库</h1>
        <button onClick={doImport} disabled={importing}
          className="btn-primary text-sm py-1.5 px-4 disabled:opacity-50">
          {importing ? "⏳ 导入中…" : "📥 导入 / 更新专家库"}
        </button>
      </div>
      <p className="text-sm text-[var(--muted)] mb-4">
        agency-agents-zh 专家角色库（MIT）· 挑人 → 工厂蓝图 → 老板审查 → 验收考试 → 上岗
      </p>

      {/* P1 找专家 + P2 组队 */}
      <div className="grid md:grid-cols-2 gap-3 mb-5">
        <div className="card p-3 space-y-2">
          <b className="text-sm">🔍 自然语言找专家</b>
          <div className="flex gap-2">
            <input value={searchQ} onChange={e => setSearchQ(e.target.value)}
              placeholder="如：我要做一个小红书种草内容专家" className="input-glow flex-1"
              onKeyDown={e => e.key === "Enter" && doSearch()} />
            <button onClick={doSearch} disabled={searchBusy}
              className="btn-primary px-3 text-sm disabled:opacity-50">
              {searchBusy ? "思考中…" : "找"}
            </button>
          </div>
          <AttachmentPicker value={attachments} onChange={setAttachments} label="📎 附加参考材料" />
          {searchRes && searchRes.length > 0 && (
            <div className="space-y-1.5">
              {searchRes.map((r, i) => (
                <div key={r.exp!.id} className="flex items-center gap-2 p-2 rounded-lg bg-white/[0.03] border border-[var(--border)] text-xs">
                  <span className="text-base">{r.exp!.emoji || "🧑‍💼"}</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium">{r.exp!.name}
                      <span className="ml-2 text-[var(--muted)]">{DEPT_LABELS[r.exp!.department] || r.exp!.department}</span>
                    </div>
                    <div className="text-[var(--muted)] truncate">{r.reason || r.exp!.description}</div>
                  </div>
                  <button onClick={() => hire(r.exp!.id)} className="btn-primary text-xs px-2.5 py-1">雇佣</button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card p-3 space-y-2">
          <b className="text-sm">👥 一句话组建专家团队</b>
          <div className="flex gap-2">
            <input value={teamGoal} onChange={e => setTeamGoal(e.target.value)}
              placeholder="如：组建一个新品上市小组" className="input-glow flex-1"
              onKeyDown={e => e.key === "Enter" && doTeam()} />
            <button onClick={doTeam} disabled={teamBusy}
              className="btn-primary px-3 text-sm disabled:opacity-50">
              {teamBusy ? "组建中…" : "组队"}
            </button>
          </div>
          <p className="text-xs text-[var(--muted)]">LLM 从库中挑互补工种 → 批量生成工厂蓝图，逐个审查批准</p>
          {teamRes && teamRes.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {teamRes.map((j, i) => (
                <span key={j.job_id} className="px-2 py-1 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 text-xs">
                  {j.template.emoji} {j.template.name}
                </span>
              ))}
              <a href="/factory" className="px-2 py-1 rounded-full bg-white/[0.04] border border-[var(--border)] text-xs text-[var(--muted)] hover:text-white">去工厂审查 →</a>
            </div>
          )}
        </div>
      </div>

      {msg && <div className="mb-4 text-sm text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-2">{msg}</div>}

      {/* 部门筛选 */}
      <div className="flex gap-1.5 mb-4 flex-wrap">
        <button onClick={() => setDept("all")}
          className={`px-3 py-1 rounded-full text-xs border transition-all ${dept === "all" ? "bg-indigo-500/30 border-indigo-400/50 text-white" : "border-[var(--border)] text-[var(--muted)] hover:text-white"}`}>
          全部 {list.length}
        </button>
        {depts.map(d => (
          <button key={d.department} onClick={() => setDept(d.department)}
            className={`px-3 py-1 rounded-full text-xs border transition-all ${dept === d.department ? "bg-indigo-500/30 border-indigo-400/50 text-white" : "border-[var(--border)] text-[var(--muted)] hover:text-white"}`}>
            {DEPT_LABELS[d.department] || d.department} {d.count}
          </button>
        ))}
      </div>

      {/* 搜索框 */}
      <input value={q} onChange={e => setQ(e.target.value)} placeholder="搜索专家名字 / 描述 / 标签…"
        className="input-glow w-full mb-4" />

      {/* 专家卡片 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {filtered.map(e => (
          <button key={e.id} onClick={() => api.get(`/experts/${e.id}`)
              .then((d: ExpertDetail) => setDetail(d))
              .catch(err => setMsg("❌ " + describeError(err)))}
            className="card p-4 text-left transition-all hover:-translate-y-0.5 hover:shadow-[0_0_20px_rgba(99,102,241,0.15)] group">
            <div className="flex items-center gap-3 mb-2">
              <div className={`w-10 h-10 rounded-full bg-gradient-to-br ${COLOR_MAP[e.color || "blue"] || COLOR_MAP.blue} flex items-center justify-center text-lg shrink-0`}>
                {e.emoji || "🧑‍💼"}
              </div>
              <div className="min-w-0">
                <div className="text-sm font-bold truncate group-hover:text-indigo-300">{e.name}</div>
                <div className="text-xs text-[var(--muted)]">
                  {DEPT_LABELS[e.department] || e.department}
                  {e.hired_count > 0 && <span className="ml-1.5 text-amber-400">×{e.hired_count} 雇佣</span>}
                </div>
              </div>
            </div>
            <p className="text-xs text-[var(--muted)] line-clamp-3 min-h-[3em]">{e.description}</p>
            {(e.deliverables || []).length > 0 && (
              <div className="text-xs text-indigo-300/80 mt-2 truncate">
                📦 {e.deliverables[0]}
              </div>
            )}
          </button>
        ))}
      </div>
      {list.length === 0 && (
        <div className="text-center py-16 text-[var(--muted)] text-sm border border-dashed border-[var(--border)] rounded-lg">
          专家库为空 — 点右上角「导入专家库」载入 265 位专家
        </div>
      )}

      {/* 详情弹窗 */}
      {detail && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setDetail(null)} />
          <div className="relative w-full max-w-4xl max-h-[90vh] bg-[var(--card)] backdrop-blur-xl border border-[var(--border)] rounded-2xl shadow-2xl overflow-y-auto p-6 animate-fade-up">
            <div className="flex items-center gap-3 mb-4">
              <div className={`w-12 h-12 rounded-full bg-gradient-to-br ${COLOR_MAP[detail.color || "blue"] || COLOR_MAP.blue} flex items-center justify-center text-2xl`}>
                {detail.emoji || "🧑‍💼"}
              </div>
              <div className="flex-1">
                <h2 className="text-lg font-bold">{detail.name}</h2>
                <div className="text-xs text-[var(--muted)]">
                  {DEPT_LABELS[detail.department] || detail.department} · {detail.source} · MIT 许可
                </div>
              </div>
              <button onClick={() => hire(detail.id)} disabled={hiring}
                className="btn-primary text-sm disabled:opacity-50">
                {hiring ? "创建中…" : "🏭 雇佣（进工厂蓝图）"}
              </button>
              <button onClick={() => setDetail(null)} className="text-[var(--muted)] hover:text-white text-xl px-2">✕</button>
            </div>

            <p className="text-sm text-[var(--muted)] mb-3">{detail.description}</p>

            {(detail.deliverables || []).length > 0 && (
              <div className="mb-3">
                <b className="text-xs text-indigo-300">📦 核心交付物（验收出题依据）</b>
                <ul className="mt-1.5 space-y-1">
                  {detail.deliverables.map((d, i) => (
                    <li key={i} className="text-xs text-[var(--muted)] flex gap-2"><span className="text-indigo-400">·</span>{d}</li>
                  ))}
                </ul>
              </div>
            )}

            <details className="mb-3">
              <summary className="text-xs text-indigo-300 cursor-pointer">📄 查看完整人设原文（{(detail.raw_md || "").length} 字符）</summary>
              <pre className="mt-2 text-xs whitespace-pre-wrap bg-black/30 border border-[var(--border)] rounded-lg p-3 max-h-64 overflow-y-auto">{detail.raw_md}</pre>
            </details>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
