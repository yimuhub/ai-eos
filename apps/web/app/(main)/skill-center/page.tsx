"use client";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";

/* ============================================================
   技能中心：技能获取（内置/自主沉淀/外部导入）+ 分类管理 + 安装
   ============================================================ */

type Skill = {
  id: string; slug: string; name: string; description?: string | null;
  version: string; scope: string; source: string; source_label?: string;
  company_id?: string | null;
};
type AgentRow = { id: string; name: string; role?: string | null };

/** Skill 市场里的一条结果（字段与后端 /skills/market/search 保持一致） */
type MarketItem = {
  name: string;
  description?: string;
  raw_url: string;
  /** 来源市场名（如 "skills.sh"），列表副标题用它 */
  source_market?: string;
  stars?: number;
};
/** 导入前的安全扫描结论：hits 命中的危险模式要原样告诉用户 */
type SecurityScan = { safe: boolean; level?: string; hits: string[] };

const SOURCE_META: Record<string, { icon: string; label: string; color: string }> = {
  builtin: { icon: "🧩", label: "系统内置", color: "#22d3ee" },
  custom: { icon: "🌱", label: "自主沉淀", color: "#34d399" },
  imported: { icon: "📥", label: "外部导入", color: "#f59e0b" },
};

export default function SkillCenterPage() {
  const cidRef = useActiveCompanyId();
  const [tab, setTab] = useState<"all" | "builtin" | "custom" | "imported">("all");
  // 创建表单
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [instruction, setInstruction] = useState("");
  const [skAttach, setSkAttach] = useState<Attachment[]>([]);
  const [scope, setScope] = useState("exclusive");
  // 导入表单
  const [showImport, setShowImport] = useState(false);
  const [impName, setImpName] = useState("");
  const [impDesc, setImpDesc] = useState("");
  const [impInstr, setImpInstr] = useState("");
  const [impScope, setImpScope] = useState("exclusive");
  const [impAttach, setImpAttach] = useState<Attachment[]>([]);
  // Skill 市场
  const [showMarket, setShowMarket] = useState(false);
  const [marketQ, setMarketQ] = useState("");
  const [marketResults, setMarketResults] = useState<MarketItem[]>([]);
  const [marketSearching, setMarketSearching] = useState(false);
  const [previewItem, setPreviewItem] = useState<MarketItem | null>(null);
  const [previewContent, setPreviewContent] = useState("");
  const [previewScan, setPreviewScan] = useState<SecurityScan | null>(null);
  const [mktScope, setMktScope] = useState("exclusive");
  // 安装目标
  const [installFor, setInstallFor] = useState<Skill | null>(null);
  const [pickAgent, setPickAgent] = useState("");
  const [msg, setMsg] = useState("");
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  /* ---------- 数据加载 ----------
   * 原实现是"先取公司拿 id → 再串行取技能 → 再取员工"，且 cid 是一次性快照：
   * 用户在顶栏切了公司，这一页还显示旧公司的技能。现在交给统一数据层。
   */
  const {
    data: bundle, error: loadError, refresh: load,
  } = useResource(
    cidRef ? `skills:${cidRef}` : null,
    async () => {
      const cid = cidRef as string;
      const [skills, agents] = await Promise.all([
        api.get<Skill[]>(`/skills?company_id=${cid}`),
        api.get<AgentRow[]>(`/agents?company_id=${cid}`),
      ]);
      return { skills, agents };
    },
  );
  const skills = useMemo(() => bundle?.skills ?? [], [bundle]);
  const agents = useMemo(() => bundle?.agents ?? [], [bundle]);
  /** 页面其余部分沿用字符串形式的 cid；未选公司时是空串 */
  const cid = cidRef ?? "";

  const filtered = skills.filter((s) =>
    tab === "all" ? true : s.source === tab ||
      (tab === "imported" && s.source === "custom" && false));

  async function createSkill() {
    if (!name.trim() || !cid) return;
    try {
      await api.post(`/skills/custom?company_id=${cid}`,
        { name, description: desc, scope, instruction });
      setShowCreate(false); setName(""); setDesc(""); setInstruction("");
      setMsg("✅ 技能已沉淀入库");
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "创建技能失败")}`); }
  }

  async function importSkill() {
    if (!impName.trim() || !cid) return;
    try {
      const attachNote = impAttach.length ? `\n\n📎 附件：${impAttach.map((a) => a.name).join("、")}` : "";
      await api.post(`/skills/import?company_id=${cid}`, {
        name: impName, description: impDesc,
        instruction: (impInstr || "") + attachNote,
        scope: impScope,
        attachment_ids: impAttach.map((a) => a.id).filter(Boolean),
      });
      setShowImport(false); setImpName(""); setImpDesc(""); setImpInstr(""); setImpAttach([]);
      setMsg("✅ 技能已导入");
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "导入技能失败")}`); }
  }

  /* ---------- Skill 市场 ---------- */
  async function searchMarket(q = "") {
    setMarketSearching(true);
    try {
      const d = await api.get<{ results?: MarketItem[] }>(
        `/skills/market/search?q=${encodeURIComponent(q)}`);
      setMarketResults(d.results || []);
    } catch (e) {
      setMarketResults([]);
      setMsg(`❌ 市场检索失败：${describeError(e, "检索失败")}`);
    }
    setMarketSearching(false);
  }

  async function previewMarket(item: MarketItem) {
    setPreviewItem(item); setPreviewContent(""); setPreviewScan(null);
    try {
      const d = await api.post<{ content?: string; security_scan?: SecurityScan }>(
        "/skills/market/fetch-preview", { raw_url: item.raw_url });
      setPreviewContent(d.content || "");
      setPreviewScan(d.security_scan || null);
    } catch (e) {
      // 原来只写"拉取失败"，不告诉用户是网络问题还是地址失效
      setPreviewContent(`拉取失败：${describeError(e, "拉取失败")}`);
    }
  }

  async function installFromMarket() {
    if (!previewItem) return;
    try {
      const d = await api.post<{ security_scan?: SecurityScan }>(
        `/skills/market/install-from-market?company_id=${cid}`, {
        name: previewItem.name, raw_url: previewItem.raw_url,
        scope: mktScope,
        force: !!(previewScan && !previewScan.safe),
      });
      setShowImport(false); setPreviewItem(null);
      setMsg(`✅ 已从市场导入「${previewItem.name}」` + (d.security_scan?.safe ? "" : ` ⚠️ 含风险内容已标记：${(d.security_scan?.hits || []).join("、")}`));
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "导入失败")}`); }
  }

  async function toggleScope(sk: Skill) {
    if (!cid) return;
    try {
      // 目标 scope 先算成常量再拼：模板里嵌三元会让契约扫描读不懂这段路径
      const nextScope = sk.scope === "public" ? "exclusive" : "public";
      await api.patch(`/skills/${sk.id}/scope?company_id=${cid}&scope=${nextScope}`);
      setMsg(`✅ 「${sk.name}」已设为${nextScope === "public" ? "公共共享" : "私有专属"}技能`);
      await load();
    } catch (e) { setMsg(`❌ ${describeError(e, "修改可见范围失败")}`); }
  }

  async function install(skill: Skill, agentId: string) {
    if (!agentId) return;
    try {
      await api.post(`/skills/${skill.slug}/install`, { agent_id: agentId });
      setMsg(`✅ 「${skill.name}」已安装给该员工`);
      setInstallFor(null);
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{ background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)", border: "1px solid rgba(99,102,241,0.22)" }}>
        <h1 className="text-2xl font-bold tracking-tight">⚡ 技能中心</h1>
        <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
          技能获取三途径：系统内置 · 长期经验自主沉淀 · 外部 skill 导入。支持私有/公共自由切换。
        </p>
        <div className="flex gap-2 mt-4">
          <button onClick={() => { setShowCreate(!showCreate); setShowImport(false); }}
            className="btn-primary !py-2 !px-4 text-xs">🌱 沉淀新技能</button>
          <button onClick={() => { setShowImport(!showImport); setShowCreate(false); }}
            className="btn-ghost text-xs !px-4 !py-2"
            style={{ border: "1px solid rgba(99,102,241,0.35)" }}>📥 导入外部 skill</button>
          <button onClick={() => { setShowMarket(!showMarket); setShowCreate(false); setShowImport(false); if (!showMarket) searchMarket(""); }}
            className="btn-ghost text-xs !px-4 !py-2"
            style={{ border: "1px solid rgba(34,211,238,0.4)", color: "#67e8f9" }}>🛒 Skill 市场</button>
        </div>
      </div>

      {msg && <p className="mb-3 text-xs text-indigo-300">{msg}</p>}

      {loadError && (
        <div className="mb-3 flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}>
          <span>🔌</span>
          <span className="flex-1">加载技能列表失败：{loadError}</span>
          <button onClick={() => load()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

      {/* 创建表单 */}
      {showCreate && (
        <div className="card p-4 space-y-2 mb-5 animate-fade-up">
          <h3 className="font-semibold text-sm">🌱 沉淀新技能（把长期总结的方法论固化为可复用技能）</h3>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="技能名称（如：爆款文案方法论）"
            className="input w-full !py-2 text-xs" />
          <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="一句话简介"
            className="input w-full !py-2 text-xs" />
          <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} rows={5}
            placeholder="技能方法论 / 执行步骤（这是技能的核心：把你的经验写成分步指引，Agent 执行时按此操作）…"
            className="input w-full !py-2 text-xs resize-none" />
          <div className="flex items-center gap-3">
            <label className="text-xs flex items-center gap-1.5 cursor-pointer">
              <input type="radio" checked={scope === "exclusive"} onChange={() => setScope("exclusive")} />
              🔒 私有专属（仅本公司）
            </label>
            <label className="text-xs flex items-center gap-1.5 cursor-pointer">
              <input type="radio" checked={scope === "public"} onChange={() => setScope("public")} />
              🌐 公共共享（全员可学）
            </label>
            <button onClick={createSkill} disabled={!name.trim()}
              className="btn-primary !py-1.5 !px-4 text-xs ml-auto disabled:opacity-40">入库</button>
          </div>
        </div>
      )}

      {/* 导入表单 */}
      {showImport && (
        <div className="card p-4 space-y-2 mb-5 animate-fade-up">
          <h3 className="font-semibold text-sm">📥 导入外部 skill</h3>
          <input value={impName} onChange={(e) => setImpName(e.target.value)} placeholder="技能名称"
            className="input w-full !py-2 text-xs" />
          <input value={impDesc} onChange={(e) => setImpDesc(e.target.value)} placeholder="简介"
            className="input w-full !py-2 text-xs" />
          <textarea value={impInstr} onChange={(e) => setImpInstr(e.target.value)} rows={4}
            placeholder="粘贴 skill 定义内容（方法论/步骤）…"
            className="input w-full !py-2 text-xs resize-none" />
          <AttachmentPicker value={impAttach} onChange={setImpAttach} label="添加 skill 相关文件" />
          <div className="flex items-center gap-3 flex-wrap">
            <label className="text-xs flex items-center gap-1.5 cursor-pointer">
              <input type="radio" checked={impScope === "exclusive"} onChange={() => setImpScope("exclusive")} />
              🔒 私有专属（仅本公司）
            </label>
            <label className="text-xs flex items-center gap-1.5 cursor-pointer">
              <input type="radio" checked={impScope === "public"} onChange={() => setImpScope("public")} />
              🌐 公共共享（全员可学）
            </label>
            <button onClick={importSkill} disabled={!impName.trim()}
              className="btn-primary !py-1.5 !px-4 text-xs ml-auto disabled:opacity-40">导入</button>
          </div>
        </div>
      )}

      {/* Skill 市场 */}
      {showMarket && (
        <div className="card p-4 space-y-3 mb-5 animate-fade-up">
          <h3 className="font-semibold text-sm">🛒 Skill 市场 <span className="text-[10px] font-normal" style={{ color: "var(--muted)" }}>搜索公开技能仓库 · 导入前自动安全审核</span></h3>
          <div className="flex gap-2">
            <input value={marketQ} onChange={(e) => setMarketQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && searchMarket(marketQ)}
              placeholder="搜索技能名，如：pdf / excel / writing…"
              className="input flex-1 !py-2 text-xs" />
            <button onClick={() => searchMarket(marketQ)} disabled={marketSearching}
              className="btn-primary !py-2 !px-5 text-xs disabled:opacity-40">
              {marketSearching ? "搜索中…" : "🔍 搜索"}
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5 max-h-[420px] overflow-y-auto pr-1">
            {marketResults.map((m, i) => (
              <div key={i} className="rounded-xl p-3 space-y-2"
                style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                <div className="flex items-start justify-between gap-2">
                  <p className="text-sm font-medium truncate">{m.name}</p>
                  <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-cyan-500/15 text-cyan-300 shrink-0">市场</span>
                </div>
                <p className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{m.source_market}</p>
                <button onClick={() => previewMarket(m)}
                  className="btn-primary !py-1 !px-3 text-[11px] w-full">👁 预览并导入</button>
              </div>
            ))}
            {!marketResults.length && !marketSearching && (
              <p className="text-xs py-6 col-span-full text-center" style={{ color: "var(--muted)" }}>
                输入关键词搜索公开 skill 市场
              </p>
            )}
          </div>
        </div>
      )}

      {/* 来源筛选 */}
      <div className="flex gap-2 mb-4">
        {([["all", "全部"], ["builtin", "🧩 系统内置"], ["custom", "🌱 自主沉淀"], ["imported", "📥 外部导入"]] as const).map(([v, label]) => (
          <button key={v} onClick={() => setTab(v)}
            className={`px-3.5 py-1.5 rounded-full text-xs transition-all border ${tab === v ? "bg-indigo-600 text-white border-indigo-600" : ""}`}
            style={tab === v ? {} : { borderColor: "rgba(99,102,241,0.25)", color: "var(--muted)" }}>
            {label}
          </button>
        ))}
      </div>

      {/* 技能卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {filtered.map((sk) => {
          const meta = SOURCE_META[sk.source] || SOURCE_META.builtin;
          return (
            <div key={sk.id} className="card p-4 space-y-2.5">
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2.5">
                  <span className="w-9 h-9 rounded-xl flex items-center justify-center text-lg shrink-0"
                    style={{ background: `${meta.color}22` }}>{meta.icon}</span>
                  <div>
                    <p className="font-semibold text-sm leading-tight">{sk.name}</p>
                    <p className="text-[10px]" style={{ color: meta.color }}>{meta.label} · v{sk.version}</p>
                  </div>
                </div>
                <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${sk.scope === "public"
                  ? "bg-cyan-500/15 text-cyan-300" : "bg-pink-500/15 text-pink-300"}`}>
                  {sk.scope === "public" ? "🌐 公共" : "🔒 私有"}
                </span>
              </div>
              <p className="text-xs line-clamp-2 min-h-[32px]" style={{ color: "var(--muted)" }}>
                {sk.description || "（无描述）"}
              </p>
              <div className="flex gap-1.5 pt-1">
                <button onClick={() => { setInstallFor(sk); setPickAgent(""); }}
                  className="btn-primary !py-1.5 !px-3 text-[11px] flex-1">安装给员工</button>
                {(sk.source !== "builtin") && (
                  <button onClick={() => toggleScope(sk)}
                    className="btn-ghost text-[11px] !px-3 !py-1.5"
                    style={{ border: "1px solid rgba(99,102,241,0.25)" }}>
                    设为{sk.scope === "public" ? "私有" : "公共"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {!filtered.length && (
        <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
          该分类暂无技能
        </div>
      )}

      {/* 市场技能预览/导入弹窗 */}
      {mounted && previewItem && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setPreviewItem(null)}>
          <div className="w-full max-w-xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: previewScan && !previewScan.safe ? "1px solid rgba(239,68,68,0.5)" : "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 pb-2 border-b" style={{ borderColor: "var(--border)" }}>
              <h3 className="font-semibold text-sm flex-1">📥 导入「{previewItem.name}」</h3>
              <button onClick={() => setPreviewItem(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
            </div>
            {/* 安全审核结果 */}
            {previewScan ? (
              previewScan.safe ? (
                <p className="text-xs px-3 py-2 rounded-lg bg-emerald-500/10 text-emerald-300">
                  ✅ 安全审核通过 · 未检测到危险内容
                </p>
              ) : (
                <div className="text-xs px-3 py-2 rounded-lg bg-red-500/10 text-red-300 space-y-1">
                  <p>⚠️ 安全审核<b>未通过</b>（风险等级：{previewScan.level}）</p>
                  <p>命中危险模式：{previewScan.hits.join("、")}</p>
                  <p className="opacity-70">确认导入后技能会带风险标记，建议人工复核内容</p>
                </div>
              )
            ) : (
              <p className="text-xs" style={{ color: "var(--muted)" }}>正在安全审核…</p>
            )}
            <pre className="text-[11px] whitespace-pre-wrap rounded-xl p-3 max-h-64 overflow-y-auto"
              style={{ background: "rgba(255,255,255,0.03)", color: "var(--foreground)", border: "1px solid var(--border)" }}>
              {previewContent || "加载中…"}
            </pre>
            <div className="flex items-center gap-3 flex-wrap">
              <label className="text-xs flex items-center gap-1.5 cursor-pointer">
                <input type="radio" checked={mktScope === "exclusive"} onChange={() => setMktScope("exclusive")} />
                🔒 私有专属
              </label>
              <label className="text-xs flex items-center gap-1.5 cursor-pointer">
                <input type="radio" checked={mktScope === "public"} onChange={() => setMktScope("public")} />
                🌐 公共共享
              </label>
              <div className="flex gap-2 ml-auto">
                <button onClick={() => setPreviewItem(null)} className="px-4 py-1.5 rounded-xl text-xs hover:bg-white/[0.06]"
                  style={{ border: "1px solid var(--border)" }}>取消</button>
                <button onClick={installFromMarket} disabled={!previewContent}
                  className={`btn-primary text-xs disabled:opacity-40 ${previewScan && !previewScan.safe ? "!from-red-600 !to-orange-500" : ""}`}>
                  {previewScan && !previewScan.safe ? "⚠️ 仍要导入（强制）" : "确认导入"}
                </button>
              </div>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 安装弹窗 */}
      {installFor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setInstallFor(null)}>
          <div className="w-full max-w-md rounded-2xl p-5 space-y-3 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-sm">安装「{installFor.name}」给员工</h3>
            <select value={pickAgent} onChange={(e) => setPickAgent(e.target.value)}
              className="input w-full !py-2 text-xs">
              <option value="">— 选择员工 —</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}{a.role ? ` · ${a.role}` : ""}</option>
              ))}
            </select>
            <div className="flex gap-2 justify-end pt-1">
              <button onClick={() => setInstallFor(null)} className="btn-ghost text-xs">取消</button>
              <button onClick={() => install(installFor, pickAgent)} disabled={!pickAgent}
                className="btn-primary text-xs disabled:opacity-40">确认安装</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
