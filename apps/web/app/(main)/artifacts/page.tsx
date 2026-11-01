"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { timeAgo } from "@/lib/workspace";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";
import { notify } from "@/components/Toast";

/* ============================================================
   成果中心：成果版本管理与审核协同
   UI 对标设计体系（深空玻璃卡 / Portal 弹窗 / 渐变页头 / 徽章）
   B2：交付验收 + 交付意见（评论）
   ============================================================ */

type Artifact = {
  id: string; name: string; type: string; status: string; version: number;
  accepted_at?: string | null;
};
type Version = { version: number; status: string; change_note?: string; content_preview?: string };
type Detail = {
  id: string; name: string; status: string; versions: Version[];
  task_id?: string | null; accepted_at?: string | null; accepted_by?: string | null;
  comment_count?: number;
};
type DeliverableComment = {
  id: string; author_type: string; author_id: string; body: string;
  created_at?: string | null;
};

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#6366f1"];

const STATUS_ZH: Record<string, string> = {
  DRAFT: "草稿", IN_REVIEW: "审核中", APPROVED: "已批准", REJECTED: "已驳回",
};
const AUTHOR_ZH: Record<string, string> = {
  user: "老板", agent: "员工", system: "系统",
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
const acceptedBadge = (
  <span className="text-[10px] px-2 py-0.5 rounded-full shrink-0 bg-emerald-500/20 text-emerald-200">
    ✓ 已验收
  </span>
);

export default function ArtifactsPage() {
  const [mounted, setMounted] = useState(false);
  const cid = useActiveCompanyId();
  // 成果列表交给统一数据层：缓存首屏、并发去重、切公司自动跟随
  // bundle 带 cid：新公司的数据回来之前，不继续展示旧公司的成果
  const { data, refresh } = useResource(
    cid ? `artifacts:${cid}` : null,
    async () => {
      const rows = await api.get<Artifact[]>(`/artifacts?company_id=${cid}`);
      return { cid: cid as string, rows: Array.isArray(rows) ? rows : [] };
    },
  );
  const arts = useMemo<Artifact[]>(
    () => (data && data.cid === cid ? data.rows : []),
    [data, cid],
  );
  const [detail, setDetail] = useState<Detail | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [artAttach, setArtAttach] = useState<Attachment[]>([]);
  const [msg, setMsg] = useState("");

  // 交付意见
  const [comments, setComments] = useState<DeliverableComment[]>([]);
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [commentText, setCommentText] = useState("");
  const [commentBusy, setCommentBusy] = useState(false);
  // 退回修改：在详情弹窗内填意见
  const [declineOpen, setDeclineOpen] = useState(false);
  const [declineText, setDeclineText] = useState("");
  const [declineBusy, setDeclineBusy] = useState(false);
  // 当前登录用户（决定"删除自己的评论"按钮是否出现）
  const [meId, setMeId] = useState("");
  const openedFromUrl = useRef(false);

  useEffect(() => { setMounted(true); }, []);

  useEffect(() => {
    let alive = true;
    api.get<{ id: string }>("/auth/me")
      .then((u) => { if (alive) setMeId(u?.id || ""); })
      .catch(() => { /* 拿不到就当未知：仍可删自己的（后端会校验） */ });
    return () => { alive = false; };
  }, []);

  // 换公司时关掉旧公司的详情，避免继续看/操作上一家公司的成果
  useEffect(() => { setDetail(null); }, [cid]);

  const reload = () => refresh();

  const loadComments = useCallback(async (aid: string) => {
    setCommentsLoading(true);
    try {
      const rows = await api.get<DeliverableComment[]>(`/artifacts/${aid}/comments`);
      setComments(Array.isArray(rows) ? rows : []);
    } catch (e) {
      setComments([]);
      setMsg(`❌ ${describeError(e, "交付意见加载失败")}`);
    }
    setCommentsLoading(false);
  }, []);

  const openDetail = useCallback(async (aid: string, opts?: { decline?: boolean }) => {
    try {
      const d = await api.get<Detail>(`/artifacts/${aid}`);
      setDetail(d);
      setDeclineOpen(!!opts?.decline);
      setDeclineText("");
      setCommentText("");
      void loadComments(aid);
    } catch (e) {
      setMsg(`❌ ${describeError(e, "成果详情加载失败")}`);
    }
  }, [loadComments]);

  // 收件箱「去验收」/ 交付单直达：/artifacts?open=<artifact_id> 自动打开详情
  useEffect(() => {
    if (openedFromUrl.current || typeof window === "undefined") return;
    const aid = new URLSearchParams(window.location.search).get("open");
    if (!aid) return;
    openedFromUrl.current = true;
    void openDetail(aid);
  }, [openDetail]);

  const create = async () => {
    if (!name || !cid) return;
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

  // B2 验收：老板确认收到交付物（幂等）
  const accept = async (aid: string) => {
    try {
      const r = await api.post<{ already?: boolean }>(`/artifacts/${aid}/accept`);
      const text = r?.already ? "该成果此前已验收" : "已验收，交付完成";
      setMsg(`✅ ${text}`);
      notify("success", text);
      reload();
      if (detail?.id === aid) void openDetail(aid);
    } catch (e) {
      const text = describeError(e, "验收失败");
      setMsg(`❌ ${text}`);
      notify("error", text);
    }
  };

  // B2 验收退回：必须写清原因（1-2000 字），返工的人才知道改什么
  const decline = async (aid: string) => {
    const text = declineText.trim();
    if (text.length < 1 || text.length > 2000) {
      setMsg("❌ 退回意见需为 1-2000 字");
      notify("error", "退回意见需为 1-2000 字");
      return;
    }
    setDeclineBusy(true);
    try {
      await api.post(`/artifacts/${aid}/decline`, { comment: text });
      setMsg("↩️ 已退回修改");
      notify("success", "已退回修改，等待返工后重新提交");
      setDeclineOpen(false);
      setDeclineText("");
      reload();
      void openDetail(aid);
    } catch (e) {
      const m = describeError(e, "退回失败");
      setMsg(`❌ ${m}`);
      notify("error", m);
    }
    setDeclineBusy(false);
  };

  const postComment = async (aid: string) => {
    const body = commentText.trim();
    if (!body) return;
    setCommentBusy(true);
    try {
      await api.post(`/artifacts/${aid}/comments`, { body });
      setCommentText("");
      setMsg("✅ 交付意见已发表");
      notify("success", "交付意见已发表");
      await loadComments(aid);
      reload();
    } catch (e) {
      const m = describeError(e, "发表失败");
      setMsg(`❌ ${m}`);
      notify("error", m);
    }
    setCommentBusy(false);
  };

  const removeComment = async (aid: string, commentId: string) => {
    try {
      await api.del(`/artifacts/${aid}/comments/${commentId}`);
      setMsg("🗑 评论已删除");
      notify("success", "评论已删除");
      await loadComments(aid);
      reload();
    } catch (e) {
      const m = describeError(e, "删除评论失败");
      setMsg(`❌ ${m}`);
      notify("error", m);
    }
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

  const canAccept = (a: { status: string; accepted_at?: string | null }) =>
    a.status === "APPROVED" && !a.accepted_at;

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
              成果版本管理 · 审核协同 · 验收留痕
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
                <div className="flex flex-col items-end gap-1 shrink-0">
                  {statusBadge(a.status)}
                  {a.accepted_at && acceptedBadge}
                </div>
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
                    {canAccept(a) ? (
                      <>
                        <button onClick={() => accept(a.id)}
                          className="px-2.5 py-1.5 rounded-lg text-[11px] font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">
                          ✓ 验收通过
                        </button>
                        <button onClick={() => openDetail(a.id, { decline: true })}
                          className="px-2.5 py-1.5 rounded-lg text-[11px] font-medium bg-amber-700 hover:bg-amber-600 text-white transition-colors">
                          ↩ 退回修改
                        </button>
                      </>
                    ) : (
                      <span className="text-[11px] px-2 py-1 rounded-lg bg-emerald-500/10 text-emerald-300">
                        ✓ 审核通过
                      </span>
                    )}
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

      {/* 版本历史 / 验收 / 交付意见弹窗 */}
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
                    {typeof detail.comment_count === "number" ? ` · ${detail.comment_count} 条交付意见` : ""}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {detail.accepted_at && acceptedBadge}
                {statusBadge(detail.status)}
                <button onClick={() => setDetail(null)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
              </div>
            </div>

            {/* B2 验收区 */}
            <div className="rounded-xl p-3 space-y-2"
              style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold">交付验收</span>
                {detail.accepted_at ? (
                  <span className="text-[11px] text-emerald-300">
                    已于 {timeAgo(detail.accepted_at)}验收
                  </span>
                ) : canAccept(detail) ? (
                  <>
                    <button onClick={() => accept(detail.id)}
                      className="px-2.5 py-1 rounded-lg text-[11px] font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">
                      ✓ 验收通过
                    </button>
                    <button onClick={() => setDeclineOpen((v) => !v)}
                      className="px-2.5 py-1 rounded-lg text-[11px] font-medium bg-amber-700 hover:bg-amber-600 text-white transition-colors">
                      ↩ 退回修改
                    </button>
                  </>
                ) : (
                  <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                    审核通过（APPROVED）后可验收
                  </span>
                )}
              </div>
              {declineOpen && !detail.accepted_at && (
                <div className="space-y-2">
                  <textarea rows={3} maxLength={2000} value={declineText}
                    onChange={(e) => setDeclineText(e.target.value)}
                    placeholder="退回意见（1-2000 字）：哪里不对、期望改成什么样…"
                    className="input resize-none text-xs w-full" />
                  <div className="flex items-center gap-2 justify-end">
                    <span className="text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>
                      {declineText.trim().length}/2000
                    </span>
                    <button onClick={() => setDeclineOpen(false)}
                      className="px-3 py-1 rounded-lg text-[11px] hover:bg-white/[0.06]"
                      style={{ border: "1px solid var(--border)" }}>取消</button>
                    <button onClick={() => decline(detail.id)}
                      disabled={!declineText.trim() || declineBusy}
                      className="px-3 py-1 rounded-lg text-[11px] font-medium bg-red-600 hover:bg-red-500 text-white disabled:opacity-40">
                      {declineBusy ? "提交中…" : "确认退回"}
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-2 max-h-[40vh] overflow-y-auto pr-1">
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

            {/* B2 交付意见 */}
            <div className="rounded-xl p-3 space-y-2"
              style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold">
                  交付意见{comments.length ? `（${comments.length}）` : ""}
                </span>
                {commentsLoading && (
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>加载中…</span>
                )}
              </div>
              <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
                {comments.map((c) => (
                  <div key={c.id} className="rounded-lg px-2.5 py-2"
                    style={{ background: "rgba(255,255,255,0.03)" }}>
                    <div className="flex items-center gap-2 text-[10px]" style={{ color: "var(--muted)" }}>
                      <span className="font-medium text-indigo-300">
                        {AUTHOR_ZH[c.author_type] || c.author_type}
                      </span>
                      <span>{timeAgo(c.created_at) || ""}</span>
                      {c.author_type === "user" && (!meId || c.author_id === meId) && (
                        <button onClick={() => removeComment(detail.id, c.id)}
                          className="ml-auto px-1.5 py-0.5 rounded hover:bg-red-500/15 text-red-300/80"
                          aria-label="删除评论">
                          删除
                        </button>
                      )}
                    </div>
                    <p className="text-xs mt-1 whitespace-pre-wrap break-words">{c.body}</p>
                  </div>
                ))}
                {!comments.length && !commentsLoading && (
                  <p className="text-[11px] py-1" style={{ color: "var(--muted)" }}>
                    还没有交付意见。验收发现问题时，先在这里说清楚。
                  </p>
                )}
              </div>
              <div className="flex items-end gap-2">
                <textarea rows={2} maxLength={4000} value={commentText}
                  onChange={(e) => setCommentText(e.target.value)}
                  placeholder="写交付意见…"
                  className="input flex-1 resize-none text-xs" />
                <button onClick={() => postComment(detail.id)}
                  disabled={!commentText.trim() || commentBusy}
                  className="btn-primary !py-1.5 !px-3 text-xs shrink-0 disabled:opacity-40">
                  {commentBusy ? "发送中…" : "发表"}
                </button>
              </div>
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
