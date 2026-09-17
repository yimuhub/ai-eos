"use client";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { api, setActiveCompanyId } from "@/lib/api";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";
import TeamGenModal from "@/components/TeamGenModal";

/* ============================================================
   公司管理：管理各公司团队 · 组织架构一览 · 单个/批量删除
   风格对齐组织架构管理界面
   ============================================================ */

type CompanyRow = {
  id: string; name: string; description?: string | null; created_at?: string;
  stats: { agents: number; departments: number; projects: number; tasks: number };
  leader_name?: string | null;
  department_names: string[];
};

const AVATAR_GRADS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#6366f1"];

export default function CompanyManagePage() {
  const router = useRouter();
  const [mounted, setMounted] = useState(false);
  const [rows, setRows] = useState<CompanyRow[]>([]);
  const [activeCid, setActiveCidState] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  // 批量删除
  const [showBatch, setShowBatch] = useState(false);
  const [batchSel, setBatchSel] = useState<Set<string>>(new Set());
  // 单卡确认（删除）
  const [confirmTarget, setConfirmTarget] = useState<CompanyRow | null>(null);
  const [showTeamGen, setShowTeamGen] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  const load = useCallback(async () => {
    try {
      const d = await api.get("/company-manage/list");
      setRows(d.companies || []);
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    if (typeof window !== "undefined") {
      setActiveCidState(localStorage.getItem("aieos_active_cid") || "");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function del(cid: string) {
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/company-manage/batch-delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ company_ids: [cid] }),
      });
      const d = await r.json();
      setMsg(`🗑 已删除公司（${d.deleted_count}）${d.failed?.length ? `，失败 ${d.failed.length}` : ""}`);
      // 若删的是激活公司 → 切回剩余第一家
      if (cid === activeCid && rows.length > 1) {
        const next = rows.find((x) => x.id !== cid);
        if (next) setActiveCompanyId(next.id);
      }
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setConfirmTarget(null);
    setBusy(false);
  }

  async function batchDelete() {
    if (!batchSel.size) return;
    // 原来这里**没有任何二次确认**：勾选若干公司后点一下就全部删除，
    // 而且是不可恢复的物理删除（连同其下所有项目、员工、成果）。
    // 这是全站破坏力最大的入口，因此要求逐字输入确认词。
    const selectedNames = rows.filter((r) => batchSel.has(r.id)).map((r) => r.name);
    if (!(await confirmAction({
      title: `确认彻底删除这 ${batchSel.size} 家公司？`,
      description: "这是全站破坏力最大的操作，选中公司下的所有内容都会一并消失。",
      bullets: [
        ...selectedNames.slice(0, 5).map((n) => `「${n}」及其全部项目、员工、成果与日志`),
        ...(selectedNames.length > 5 ? [`…以及另外 ${selectedNames.length - 5} 家公司`] : []),
        "此操作不可恢复",
      ],
      confirmPhrase: "删除",
      confirmText: "删除公司",
    }))) return;
    setBusy(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/company-manage/batch-delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
        body: JSON.stringify({ company_ids: Array.from(batchSel) }),
      });
      const d = await r.json();
      setMsg(`🗑 已彻底删除 ${d.deleted_count} 家公司${d.failed?.length ? `，失败 ${d.failed.length}` : ""}`);
      setShowBatch(false); setBatchSel(new Set());
      await load();
    } catch (e: any) { setMsg("❌ " + (e.message || e)); }
    setBusy(false);
  }

  function toggleSel(id: string) {
    setBatchSel((prev) => {
      const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
    });
  }

  function switchTo(cid: string) {
    setActiveCompanyId(cid);
    setMsg(`✅ 已切换到该公司，全站数据随之切换`);
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)", border: "1px solid rgba(99,102,241,0.22)" }}>
        <div className="flex items-center gap-4 flex-wrap">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>🏢</div>
          <div>
            <h1 className="text-xl font-bold">公司管理</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              管理各公司团队 · 组织架构一览 · 数据完全隔离互不影响
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button onClick={() => router.push("/")} className="btn-ghost text-xs">← 指挥中心</button>
            <button onClick={() => setShowTeamGen(true)}
              className="text-xs px-4 py-2 rounded-lg font-medium text-white transition-all"
              style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}>
              ✨ 创建新团队
            </button>
          </div>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg}
        </div>
      )}

      {/* 卡片网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {rows.map((c, i) => {
          const grad = AVATAR_GRADS[i % AVATAR_GRADS.length];
          const isActive = c.id === activeCid;
          return (
            <div key={c.id} className={`card group relative overflow-hidden flex flex-col ${isActive ? "!border-emerald-500/50" : ""}`}>
              <div className="absolute top-0 left-0 w-full h-[2px] opacity-70 group-hover:opacity-100 transition-opacity"
                style={{ background: `linear-gradient(90deg, ${grad.split(",")[0]}, transparent)` }} />

              {/* 头部：名称 + 当前使用标记 */}
              <div className="flex items-start gap-3">
                <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                  style={{ background: `linear-gradient(135deg, ${grad})` }}>🏢</span>
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-sm leading-snug truncate">{c.name}</p>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                    {c.leader_name ? `🧭 负责人：${c.leader_name}` : "未设负责人"}
                  </p>
                </div>
                {isActive && (
                  <span className="text-[9px] px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 shrink-0">
                    ● 使用中
                  </span>
                )}
              </div>

              {/* 简介 */}
              {c.description && (
                <p className="text-[11px] mt-2 line-clamp-1" style={{ color: "var(--muted)" }}>{c.description}</p>
              )}

              {/* 组织架构预览 */}
              <div className="mt-3 space-y-1.5 flex-1">
                <p className="text-[10px] font-semibold" style={{ color: "var(--muted)" }}>
                  组织架构（{c.stats.departments} 部门 / {c.stats.agents} 名 Agent）
                </p>
                <div className="flex flex-wrap gap-1">
                  {c.department_names.slice(0, 5).map((dn) => (
                    <span key={dn} className="text-[10px] px-2 py-0.5 rounded-md"
                      style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                      🏢 {dn}
                    </span>
                  ))}
                  {c.department_names.length > 5 && (
                    <span className="text-[10px]" style={{ color: "var(--muted)" }}>+{c.department_names.length - 5}</span>
                  )}
                  {!c.department_names.length && (
                    <span className="text-[10px]" style={{ color: "var(--muted)" }}>暂无部门</span>
                  )}
                </div>
              </div>

              {/* 统计 */}
              <div className="mt-3 grid grid-cols-3 gap-1.5 text-center">
                {[["Agent", c.stats.agents], ["项目", c.stats.projects], ["任务", c.stats.tasks]].map(([k, v]) => (
                  <div key={k as string} className="rounded-lg py-1.5" style={{ background: "var(--surface-2)" }}>
                    <p className="text-sm font-bold tabular-nums">{v as number}</p>
                    <p className="text-[9px]" style={{ color: "var(--muted)" }}>{k as string}</p>
                  </div>
                ))}
              </div>

              {/* 悬停操作条（与项目中心一致） */}
              <div className="mt-auto pt-3 mt-3 border-t flex gap-1.5 opacity-0 group-hover:opacity-100 transition-all duration-200"
                style={{ borderColor: "var(--border)" }} onClick={(e) => e.stopPropagation()}>
                {!isActive && (
                  <button onClick={() => switchTo(c.id)}
                    className="flex-1 text-[11px] py-1.5 rounded-lg hover:bg-indigo-500/12 text-indigo-300 transition-colors"
                    style={{ border: "1px solid rgba(99,102,241,0.3)" }}>⇄ 切换使用</button>
                )}
                {isActive && (
                  <span className="flex-1 text-[11px] py-1.5 rounded-lg text-center text-emerald-300"
                    style={{ border: "1px solid rgba(16,185,129,0.25)" }}>当前使用中</span>
                )}
                <button onClick={() => setConfirmTarget(c)}
                  className="flex-1 text-[11px] py-1.5 rounded-lg text-red-400/90 hover:bg-red-500/12 transition-colors"
                  style={{ border: "1px solid rgba(248,113,113,0.25)" }}>🗑 删除</button>
              </div>
            </div>
          );
        })}
      </div>

      {!rows.length && (
        <div className="card card-static text-center py-16 space-y-2">
          <p className="text-sm">还没有公司</p>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            前往指挥中心点「✨ 创建新团队」生成第一家 Agent 公司
          </p>
        </div>
      )}

      {/* 批量删除按钮 */}
      {rows.length >= 1 && (
        <button onClick={() => { setShowBatch(true); setBatchSel(new Set()); }}
          className="fixed bottom-6 right-6 z-40 px-5 py-2.5 rounded-xl text-sm font-medium text-white shadow-lg transition-all hover:scale-105"
          style={{ background: "linear-gradient(135deg,#ef4444,#f97316)" }}>
          🗑 批量删除
        </button>
      )}

      {/* 批量删除确认弹窗 */}
      {mounted && showBatch && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
          style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
          onClick={() => setShowBatch(false)}>
          <div className="w-full max-w-lg rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.45)" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2.5">
              <h3 className="font-bold text-sm text-red-300 flex-1">🗑 批量删除公司</h3>
              {batchSel.size > 0 && (
                <span className="text-[11px] px-2 py-0.5 rounded-full bg-red-500/15 text-red-300">已选 {batchSel.size}</span>
              )}
              <button onClick={() => setShowBatch(false)} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              将彻底删除选中公司的全部 Agent、部门、项目、任务、会议、知识库等数据，不可恢复！
            </p>
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              <button onClick={() => setBatchSel(batchSel.size === rows.length ? new Set() : new Set(rows.map((x) => x.id)))}
                className="w-full px-3 py-2 rounded-xl text-xs text-left hover:bg-white/[0.05]"
                style={{ border: "1px solid var(--border)" }}>
                {batchSel.size === rows.length ? "☑" : "☐"} 全选（{rows.length} 家）
              </button>
              {rows.map((x) => {
                const on = batchSel.has(x.id);
                return (
                  <button key={x.id} onClick={() => toggleSel(x.id)}
                    className={`w-full px-3 py-2 rounded-xl text-xs text-left transition-all ${on ? "bg-red-500/10 border border-red-500/40" : "hover:bg-white/[0.04]"}`}
                    style={!on ? { border: "1px solid var(--border)" } : {}}>
                    {on ? "✓" : "☐"} 🏢 {x.name}
                    <span className="ml-2 opacity-60">{x.stats.agents} Agent · {x.stats.departments} 部门</span>
                    {x.id === activeCid && <span className="ml-1.5 text-[9px] text-emerald-300">使用中</span>}
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-2 pt-1">
              <span className="text-[10px] ml-auto" style={{ color: "var(--muted)" }}>⚠️ 删除不可恢复</span>
            </div>
            <div className="flex justify-end">
              <button onClick={batchDelete} disabled={!batchSel.size || busy}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90 disabled:opacity-40 transition-all">
                {busy ? "删除中…" : `确认彻底删除 ${batchSel.size} 家`}
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 单卡删除确认弹窗 */}
      {mounted && confirmTarget && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setConfirmTarget(null)}>
          <div className="w-full max-w-sm rounded-2xl p-5 space-y-4 animate-fade-up"
            style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(239,68,68,0.45)" }}
            onClick={(e) => e.stopPropagation()}>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">
              ⚠️ 彻底删除公司「{confirmTarget.name}」？{"\n"}
              全部 {confirmTarget.stats.agents} 名 Agent、{confirmTarget.stats.departments} 个部门、
              {" "}项目任务会议知识库等数据将一并清除，不可恢复！
            </p>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmTarget(null)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
                style={{ border: "1px solid var(--border)" }}>取消</button>
              <button onClick={() => del(confirmTarget.id)} disabled={busy}
                className="px-4 py-2 rounded-xl text-xs font-medium text-white bg-gradient-to-r from-red-600 to-orange-500 hover:opacity-90 disabled:opacity-40 transition-all">
                确认彻底删除
              </button>
            </div>
          </div>
        </div>, document.body
      )}

      {/* 创建新团队向导 */}
      <TeamGenModal open={showTeamGen} onClose={() => setShowTeamGen(false)}
        onCreated={async (name) => {
          setMsg(`🎉 新团队「${name}」已创建`);
          await load();
        }} />
    </div>
  );
}
