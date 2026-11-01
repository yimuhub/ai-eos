"use client";
import { useCallback, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { invalidate, useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker from "@/components/AttachmentPicker";
import type { Attachment } from "@/components/AttachmentPicker";

type Expert = {
  id: string; source: string; name: string; emoji?: string | null;
  color?: string | null; department: string; tags: string[];
  description: string; deliverables: string[]; enabled: boolean;
  hired_count: number; prompt_chars: number; license?: string | null;
  recommended_packs?: string[];
};
type ExpertDetail = Expert & { raw_md?: string };
type DeptRow = { department: string; count: number };
type JobBrief = { job_id: string; template: Expert; task: string };

/** 技能包 key → 中文标签（与后端 capability_packs 对齐，用于"推荐技能包"徽章） */
const PACK_LABELS: Record<string, string> = {
  computer_use: "🖥 电脑操作",
  web_browsing: "🌐 网页浏览",
  persistent_memory: "🧠 持久记忆",
  self_learning: "📈 自我学习",
  scheduled_work: "⏰ 周期性工作",
};

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

/** 把人设 raw_md 按 "## " 二级标题拆成有序章节（参考上游"一页一份完整档案"形式）。
 *  返回 [{ title, body }]，正文已去掉标题行本身。 */
function parseSections(rawMd?: string): { title: string; body: string }[] {
  if (!rawMd) return [];
  const lines = rawMd.split("\n");
  const out: { title: string; body: string }[] = [];
  let cur: { title: string; body: string } | null = null;
  let inFrontmatter = false;
  for (const ln of lines) {
    if (ln.trim() === "---") { inFrontmatter = !inFrontmatter; continue; }
    if (inFrontmatter) continue; // frontmatter 不展示
    const h = ln.match(/^##\s+(.+)$/);
    if (h) {
      if (cur) out.push(cur);
      cur = { title: h[1].trim(), body: "" };
    } else if (ln.startsWith("# ") || ln.startsWith("> ")) {
      // 一级标题(H1)和引言(>)并入第一个章节前的"导语"，单独不成节
      if (cur) cur.body += ln + "\n";
      else if (ln.trim() && out.length === 0) out.push({ title: "导语", body: ln + "\n" });
    } else if (cur) {
      cur.body += ln + "\n";
    }
  }
  if (cur) out.push(cur);
  return out.filter(s => s.body.trim() || s.title !== "导语");
}

/** 章节标题 → 强调色（对应原玻璃拟态体系） */
const SECTION_ACCENT: Record<string, string> = {
  "使命": "text-cyan-300", "规则": "text-amber-300", "交付物": "text-emerald-300",
  "工作流程": "text-indigo-300", "沟通风格": "text-pink-300", "成功指标": "text-green-300",
  "进阶能力": "text-violet-300", "身份": "text-sky-300", "记忆": "text-sky-300",
};

export default function ExpertsPage() {
  // 专家库**按公司隔离**：`lib/api.ts` 会给 /experts 自动补上当前公司的
  // company_id，后端也只回该公司的模板。所以缓存 key 必须带 cid——
  // 不带的话切了公司这一页仍显示上一家公司的专家（同 key 的缓存在 30 秒内直接复用），
  // 而卡片上的「雇佣」会把别家公司的人设招进当前公司。
  // 两个接口互不依赖：并发发出、各自缓存、各自重取。
  const cid = useActiveCompanyId();
  const { data: expertData, error: loadError, loading: listLoading, refresh: refreshExperts } =
    useResource<Expert[]>(cid ? `experts:list:${cid}` : null,
      () => api.get<Expert[]>("/experts"));
  const { data: deptData, refresh: refreshDepts } =
    useResource<DeptRow[]>(cid ? `experts:departments:${cid}` : null,
      () => api.get<DeptRow[]>("/experts/departments"));
  // cid 还在确认（首帧）时也算加载中，否则会先闪一下"暂无专家"
  const loading = cid === undefined || listLoading;

  const list = useMemo<Expert[]>(() => expertData ?? [], [expertData]);
  const depts = useMemo<DeptRow[]>(() => deptData ?? [], [deptData]);
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

  // V5.0 专家管理：复制 / 编辑 / 手动新建
  const [copying, setCopying] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editingBusy, setEditingBusy] = useState(false);
  const [editForm, setEditForm] = useState<Record<string, string>>({});
  const [showManual, setShowManual] = useState(false);
  const [manualBusy, setManualBusy] = useState(false);
  const [manualForm, setManualForm] = useState<Record<string, string>>({});

  // 专家库变了（导入 / 复制 / 编辑 / 新建 / 雇佣计数）→ 列表与部门计数一起重取
  const reload = useCallback(() => {
    refreshExperts();
    refreshDepts();
  }, [refreshExperts, refreshDepts]);

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
      const r = await api.post("/experts/import-original") as any;
      setMsg(`✅ 专家库已就位：新增 ${r.created} / 更新 ${r.updated}，下架旧上游 ${r.disabled_legacy} 个`);
      reload();
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
      invalidate("factory:"); // 新蓝图要出现在工厂列表里
      setMsg(`✅ 已创建 ${r.team_size} 份团队蓝图，去工厂页审查批准`);
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setTeamBusy(false);
  };

  const hire = async (id: string) => {
    setHiring(true);
    try {
      const r = await api.post(`/experts/${id}/to-blueprint`) as any;
      setMsg(`✅ 蓝图已创建（${r.job_id.slice(0, 8)}），请到「Agent 工厂」审查批准后上岗`);
      invalidate("factory:"); // 新蓝图要出现在工厂列表里
      setDetail(null);
      reload();
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setHiring(false);
  };

  // V5.0 复制专家
  const doCopy = async () => {
    if (!detail) return;
    setCopying(true);
    try {
      const r = await api.post(`/experts/${detail.id}/copy`) as any;
      setMsg(`✅ 已复制为「${r.name}」，可继续编辑`);
      reload();
      setDetail({ ...detail, id: r.id, name: r.name });
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setCopying(false);
  };

  // V5.0 编辑专家
  const openEdit = (id: string) => {
    setEditing(id);
    setEditForm({});
    api.get(`/experts/${id}`).then((d: any) => {
      setEditForm({
        name: d.name || "", emoji: d.emoji || "", department: d.department || "",
        description: d.description || "", system_prompt: d.system_prompt || "",
        raw_md: d.raw_md || "",
      });
    }).catch(err => setMsg("❌ " + describeError(err)));
  };
  const saveEdit = async () => {
    if (!editing) return;
    setEditingBusy(true);
    try {
      const payload: Record<string, string> = {};
      (["name", "emoji", "department", "description", "system_prompt", "raw_md"] as string[]).forEach(k => {
        if (editForm[k] !== undefined) payload[k] = editForm[k];
      });
      await api.put(`/experts/${editing}`, payload);
      setMsg("✅ 专家已更新");
      setEditing(null); setDetail(null);
      reload();
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setEditingBusy(false);
  };

  // V5.0 手动新建专家
  const openManual = () => {
    setManualForm({});
    setShowManual(true);
  };
  const saveManual = async () => {
    setManualBusy(true);
    try {
      const payload: Record<string, unknown> = { name: (manualForm.name || "").trim() };
      (["emoji", "department", "description", "system_prompt"] as string[]).forEach(k => {
        if ((manualForm[k] || "").trim()) payload[k] = manualForm[k];
      });
      if (attachments.length) payload.attachments = attachments.map(a => a.name);
      await api.post("/experts/manual", payload);
      setMsg("✅ 新专家已创建");
      setShowManual(false); setAttachments([]);
      reload();
    } catch (e) { setMsg("❌ " + describeError(e)); }
    setManualBusy(false);
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="flex items-end justify-between mb-1">
        <h1 className="text-2xl font-bold">📚 专家库</h1>
        <div className="flex gap-2">
          <button onClick={openManual} className="btn-ghost text-sm py-1.5 px-3">＋ 手动添加专家</button>
          <button onClick={doImport} disabled={importing}
            className="btn-primary text-sm py-1.5 px-4 disabled:opacity-50">
            {importing ? "⏳ 处理中…" : "📥 载入专家库"}
          </button>
        </div>
      </div>
      <p className="text-sm text-[var(--muted)] mb-2">
        {list.length} 位专家 · 挑人 → 工厂蓝图 → 老板审查 → 验收考试 → 上岗
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
      {cid === null && (
        <div className="mb-4 text-sm text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg p-2">
          你还没有创建 AI 公司——专家库按公司隔离，请先建公司再来雇佣。
        </div>
      )}
      {loadError && (
        <div className="mb-4 text-sm text-red-300 bg-red-500/10 border border-red-500/30 rounded-lg p-2">
          ❌ 加载专家库失败：{loadError}
          <button onClick={() => reload()} className="btn-ghost !py-0.5 text-xs ml-2">重试</button>
        </div>
      )}

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
      {!loading && list.length === 0 && (
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
                  {DEPT_LABELS[detail.department] || detail.department}
                </div>
              </div>
              <button onClick={doCopy} disabled={copying}
                className="btn-ghost text-sm disabled:opacity-50">📋 复制</button>
              <button onClick={() => openEdit(detail.id)} disabled={editingBusy}
                className="btn-ghost text-sm disabled:opacity-50">✏️ 编辑</button>
              <button onClick={() => hire(detail.id)} disabled={hiring}
                className="btn-primary text-sm disabled:opacity-50">
                {hiring ? "创建中…" : "🏭 雇佣（进工厂蓝图）"}
              </button>
              <button onClick={() => setDetail(null)} className="text-[var(--muted)] hover:text-white text-xl px-2">✕</button>
            </div>

            <p className="text-sm text-[var(--muted)] mb-3">{detail.description}</p>

            {/* V5.0.2 专家↔技能包：推荐技能包（招聘/上岗时自动按此预配） */}
            {(detail.recommended_packs || []).length > 0 && (
              <div className="mb-3">
                <p className="text-[11px] text-[var(--muted)] mb-1.5">🧩 推荐技能包（上岗自动预配）</p>
                <div className="flex flex-wrap gap-1.5">
                  {detail.recommended_packs!.map((k) => (
                    <span key={k} className="px-2 py-0.5 rounded-full text-[11px] bg-white/[0.05] border border-[var(--border)]">
                      {PACK_LABELS[k] || k}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 章节化人设档案（参考上游"一页一份完整档案"形式，按 ## 章节结构化呈现） */}
            {(() => {
              const secs = parseSections(detail.raw_md);
              if (secs.length === 0) return null;
              return (
                <div className="space-y-2 mb-3">
                  {secs.map((s, i) => {
                    const accent = Object.entries(SECTION_ACCENT)
                      .find(([k]) => s.title.includes(k))?.[1] || "text-indigo-300";
                    return (
                      <details key={i} className="card p-3" open={i < 2}>
                        <summary className={`cursor-pointer select-none text-sm font-semibold ${accent}`}>
                          {s.title}
                        </summary>
                        <pre className="mt-2 text-xs leading-relaxed whitespace-pre-wrap text-[var(--muted)]">
                          {s.body.trim()}
                        </pre>
                      </details>
                    );
                  })}
                </div>
              );
            })()}

            <details className="mb-3">
              <summary className="text-xs text-indigo-300 cursor-pointer">
                📄 查看原始 Markdown（{(detail.raw_md || "").length} 字符）
              </summary>
              <pre className="mt-2 text-xs whitespace-pre-wrap bg-black/30 border border-[var(--border)] rounded-lg p-3 max-h-64 overflow-y-auto">{detail.raw_md}</pre>
            </details>
          </div>
        </div>,
        document.body
      )}

      {/* V5.0 编辑专家弹窗 */}
      {editing && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setEditing(null)} />
          <div className="relative w-full max-w-2xl max-h-[90vh] bg-[var(--card)] backdrop-blur-xl border border-[var(--border)] rounded-2xl shadow-2xl overflow-y-auto p-6 animate-fade-up">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-bold">✏️ 编辑专家</h2>
              <button onClick={() => setEditing(null)} className="text-[var(--muted)] hover:text-white text-xl px-2">✕</button>
            </div>
            <div className="grid grid-cols-3 gap-2.5">
              <label className="col-span-2 text-xs space-y-1">
                <span className="text-[var(--muted)]">姓名</span>
                <input className="input" value={editForm.name || ""}
                  onChange={e => setEditForm({ ...editForm, name: e.target.value })} />
              </label>
              <label className="text-xs space-y-1">
                <span className="text-[var(--muted)]">Emoji</span>
                <input className="input" value={editForm.emoji || ""}
                  onChange={e => setEditForm({ ...editForm, emoji: e.target.value })} />
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">部门</span>
                <select className="input" value={editForm.department || ""}
                  onChange={e => setEditForm({ ...editForm, department: e.target.value })}>
                  {Object.entries(DEPT_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">简介</span>
                <textarea rows={2} className="input" value={editForm.description || ""}
                  onChange={e => setEditForm({ ...editForm, description: e.target.value })} />
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">人设正文（system prompt，支持 8 章节 Markdown）</span>
                <textarea rows={8} className="input font-mono text-xs" value={editForm.system_prompt || ""}
                  onChange={e => setEditForm({ ...editForm, system_prompt: e.target.value })} />
              </label>
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => setEditing(null)} className="btn-ghost text-sm">取消</button>
              <button onClick={saveEdit} disabled={editingBusy} className="btn-primary text-sm disabled:opacity-50">
                {editingBusy ? "保存中…" : "保存修改"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* V5.0 手动新建专家弹窗（必带 📎 附件口） */}
      {showManual && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setShowManual(false)} />
          <div className="relative w-full max-w-2xl max-h-[90vh] bg-[var(--card)] backdrop-blur-xl border border-[var(--border)] rounded-2xl shadow-2xl overflow-y-auto p-6 animate-fade-up">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-bold">＋ 手动添加专家</h2>
                <p className="text-xs text-[var(--muted)] mt-0.5">凭空定义一位专家人设，可附参考材料/能力包</p>
              </div>
              <button onClick={() => setShowManual(false)} className="text-[var(--muted)] hover:text-white text-xl px-2">✕</button>
            </div>
            <div className="grid grid-cols-3 gap-2.5">
              <label className="col-span-2 text-xs space-y-1">
                <span className="text-[var(--muted)]">姓名 <b className="text-red-400">*</b></span>
                <input className="input" placeholder="如：文旅策划总监"
                  value={manualForm.name || ""}
                  onChange={e => setManualForm({ ...manualForm, name: e.target.value })} />
              </label>
              <label className="text-xs space-y-1">
                <span className="text-[var(--muted)]">Emoji</span>
                <input className="input" placeholder="🏞️" value={manualForm.emoji || ""}
                  onChange={e => setManualForm({ ...manualForm, emoji: e.target.value })} />
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">部门</span>
                <select className="input" value={manualForm.department || ""}
                  onChange={e => setManualForm({ ...manualForm, department: e.target.value })}>
                  <option value="">通用</option>
                  {Object.entries(DEPT_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">简介</span>
                <textarea rows={2} className="input" placeholder="一句话说明这位专家擅长什么"
                  value={manualForm.description || ""}
                  onChange={e => setManualForm({ ...manualForm, description: e.target.value })} />
              </label>
              <label className="text-xs space-y-1 col-span-3">
                <span className="text-[var(--muted)]">人设正文（身份/使命/规则/交付物/流程，支持 8 章节 Markdown）</span>
                <textarea rows={8} className="input font-mono text-xs"
                  placeholder={"## 🧠 你的身份与记忆\n你是一位…\n## 🎯 核心使命\n…"}
                  value={manualForm.system_prompt || ""}
                  onChange={e => setManualForm({ ...manualForm, system_prompt: e.target.value })} />
              </label>
            </div>
            <div className="mt-3">
              <AttachmentPicker value={attachments} onChange={setAttachments} label="📎 附参考材料 / 能力包 / 知识库文档" />
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => setShowManual(false)} className="btn-ghost text-sm">取消</button>
              <button onClick={saveManual} disabled={manualBusy || !(manualForm.name || "").trim()}
                className="btn-primary text-sm disabled:opacity-50">
                {manualBusy ? "创建中…" : "创建专家"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
