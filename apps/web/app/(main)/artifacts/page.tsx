"use client";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import type { Company } from "@/lib/types";

/* ============================================================
   成果中心：成果版本管理与审核协同
   UI 对标设计体系（深空玻璃卡 / Portal 弹窗 / 渐变页头 / 徽章）
   ============================================================ */

type Artifact = { id: string; name: string; type: string; status: string; version: number };
type Version = { version: number; status: string; change_note?: string; content_preview?: string };

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#6366f1"];

const STATUS_ZH: Record<string, string> = {
  DRAFT: "草稿", IN_REVIEW: "审核中", APPROVED: "已批准", REJECTED: "已驳回",
};
const statusBadge = (s: string) => {
  const map: Record<string, string> = {
    DRAFT: "bg-white/[0.06] text-[var(--muted)]",
    IN_REVIEW: "bg-amber-500/15 text-amber-300",
    APPROVED: "bg-emerald-500/15 text-emerald-300",
    REJECTED: "bg-red-500/15 text-red-300",
  };
  return (
    <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${map[s] || "bg-white/[0.06] text-[var(--muted)]"}`}>
      {STATUS_ZH[s] || s}
    </span>
  );
};
const vStatusBadge = (s: string) => {
  const map: Record<string, string> = {
    DRAFT: "text-[var(--muted)]", IN_REVIEW: "text-amber-300",
    APPROVED: "text-emerald-300", REJECTED: "text-red-300",
  };
  return <span className={`text-[10px] ${map[s] || "text-[var(--muted)]"}`}>[{STATUS_ZH[s] || s}]</span>;
};

export default function ArtifactsPage() {
  const [mounted, setMounted] = useState(false);
  const [cid, setCid] = useState("");
  const [arts, setArts] = useState<Artifact[]>([]);
  const [detail, setDetail] = useState<{ id: string; name: string; status: string; versions: Version[] } | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [artAttach, setArtAttach] = useState<Attachment[]>([]);
  const [msg, setMsg] = useState("");

  useEffect(() => { setMounted(true); }, []);

  useEffect(() => {
    api.get<Company[]>("/companies").then((cs) => {
      const c = cs[0]?.id;
      if (c) {
        setCid(c);
        api.get(`/artifacts?company_id=${c}`).then(setArts);
      }
    }).catch(() => {});
  }, []);

  const reload = () => api.get(`/artifacts?company_id=${cid}`).then(setArts);

  const create = async () => {
    if (!name) return;
    const attachNote = artAttach.length ? `\n\n📎 附件：${artAttach.map((a) => a.name).join("、")}` : "";
    await api.post(`/artifacts?company_id=${cid}`, {
      name, content: (content || "") + attachNote, artifact_type: "document",
      attachments: artAttach.map((a) => a.id).filter(Boolean),
    });
    setName(""); setContent(""); setArtAttach([]); setShowCreate(false);
    setMsg("✅ 成果草稿已创建");
    reload();
  };

  const submitReview = async (aid: string) => {
    await api.post(`/artifacts/${aid}/submit`);
    setMsg("✅ 已提交审核");
    reload();
  };

  const review = async (aid: string, decision: string) => {
    await api.post(`/artifacts/${aid}/review`, { decision, comment: "" });
    setMsg(decision === "approve" ? "✅ 已批准" : "❌ 已驳回");
    openDetail(aid);
    reload();
  };

  // V5.5 C2：交付流水线三分离——APPROVED 后"发布"才离开内网
  const publish = async (aid: string) => {
    try {
      const r = await api.post<{ status?: string; message?: string; approval_id?: string }>(
        `/artifacts/${aid}/publish?company_id=${cid}`);
      setMsg(r.status === "pending_approval"
        ? `⏳ ${r.message || "已建发布审批单"}`
        : "✅ 已发布");
      reload();
    } catch (e: any) { setMsg(`❌ ${e.message || "发布失败"}`); }
  }

  // V5.6.1 ④ SOP 交接单：结构化 Markdown 交接文档（不靠聊天记录）
  const [sop, setSop] = useState<{ artifact_id: string; title: string; markdown: string } | null>(null);
  const showSop = async (aid: string) => {
    try {
      const r = await api.get<{ artifact_id: string; title: string; markdown: string }>(
        `/artifacts/${aid}/sop?company_id=${cid}`);
      setSop(r);
    } catch (e: any) { setMsg(`❌ ${e.message || "交接单生成失败"}`); }
  };

  const openDetail = async (aid: string) => {
    setDetail(await api.get(`/artifacts/${aid}?company_id=${cid}`));
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(16,185,129,0.12) 0%, rgba(34,211,238,0.08) 100%)", border: "1px solid rgba(16,185,129,0.25)" }}>
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>▣</div>
          <div>
            <h1 className="text-xl font-bold text-[var(--foreground)]">成果中心</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              成果版本管理 · 审核协同 · 全程留痕
            </p>
          </div>
          <button onClick={() => setShowCreate(!showCreate)}
            className="ml-auto btn-primary text-sm px-5 py-2.5">
            {showCreate ? "✕ 收起" : "＋ 新建成果"}
          </button>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(16,185,129,0.1)", color: "#6ee7b7", border: "1px solid rgba(16,185,129,0.25)" }}>
          {msg}
        </div>
      )}

      {/* 创建面板 */}
      {showCreate && (
        <div className="card card-static p-5 space-y-3 mb-6 animate-fade-up">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="成果名称"
            className="input" />
          <textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="内容…"
            className="input h-28 resize-none" />
          <AttachmentPicker value={artAttach} onChange={setArtAttach} label="添加成果附件" />
          <button onClick={create} disabled={!name} className="btn-primary disabled:opacity-40">
            创建草稿
          </button>
        </div>
      )}

      {/* 成果卡片列表 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {arts.map((a, i) => {
          const cg = AVATAR_COLORS[i % AVATAR_COLORS.length];
          return (
            <div key={a.id} className="card group relative overflow-hidden">
              <div className="absolute top-0 left-0 w-full h-[2px] opacity-60 group-hover:opacity-100 transition-opacity"
                style={{ background: `linear-gradient(90deg, ${cg.split(",")[0]}, transparent)` }} />
              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                  style={{ background: `linear-gradient(135deg, ${cg})` }}>📄</div>
                <div className="min-w-0 flex-1">
                  <button onClick={() => openDetail(a.id)}
                    className="font-medium text-sm truncate hover:text-indigo-300 transition-colors text-left w-full">
                    {a.name}
                  </button>
                  <div className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                    当前 v{a.version} · {a.type}
                  </div>
                </div>
                {statusBadge(a.status)}
              </div>
              <div className="mt-3 flex gap-1.5 flex-wrap">
                {a.status === "DRAFT" && (
                  <button onClick={() => submitReview(a.id)}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors">
                    提交审核
                  </button>
                )}
                {a.status === "IN_REVIEW" && (
                  <>
                    <button onClick={() => review(a.id, "approve")}
                      className="px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">✓ 批准</button>
                    <button onClick={() => review(a.id, "reject")}
                      className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">✕ 驳回</button>
                  </>
                )}
                {a.status === "APPROVED" && (
                  <>
                    <span className="text-[11px] px-2 py-1 rounded-lg bg-emerald-500/10 text-emerald-300">
                      ✓ 审核通过
                    </span>
                    <button onClick={() => publish(a.id)}
                      title="C2 三分离：审核通过 ≠ 已发布，点发布才离开内网"
                      className="px-3 py-1.5 rounded-lg text-xs font-medium bg-cyan-700 hover:bg-cyan-600 text-white transition-colors">
                      📤 发布
                    </button>
                  </>
                )}
                <button onClick={() => showSop(a.id)}
                  title="V5.6.1 ④ SOP 交接单：结构化 Markdown 交接文档"
                  className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 transition-colors">
                  📄 交接单
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {!arts.length && (
        <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
          暂无成果 · 点击右上角「＋ 新建成果」开始
        </div>
      )}

      {/* 版本历史弹窗 */}
      {mounted && detail && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setDetail(null)}>
          <div className="w-full max-w-2xl rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(16,185,129,0.35)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border)" }}>
              <div className="flex items-center gap-3">
                <div className="w-11 h-11 rounded-2xl flex items-center justify-center text-lg"
                  style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>📄</div>
                <div>
                  <h3 className="font-semibold text-base">{detail.name}</h3>
                  <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                    共 {detail.versions.length} 个版本
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {statusBadge(detail.status)}
                <button onClick={() => setDetail(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
              </div>
            </div>

            <div className="space-y-2 max-h-[60vh] overflow-y-auto pr-1">
              {[...detail.versions].reverse().map((v) => (
                <div key={v.version} className="rounded-xl p-3 space-y-1.5"
                  style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold text-indigo-300">v{v.version}</span>
                    {vStatusBadge(v.status)}
                    {v.change_note && (
                      <span className="text-xs truncate" style={{ color: "var(--muted)" }}>{v.change_note}</span>
                    )}
                  </div>
                  {v.content_preview && (
                    <pre className="text-xs whitespace-pre-wrap rounded-lg p-2.5 max-h-32 overflow-y-auto"
                      style={{ background: "rgba(255,255,255,0.03)", color: "var(--foreground)" }}>
                      {v.content_preview}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>, document.body
      )}

      {/* V5.6.1 ④ SOP 交接单弹窗 */}
      {mounted && sop && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setSop(null)}>
          <div className="w-full max-w-2xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(148,163,184,0.3)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border)" }}>
              <div>
                <h3 className="font-semibold text-base">📄 交接单 · {sop.title}</h3>
                <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                  V5.6.1 SOP 交接文档 —— 下一个 Agent / 老板可直接接手
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={async () => {
                    await navigator.clipboard.writeText(sop.markdown);
                    setMsg("✅ 交接单已复制到剪贴板");
                  }}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors">
                  ⧉ 复制
                </button>
                <button onClick={() => setSop(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
              </div>
            </div>
            <pre className="text-xs whitespace-pre-wrap rounded-lg p-3"
              style={{ background: "rgba(255,255,255,0.03)", color: "var(--foreground)" }}>
              {sop.markdown}
            </pre>
          </div>
        </div>, document.body
      )}
    </div>
  );
}
