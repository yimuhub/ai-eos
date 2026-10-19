"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import type { Company } from "@/lib/types";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

/* ============================================================
   组织架构 v3：卡片排序/置顶 · 部门技能 · 部门专属知识库
   ============================================================ */

type Dept = {
  id: string; name: string;
  parent_department_id: string | null;
  description: string | null;
  sort_order?: number; pinned?: boolean;
  skill_ids?: string[]; kb_document_ids?: string[];
  attachments?: Attachment[];
};
type Agent = { id: string; name: string; role: string | null; department_id: string | null };
type SkillRow = { id: string; name: string; slug: string };
type KbDoc = { id: string; title: string; chunk_count: number };

const DEPT_GRADS = [
  "#6366f1,#a78bfa", "#22d3ee,#3b82f6", "#f59e0b,#f97316",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#8b5cf6,#6366f1",
];
function gradOf(seed: string) {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return DEPT_GRADS[h % DEPT_GRADS.length];
}

export default function OrganizationPage() {
  const [depts, setDepts] = useState<Dept[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [allSkills, setAllSkills] = useState<SkillRow[]>([]);
  const [kbDocs, setKbDocs] = useState<KbDoc[]>([]);
  const [msg, setMsg] = useState("");
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  const [editing, setEditing] = useState<Dept | null>(null);
  const [editForm, setEditForm] = useState({
    name: "", description: "", parent_department_id: "",
    skills: new Set<string>(), kbDocs: new Set<string>(),
  });
  const [deptAttachments, setDeptAttachments] = useState<Attachment[]>([]); // 📎 部门附件口
  const [showBatchAdd, setShowBatchAdd] = useState(false);
  const [batchParent, setBatchParent] = useState("");
  const [batchText, setBatchText] = useState("");
  const [showBatchDelete, setShowBatchDelete] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  // 排序模式
  const [sortMode, setSortMode] = useState(false);

  // V5.0 行业组织形态预设一键引入
  type PresetRow = { id: string; industry: string; emoji: string; department_count: number; agent_count: number };
  type PresetDetail = { id: string; industry: string; emoji: string;
    departments: { name: string; desc: string; agents: { name: string; role: string; expert_slug: string }[] }[] };
  const [showPresets, setShowPresets] = useState(false);
  const [presets, setPresets] = useState<PresetRow[]>([]);
  const [presetDetail, setPresetDetail] = useState<PresetDetail | null>(null);
  const [presetChecked, setPresetChecked] = useState<Set<string>>(new Set());
  const [presetApplying, setPresetApplying] = useState(false);
  const [presetDry, setPresetDry] = useState<{ departments: number; agents: number; note: string } | null>(null);
  const cidRef = useRef<string>("");  // 当前公司 id（load 里赋值，供预设引入用）

  const loadPresets = useCallback(async () => {
    try { setPresets(await api.get<PresetRow[]>("/org-presets")); } catch {}
  }, []);
  useEffect(() => { loadPresets(); }, [loadPresets]);

  async function previewPreset(p: PresetRow) {
    setShowPresets(false);
    setPresetDry(null);
    try {
      const d: PresetDetail = await api.get(`/org-presets/${p.id}`);
      setPresetDetail(d);
      setPresetChecked(new Set(d.departments.map((x) => x.name)));
      setMsg("");
      // 立即 dry_run 预览
      const r = await api.post<{ created_departments: number; created_agents: number; skipped_agents: number; note: string }>(
        `/org-presets/${p.id}/apply`,
        { company_id: firstCompanyId(), departments: d.departments.map((x) => x.name), dry_run: true } as any,
      ) as any;
      setPresetDry({ departments: r.created_departments, agents: r.created_agents, note: `将建 ${r.created_departments} 部门 / ${r.created_agents} 员工，跳过已存在 ${r.skipped_agents} 个` });
    } catch (e) { setMsg(`❌ ${describeError(e, "加载预设失败")}`); }
  }

  async function applyPreset() {
    if (!presetDetail) return;
    setPresetApplying(true); setMsg("");
    try {
      const r = await api.post<{ created_departments: number; created_agents: number; skipped_agents: number; skipped_departments: number }>(
        `/org-presets/${presetDetail.id}/apply`,
        { company_id: firstCompanyId(), departments: Array.from(presetChecked) } as any,
      ) as any;
      setMsg(`✅ 已引入「${presetDetail.industry}」：${r.created_departments} 部门 / ${r.created_agents} 员工（跳过 ${r.skipped_departments} 部门 / ${r.skipped_agents} 员工）`);
      setPresetDetail(null);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "引入失败")}`); }
    setPresetApplying(false);
  }

  function firstCompanyId(): string {
    // 组织架构页以公司首个 cid 为准（load 里赋值到 cidRef）
    return cidRef.current;
  }

  const load = useCallback(async () => {
    const cs = await api.get<Company[]>("/companies");
    if (!cs.length) return;
    const cid = cs[0].id;
    cidRef.current = cid;
    const ds: Dept[] = await api.get(`/companies/${cid}/departments`);
    setDepts(ds);
    setAgents(await api.get(`/agents?company_id=${cid}`));
    try { setAllSkills(await api.get("/skills")); } catch {}
    try { setKbDocs(await api.get(`/knowledge/documents?company_id=${cid}`)); } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

  /* 置顶在前 → sort_order 升序（后端已排）*/
  const tree = useMemo(() => {
    const childrenMap: Record<string, Dept[]> = {};
    const roots: Dept[] = [];
    for (const d of depts) {
      if (d.parent_department_id && depts.some((x) => x.id === d.parent_department_id)) {
        (childrenMap[d.parent_department_id] ||= []).push(d);
      } else {
        roots.push(d);
      }
    }
    return { roots, childrenMap };
  }, [depts]);

  const agentsOf = (deptId: string) => agents.filter((a) => a.department_id === deptId);

  /* ---------- 增改删 ---------- */
  function openCreate(parentId: string | null = null) {
    setEditing({ id: "__new__", name: "", parent_department_id: parentId, description: null });
    setEditForm({ name: "", description: "", parent_department_id: parentId || "", skills: new Set(), kbDocs: new Set() });
    setDeptAttachments([]);
  }
  function openEdit(d: Dept) {
    setEditing(d);
    setEditForm({
      name: d.name,
      description: d.description || "",
      parent_department_id: d.parent_department_id || "",
      skills: new Set(d.skill_ids || []),
      kbDocs: new Set(d.kb_document_ids || []),
    });
    setDeptAttachments(d.attachments || []); // 📎 载入已有附件，避免 PATCH 覆盖丢失
  }

  async function saveDept() {
    if (!editForm.name.trim()) return;
    try {
      if (editing?.id === "__new__") {
        const cs = await api.get<Company[]>("/companies");
        await api.post(`/departments?cid=${cs[0].id}`, {
          name: editForm.name.trim(),
          description: editForm.description || null,
          parent_department_id: editForm.parent_department_id || null,
          attachments: deptAttachments.length ? deptAttachments : undefined, // 📎
        });
        setMsg(`✅ 已创建部门「${editForm.name}」`);
        setDeptAttachments([]);
      } else if (editing) {
        await api.patch(`/departments/${editing.id}`, {
          name: editForm.name.trim(),
          description: editForm.description || null,
          parent_department_id: editForm.parent_department_id || null,
          skill_ids: Array.from(editForm.skills),
          kb_document_ids: Array.from(editForm.kbDocs),
          attachments: deptAttachments, // 📎 带上当前附件列表（含载入的已有附件）
        });
        setMsg(`✅ 部门「${editForm.name}」已更新`);
      }
      setEditing(null);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
  }

  async function deleteOne(d: Dept) {
    if (!(await confirmAction({
      title: `确认删除部门「${d.name}」？`,
      description: "部门本身会被删除，但下级对象不会消失——它们只是失去归属。",
      bullets: [
        "子部门将上提为顶级部门",
        "部门内的员工变为「未分配」",
        "员工的档案与记忆不受影响",
      ],
      confirmText: "删除部门",
    }))) return;
    try {
      const r = await api.post("/departments/batch-delete", { department_ids: [d.id], mode: "promote" });
      setMsg(r.deleted_count ? `✅ 已删除「${d.name}」` : "❌ 删除失败");
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
  }

  async function batchCreate() {
    const items = batchText.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [name, description] = l.split(/[,，]/).map((s) => s.trim());
      return { name, description: description || undefined };
    }).filter((i) => i.name);
    if (!items.length) return;
    try {
      const cs = await api.get<Company[]>("/companies");
      const r = await api.post("/departments/batch-create", {
        company_id: cs[0].id,
        parent_department_id: batchParent || undefined,
        departments: items,
      });
      setMsg(`✅ 批量创建 ${r.created_count} 个部门`);
      setShowBatchAdd(false);
      setBatchText("");
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
  }

  async function batchDelete() {
    if (!selected.size) return;
    setDeleting(true);
    try {
      const r = await api.post("/departments/batch-delete", {
        department_ids: Array.from(selected), mode: "promote",
      });
      setMsg(`✅ 已删除 ${r.deleted_count} 个部门${r.failed.length ? `，失败 ${r.failed.length}` : ""}`);
      setShowBatchDelete(false); setSelected(new Set());
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "操作失败")}`); }
    setDeleting(false);
  }

  /* ---------- 排序 / 置顶 ---------- */
  async function togglePin(d: Dept) {
    // 原来裸 fetch 且不看返回：置顶失败也会提示"已置顶"
    try {
      await api.put(`/departments/${d.id}/pin`, { pinned: !d.pinned });
      setMsg(d.pinned ? `「${d.name}」已取消置顶` : `📌 「${d.name}」已置顶`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "置顶失败")}`); }
  }

  const [orderDraft, setOrderDraft] = useState<string[] | null>(null);
  function startSort() {
    setOrderDraft(depts.map((d) => d.id));
    setSortMode(true);
  }
  function moveDept(id: string, dir: -1 | 1) {
    if (!orderDraft) return;
    const arr = [...orderDraft];
    const i = arr.indexOf(id);
    const j = i + dir;
    if (j < 0 || j >= arr.length) return;
    [arr[i], arr[j]] = [arr[j], arr[i]];
    setOrderDraft(arr);
  }
  async function saveSort() {
    if (!orderDraft) return;
    try {
      await api.post("/departments/reorder", { ordered_ids: orderDraft });
      setSortMode(false);
      setOrderDraft(null);
      setMsg("✅ 排序已保存");
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "保存排序失败")}`); }
  }

  const nameOf = (id: string | undefined) =>
    depts.find((d) => d.id === id)?.name || "";

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
        <div className="relative flex items-end justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">组织架构</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              部门层级 · 技能配置 · 专属知识库
            </p>
          </div>
          <div className="flex gap-2">
            <button onClick={() => setShowPresets(!showPresets)} className="btn-ghost">🏢 组织形态</button>
            <button onClick={sortMode ? saveSort : startSort}
              className={sortMode ? "btn-primary" : "btn-ghost"}>
              {sortMode ? "✓ 保存排序" : "⇅ 排序"}
            </button>
            <button onClick={() => setShowBatchDelete(true)} className="btn-ghost">🗑 批量删除</button>
            <button onClick={() => setShowBatchAdd(!showBatchAdd)} className="btn-ghost">⚡ 批量添加</button>
            <button onClick={() => openCreate(null)} className="btn-primary">＋ 新建部门</button>
          </div>
        </div>
        <div className="relative mt-5 flex gap-8">
          {[["部门总数", depts.length], ["在职员工", agents.length],
            ["专属技能配置", depts.reduce((s, d) => s + (d.skill_ids?.length || 0), 0)],
            ["知识库文档", depts.reduce((s, d) => s + (d.kb_document_ids?.length || 0), 0)]].map(([k, v]) => (
            <div key={k as string}>
              <div className="text-xl font-bold tabular-nums text-gradient">{v}</div>
              <div className="text-[10px] mt-0.5 tracking-wide" style={{ color: "var(--muted)" }}>{k}</div>
            </div>
          ))}
        </div>
      </div>

      {sortMode && (
        <p className="mb-3 text-xs text-cyan-300">
          排序模式：用 ↑↓ 调整部门顺序（自上而下即显示顺序），完成后点「✓ 保存排序」
        </p>
      )}
      {msg && <p className="mb-3 text-sm text-indigo-300">{msg}</p>}

      {/* V5.0 行业组织形态预设：选行业 → 预览部门树 → 一键引入（自动配专家） */}
      {showPresets && (
        <div className="card mb-5 space-y-3 animate-fade-up">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="font-medium text-sm">🏢 行业组织形态预设（一键引入）</h3>
              <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                选一个行业 → 自动建部门 + 配好专家的数字员工；已存在的自动跳过，可反复引入
              </p>
            </div>
            <button onClick={() => setShowPresets(false)} className="text-[var(--muted)] hover:text-white text-lg px-1">✕</button>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
            {presets.map((p) => (
              <button key={p.id} onClick={() => previewPreset(p)}
                className="text-left p-3 rounded-xl border transition-all hover:-translate-y-0.5"
                style={{ borderColor: "var(--border)", background: "rgba(255,255,255,0.02)" }}>
                <div className="text-2xl mb-1.5">
                  {p.emoji}
                  {p.id?.startsWith("pack_") && (
                    <span className="ml-1.5 align-middle text-[10px] px-1.5 py-0.5 rounded-full font-medium"
                      style={{ background: "rgba(99,102,241,0.15)", color: "#a5b4fc" }}>
                      岗位包
                    </span>
                  )}
                  {/* V5.6：大厂仿照包——一键生成对标大厂架构的数字组织 */}
                  {p.id?.startsWith("bigco_") && (
                    <span className="ml-1.5 align-middle text-[10px] px-1.5 py-0.5 rounded-full font-medium"
                      style={{ background: "rgba(245,158,11,0.15)", color: "#fbbf24" }}>
                      大厂仿
                    </span>
                  )}
                </div>
                <div className="text-sm font-semibold">{p.industry}</div>
                <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                  {p.department_count} 部门 · {p.agent_count} 岗位（配专家）
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 批量添加面板 */}
      {showBatchAdd && (
        <div className="card mb-5 space-y-2.5 animate-fade-up">
          <h3 className="font-medium text-sm">⚡ 批量添加部门</h3>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            每行一个：<code>部门名称, 职责描述</code>（描述可省略）
          </p>
          <textarea rows={4} value={batchText} onChange={(e) => setBatchText(e.target.value)}
            placeholder={"产品部, 负责产品规划\n运营部, 用户增长与活动"} className="input font-mono" />
          <select value={batchParent} onChange={(e) => setBatchParent(e.target.value)} className="input">
            <option value="">作为顶级部门（可选上级）</option>
            {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
          </select>
          <button onClick={batchCreate} disabled={!batchText.trim()}
            className="btn-primary disabled:opacity-40">批量创建</button>
        </div>
      )}

      {/* 部门列表：响应式网格 1~4 列 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-4 items-start">
        {(sortMode && orderDraft
          ? orderDraft.map((id) => depts.find((d) => d.id === id)!).filter(Boolean)
          : tree.roots
        ).map((d) => {
          const grad = gradOf(d.id);
          const staff = agentsOf(d.id);
          const children = tree.childrenMap[d.id] || [];
          const deptSkillNames = (d.skill_ids || [])
            .map((sid: string) => allSkills.find((sk) => sk.id === sid)?.name)
            .filter(Boolean) as string[];

          return (
            <div key={d.id} className="card group relative overflow-hidden cursor-pointer flex flex-col"
              onClick={() => !sortMode && openEdit(d)}>
              <div className="absolute top-0 left-0 w-full h-[2px]"
                style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />

              {d.pinned && (
                <span className="absolute top-3 right-3 text-[10px] px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300">
                  📌 已置顶
                </span>
              )}

              <div className="flex items-start gap-3.5">
                <div className="w-11 h-11 rounded-xl flex items-center justify-center text-lg shrink-0"
                  style={{
                    background: `linear-gradient(135deg, ${grad})`,
                    boxShadow: `0 4px 16px -6px ${grad.split(",")[0]}66`,
                  }}>
                  🏢
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 pr-14">
                    <span className="text-[15px] font-semibold truncate">{d.name}</span>
                    {sortMode && (
                      <span className="flex gap-1 shrink-0">
                        <button onClick={(e) => { e.stopPropagation(); moveDept(d.id, -1); }}
                          className="w-6 h-6 rounded-md hover:bg-white/[0.08] text-xs">↑</button>
                        <button onClick={(e) => { e.stopPropagation(); moveDept(d.id, 1); }}
                          className="w-6 h-6 rounded-md hover:bg-white/[0.08] text-xs">↓</button>
                      </span>
                    )}
                  </div>
                  <p className="text-xs mt-1 leading-relaxed line-clamp-2" style={{ color: "var(--muted)" }}>
                    {d.description || "点击卡片补充部门介绍与专属配置"}
                  </p>

                  <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
                    <span className="inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full font-medium"
                      style={{ background: "rgba(99,102,241,0.14)", color: "#a5b4fc" }}>
                      👥 {staff.length} 名员工
                    </span>
                    {children.length > 0 && (
                      <span className="inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full"
                        style={{ background: "rgba(34,211,238,0.10)", color: "#67e8f9" }}>
                        📂 {children.length} 子部门
                      </span>
                    )}
                    {(d.kb_document_ids?.length ?? 0) > 0 && (
                      <span className="inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full"
                        style={{ background: "rgba(245,158,11,0.12)", color: "#fcd34d" }}>
                        📚 {d.kb_document_ids!.length} 份文档
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* 部门掌握的技能 */}
              <div className="mt-3">
                <p className="text-[10px] font-semibold tracking-widest uppercase mb-1.5" style={{ color: "var(--muted)" }}>
                  ⚡ 掌握技能
                </p>
                {deptSkillNames.length ? (
                  <div className="flex flex-wrap gap-1">
                    {deptSkillNames.slice(0, 4).map((n) => (
                      <span key={n} className="text-[10px] px-2 py-0.5 rounded-md"
                        style={{ background: "rgba(16,185,129,0.12)", color: "#6ee7b7" }}>{n}</span>
                    ))}
                    {deptSkillNames.length > 4 && (
                      <span className="text-[10px] px-1.5 py-0.5" style={{ color: "var(--muted)" }}>
                        +{deptSkillNames.length - 4}
                      </span>
                    )}
                  </div>
                ) : (
                  <p className="text-[10px]" style={{ color: "var(--muted)" }}>尚未配置 · 编辑卡片可添加</p>
                )}
              </div>

              {/* 部门员工名单 */}
              <div className="mt-3">
                <p className="text-[10px] font-semibold tracking-widest uppercase mb-1.5" style={{ color: "var(--muted)" }}>
                  👥 部门员工
                </p>
                {staff.length ? (
                  <div className="space-y-1">
                    {staff.slice(0, 5).map((a, ai) => {
                      const g = DEPT_GRADS[(ai + 2) % DEPT_GRADS.length];
                      return (
                        <div key={a.id} className="flex items-center gap-2">
                          <div className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold text-white shrink-0"
                            style={{ background: `linear-gradient(135deg, ${g})` }}>
                            {a.name.slice(0, 1)}
                          </div>
                          <span className="text-xs truncate">{a.name}</span>
                          {a.role && (
                            <span className="text-[10px] truncate opacity-70" style={{ color: "var(--muted)" }}>
                              · {a.role}
                            </span>
                          )}
                        </div>
                      );
                    })}
                    {staff.length > 5 && (
                      <p className="text-[10px] pl-7" style={{ color: "var(--muted)" }}>
                        … 还有 {staff.length - 5} 人
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="text-[10px]" style={{ color: "var(--muted)" }}>暂无员工</p>
                )}
              </div>

              {/* 悬停操作 */}
              {!sortMode && (
                <div className="mt-auto pt-3 mt-3 border-t flex gap-2 opacity-0 group-hover:opacity-100 transition-all duration-200"
                  style={{ borderColor: "var(--border)" }}
                  onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => togglePin(d)}
                    className={`flex-1 text-[11px] py-1.5 rounded-lg transition-colors ${
                      d.pinned ? "text-amber-300 bg-amber-500/12" : "hover:bg-amber-500/12 text-[var(--muted)]"}`}
                    style={{ border: "1px solid rgba(251,191,36,0.25)" }}>
                    {d.pinned ? "取消置顶" : "📌 置顶"}
                  </button>
                  <button onClick={() => openCreate(d.id)}
                    className="flex-1 text-[11px] py-1.5 rounded-lg transition-colors hover:bg-indigo-500/15 text-indigo-300"
                    style={{ border: "1px solid rgba(99,102,241,0.25)" }}>
                    ＋ 子部门
                  </button>
                  <button onClick={() => deleteOne(d)}
                    className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                    style={{ border: "1px solid rgba(248,113,113,0.25)" }}>
                    删除
                  </button>
                </div>
              )}

              {/* 子部门小卡片（矮版） */}
              {children.length > 0 && !sortMode && (
                <div className="mt-3 pt-3 border-t" style={{ borderColor: "var(--border)" }}
                  onClick={(e) => e.stopPropagation()}>
                  <p className="text-[10px] font-semibold tracking-widest uppercase mb-2" style={{ color: "var(--muted)" }}>
                    子部门
                  </p>
                  <div className="grid grid-cols-2 gap-2">
                    {children.map((c) => {
                      const cg = gradOf(c.id);
                      return (
                        <button key={c.id} onClick={() => openEdit(c)}
                          className="relative overflow-hidden rounded-xl p-2.5 text-left transition-all border hover:border-indigo-500/40"
                          style={{ background: "var(--surface-2)", borderColor: "var(--border)" }}>
                          <span className="absolute top-0 left-0 w-full h-[2px]"
                            style={{ background: `linear-gradient(90deg, ${cg.split(",")[0]}, transparent)` }} />
                          <div className="flex items-center gap-2">
                            <div className="w-6 h-6 rounded-md flex items-center justify-center text-[10px] shrink-0"
                              style={{ background: `linear-gradient(135deg, ${cg})` }}>🏢</div>
                            <span className="text-xs font-medium truncate">{c.name}</span>
                            <span className="ml-auto text-[10px] shrink-0" style={{ color: "var(--muted)" }}>
                              {agentsOf(c.id).length}人
                            </span>
                          </div>
                          <p className="text-[10px] mt-1 line-clamp-1" style={{ color: "var(--muted)" }}>
                            {c.description || "点击编辑介绍"}
                          </p>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          );
        })}

        {!tree.roots.length && (
          <div className="card card-static text-center py-20 animate-fade-up col-span-full">
            <div className="text-4xl mb-3 opacity-40">🏗</div>
            <p className="text-sm" style={{ color: "var(--muted)" }}>
              还没有部门 · 点击右上角「新建部门」或「批量添加」开始搭建组织
            </p>
          </div>
        )}
      </div>

      {/* 批量删除弹窗 */}
      {showBatchDelete && mounted && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => { setShowBatchDelete(false); setSelected(new Set()); }}>
          <div className="w-full max-w-2xl rounded-2xl overflow-hidden animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(248,113,113,0.25)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="px-5 pt-5 pb-3 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <div>
                <h3 className="font-bold text-sm">🗑 批量删除部门</h3>
                <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  已选 {selected.size} 个 · 子部门上提为顶层，员工变为未分配
                </p>
              </div>
              <button onClick={() => { setShowBatchDelete(false); setSelected(new Set()); }}
                className="btn-ghost !px-2 !py-1 text-xs">✕</button>
            </div>
            <div className="px-5 pt-3 pb-1 flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
              <label className="flex items-center gap-2 text-xs cursor-pointer select-none" style={{ color: "var(--muted)" }}>
                <input type="checkbox"
                  checked={selected.size === depts.length && depts.length > 0}
                  onChange={() => setSelected(new Set(depts.map((x) => x.id)))}
                  className="accent-indigo-500 w-3.5 h-3.5" />
                全选
              </label>
              {selected.size > 0 && (
                <button onClick={() => () => setSelected(new Set())} className="text-[11px] hover:text-[var(--foreground)] transition-colors">清空</button>
              )}
            </div>
            <div className="max-h-[50vh] overflow-y-auto p-4 grid grid-cols-3 gap-2">
              {depts.map((d) => {
                const on = selected.has(d.id);
                return (
                  <button key={d.id} type="button"
                    onClick={() => setSelected((prev) => {
                      const n = new Set(prev); if (n.has(d.id)) n.delete(d.id); else n.add(d.id); return n;
                    })}
                    className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${
                      on ? "border-red-500/60 bg-red-500/10" : "hover:bg-white/[0.04]"}`}
                    style={on ? {} : { borderColor: "var(--border)" }}>
                    <div className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs shrink-0"
                      style={{ background: `linear-gradient(135deg, ${gradOf(d.id)})` }}>📁</div>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium truncate">{d.name}</div>
                      <div className="text-[10px]" style={{ color: "var(--muted)" }}>{agentsOf(d.id).length} 人</div>
                    </div>
                    {on && <span className="text-red-400 text-xs shrink-0">✓</span>}
                  </button>
                );
              })}
            </div>
            <div className="px-5 py-4 border-t flex items-center gap-3" style={{ borderColor: "var(--border)" }}>
              <button onClick={batchDelete} disabled={!selected.size || deleting}
                className="disabled:opacity-40 px-5 py-2 rounded-lg text-sm font-medium text-white"
                style={{ background: "linear-gradient(135deg,#ef4444,#f97316)" }}>
                {deleting ? "删除中…" : `确认删除 ${selected.size} 个部门`}
              </button>
              <button onClick={() => setSelected(new Set())} className="btn-ghost text-xs">清空选择</button>
              <p className="text-[11px] ml-auto" style={{ color: "var(--muted)" }}>⚠️ 删除不可恢复</p>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* 编辑弹窗 */}
      {mounted && editing && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setEditing(null)}>
          <div className="card w-full max-w-lg space-y-3 max-h-[90vh] overflow-y-auto"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between pb-2 border-b" style={{ borderColor: "var(--border)" }}>
              <h3 className="font-bold text-sm">
                {editing.id === "__new__" ? "📁 新建部门" : `✎ 编辑「${editing.name}」`}
              </h3>
              <button onClick={() => setEditing(null)} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
            </div>

            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>部门名称 *</label>
              <input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                placeholder="如：市场部" className="input" autoFocus />
            </div>

            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>上级部门</label>
              <select value={editForm.parent_department_id}
                onChange={(e) => setEditForm({ ...editForm, parent_department_id: e.target.value })}
                className="input">
                <option value="">（无 · 作为顶级部门）</option>
                {depts.filter((d) => d.id !== editing.id).map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="text-xs block mb-1" style={{ color: "var(--muted)" }}>部门介绍</label>
              <textarea rows={2} value={editForm.description}
                onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                placeholder="这个部门是干什么的？" className="input" />
            </div>

            {/* 📎 部门附件口 */}
            <div>
              <AttachmentPicker value={deptAttachments} onChange={setDeptAttachments}
                label="📎 挂参考材料（部门制度 / 规范 / 说明）" />
            </div>

            {/* 部门专属技能 */}
            <div>
              <label className="text-xs block mb-1.5" style={{ color: "var(--muted)" }}>
                ⚡ 部门专属技能（该部门员工可用的技能）
              </label>
              {allSkills.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {allSkills.map((sk) => {
                    const on = editForm.skills.has(sk.id);
                    return (
                      <button key={sk.id} type="button"
                        onClick={() => setEditForm((prev) => {
                          const n = new Set(prev.skills);
                          if (n.has(sk.id)) n.delete(sk.id); else n.add(sk.id);
                          return { ...prev, skills: n };
                        })}
                        className={`px-2.5 py-1 rounded-full text-xs transition-all border ${
                          on ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "hover:bg-white/[0.04]"}`}
                        style={on ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                        {on && "✓ "}{sk.name}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs" style={{ color: "var(--muted)" }}>暂无可选技能</p>
              )}
            </div>

            {/* 部门专属知识库 */}
            <div>
              <label className="text-xs block mb-1.5" style={{ color: "var(--muted)" }}>
                📚 部门专属知识库文档（从知识库中勾选）
              </label>
              {kbDocs.length ? (
                <div className="max-h-40 overflow-y-auto space-y-1 p-1 rounded-xl" style={{ background: "var(--surface-2)" }}>
                  {kbDocs.map((doc) => {
                    const on = editForm.kbDocs.has(doc.id);
                    return (
                      <button key={doc.id} type="button"
                        onClick={() => setEditForm((prev) => {
                          const n = new Set(prev.kbDocs);
                          if (n.has(doc.id)) n.delete(doc.id); else n.add(doc.id);
                          return { ...prev, kbDocs: n };
                        })}
                        className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left text-xs transition-colors ${
                          on ? "bg-amber-500/10 text-amber-200" : "hover:bg-white/[0.04]"}`}>
                        <span className={`shrink-0 w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${
                          on ? "bg-amber-500 border-amber-500 text-white" : ""}`}
                          style={!on ? { borderColor: "var(--border)" } : {}}>{on && "✓"}</span>
                        <span className="truncate flex-1">{doc.title}</span>
                        <span className="shrink-0 text-[10px]" style={{ color: "var(--muted)" }}>{doc.chunk_count} 块</span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  知识库还没有文档，先去「知识库」页上传
                </p>
              )}
              <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                新文档请到「资产与治理 → 知识库」上传后回来勾选
              </p>
            </div>

            <div className="flex gap-2 pt-2 border-t" style={{ borderColor: "var(--border)" }}>
              <button onClick={saveDept} disabled={!editForm.name.trim()}
                className="btn-primary disabled:opacity-40">
                {editing.id === "__new__" ? "创建" : "保存修改"}
              </button>
              {msg && editing?.id === "__new__" && (
                <span className="text-xs text-red-300 self-center">{msg}</span>
              )}
              {editing.id !== "__new__" && (
                <button onClick={async () => {
                  if (!(await confirmAction({
                    title: `确认删除部门「${editing.name}」？`,
                    description: "子部门将上提为顶级，部门内员工变为「未分配」，员工档案不受影响。",
                    confirmText: "删除部门",
                  }))) return;
                  await api.del(`/departments/${editing.id}`).catch((e) => setMsg(`❌ ${describeError(e, "删除失败")}`));
                  setEditing(null);
                  await load();
                }}
                  className="text-xs py-1.5 px-3 rounded-lg text-red-400/80 hover:text-red-300 hover:bg-red-400/10 transition-colors"
                  style={{ border: "1px solid rgba(248,113,113,0.3)" }}>
                  🗑 删除此部门
                </button>
              )}
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* V5.0 组织形态预设预览弹窗：部门可勾选 → dry_run 预览 → 一键引入 */}
      {presetDetail && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setPresetDetail(null)} />
          <div className="relative w-full max-w-3xl max-h-[90vh] bg-[var(--card)] backdrop-blur-xl border border-[var(--border)] rounded-2xl shadow-2xl overflow-y-auto p-6 animate-fade-up">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-bold">{presetDetail.emoji} {presetDetail.industry}</h2>
                <p className="text-xs text-[var(--muted)] mt-0.5">
                  勾选要引入的部门 → 一键引入：自动建部门 + 配好专家的数字员工
                </p>
              </div>
              <button onClick={() => setPresetDetail(null)} className="text-[var(--muted)] hover:text-white text-xl px-2">✕</button>
            </div>

            {presetDry && (
              <div className="mb-3 text-xs px-3 py-2 rounded-lg bg-cyan-500/10 border border-cyan-400/25 text-cyan-300">
                📊 {presetDry.note}（部门可增减，引入前先预览，已存在的自动跳过）
              </div>
            )}

            <div className="space-y-2.5">
              {presetDetail.departments.map((d) => {
                const on = presetChecked.has(d.name);
                return (
                  <div key={d.name} className="rounded-xl border p-3"
                    style={{ borderColor: on ? "rgba(99,102,241,0.4)" : "var(--border)", background: on ? "rgba(99,102,241,0.06)" : "transparent" }}>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={on}
                        onChange={() => setPresetChecked((prev) => {
                          const n = new Set(prev);
                          if (n.has(d.name)) n.delete(d.name); else n.add(d.name);
                          return n;
                        })}
                        className="accent-indigo-500" />
                      <span className="text-sm font-semibold">{d.name}</span>
                      <span className="text-[11px]" style={{ color: "var(--muted)" }}>{d.desc}</span>
                    </label>
                    {on && d.agents.length > 0 && (
                      <div className="mt-2 pl-5 flex flex-wrap gap-1.5">
                        {d.agents.map((a, i) => (
                          <span key={i} className="text-[11px] px-2 py-0.5 rounded-full bg-white/[0.04] border border-[var(--border)]">
                            👤 {a.name} <span className="text-[var(--muted)]">· {a.role}</span>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="flex items-center justify-end gap-2 mt-5">
              <button onClick={() => setPresetDetail(null)} className="btn-ghost text-sm">取消</button>
              <button onClick={applyPreset} disabled={presetApplying || presetChecked.size === 0}
                className="btn-primary text-sm disabled:opacity-40">
                {presetApplying ? "引入中…" : `📥 一键引入（${presetChecked.size} 部门）`}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
