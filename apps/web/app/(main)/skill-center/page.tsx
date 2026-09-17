"use client";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
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

const SOURCE_META: Record<string, { icon: string; label: string; color: string }> = {
  builtin: { icon: "🧩", label: "系统内置", color: "#22d3ee" },
  custom: { icon: "🌱", label: "自主沉淀", color: "#34d399" },
  imported: { icon: "📥", label: "外部导入", color: "#f59e0b" },
};

export default function SkillCenterPage() {
  const [cid, setCid] = useState("");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
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
  const [marketResults, setMarketResults] = useState<any[]>([]);
  const [marketSearching, setMarketSearching] = useState(false);
  const [previewItem, setPreviewItem] = useState<any | null>(null);
  const [previewContent, setPreviewContent] = useState("");
  const [previewScan, setPreviewScan] = useState<any | null>(null);
  const [mktScope, setMktScope] = useState("exclusive");
  // 安装目标
  const [installFor, setInstallFor] = useState<Skill | null>(null);
  const [pickAgent, setPickAgent] = useState("");
  const [msg, setMsg] = useState("");
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    setSkills(await api.get(`/skills?company_id=${cs[0].id}`));
    setAgents(await api.get(`/agents?company_id=${cs[0].id}`));
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = skills.filter((s) =>
    tab === "all" ? true : s.source === tab ||
      (tab === "imported" && s.source === "custom" && false));

  async function createSkill() {
    if (!name.trim() || !cid) return;
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/custom?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ name, description: desc, scope, instruction }),
      });
      setShowCreate(false); setName(""); setDesc(""); setInstruction("");
      setMsg("✅ 技能已沉淀入库");
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  async function importSkill() {
    if (!impName.trim() || !cid) return;
    try {
      const attachNote = impAttach.length ? `\n\n📎 附件：${impAttach.map((a) => a.name).join("、")}` : "";
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/import?company_id=${cid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({
          name: impName, description: impDesc,
          instruction: (impInstr || "") + attachNote,
          scope: impScope,
          attachment_ids: impAttach.map((a) => a.id).filter(Boolean),
        }),
      });
      setShowImport(false); setImpName(""); setImpDesc(""); setImpInstr(""); setImpAttach([]);
      setMsg("✅ 技能已导入");
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  /* ---------- Skill 市场 ---------- */
  async function searchMarket(q = "") {
    setMarketSearching(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/market/search?q=${encodeURIComponent(q)}`);
      const d = await r.json();
      setMarketResults(d.results || []);
    } catch { setMarketResults([]); }
    setMarketSearching(false);
  }

  async function previewMarket(item: any) {
    setPreviewItem(item); setPreviewContent(""); setPreviewScan(null);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/market/fetch-preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_url: item.raw_url }),
      });
      const d = await r.json();
      setPreviewContent(d.content || "");
      setPreviewScan(d.security_scan || null);
    } catch { setPreviewContent("拉取失败"); }
  }

  async function installFromMarket() {
    if (!previewItem) return;
    try {
      const cs: any[] = await api.get("/companies");
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/market/install-from-market?company_id=${cs[0].id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({
          name: previewItem.name, raw_url: previewItem.raw_url,
          scope: mktScope,
          force: !!(previewScan && !previewScan.safe),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error((d.detail?.message) || d.detail || `HTTP ${r.status}`);
      setShowImport(false); setPreviewItem(null);
      setMsg(`✅ 已从市场导入「${previewItem.name}」` + (d.security_scan?.safe ? "" : ` ⚠️ 含风险内容已标记：${(d.security_scan?.hits || []).join("、")}`));
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
  }

  async function toggleScope(sk: Skill) {
    if (!cid) return;
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/skills/${sk.id}/scope?company_id=${cid}&scope=${sk.scope === "public" ? "exclusive" : "public"}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
    });
    setMsg(`✅ 「${sk.name}」已设为${sk.scope === "public" ? "私有专属" : "公共共享"}技能`);
    await load();
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
