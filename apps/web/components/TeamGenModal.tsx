"use client";
import { useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { notify } from "@/components/Toast";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

/* ============================================================
   创建新团队向导弹窗（指挥中心 / 公司管理 共用）
   范本 → AI 解析组织蓝图 → 确认 → 一键生成全新 Agent 公司
   ============================================================ */

const TG_TEMPLATE = `【组织架构】
设市场部、内容部、技术部三个部门

【需要哪些 Agent】
- CEO：统筹全局，负责战略决策
- 文案专员：写小红书/公众号文案
- 设计师：生成海报与配图
- 运营专员：账号运营与数据分析

【各自能力要求】
文案要擅长种草文风；设计师要会用AI生图；运营要懂数据复盘…`;

export default function TeamGenModal({ open, onClose, onCreated }: {
  open: boolean;
  onClose: () => void;
  onCreated?: (companyName: string) => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [name, setName] = useState("");
  const [industry, setIndustry] = useState("");
  const [blueprint, setBlueprint] = useState("");
  const [teamAttachments, setTeamAttachments] = useState<Attachment[]>([]); // 📎 建团参考材料
  const [preview, setPreview] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  async function doPreview() {
    if (!blueprint.trim()) return;
    setBusy(true);
    try {
      const d = await api.post("/team-generator/preview", {
        company_name: name.trim() || null,
        industry, blueprint,
      });
      setPreview(d.blueprint);
      setStep(2);
    } catch (e: any) { notify("error", `方案生成失败：${e.message || e}`); }
    setBusy(false);
  }

  async function doGenerate() {
    setBusy(true);
    try {
      const d = await api.post("/team-generator/generate", {
        company_name: name.trim() || null,
        industry, blueprint,
        attachments: teamAttachments.length ? teamAttachments : undefined, // 📎 参考材料
      });
      onClose(); setStep(1); setPreview(null);
      setBlueprint(""); setIndustry(""); setName(""); setTeamAttachments([]);
      onCreated?.(`${d.company_name}（${d.departments} 部门 · ${d.agents.length} Agent）`);
    } catch (e: any) { notify("error", `团队生成失败：${e.message || e}`); }
    setBusy(false);
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
      onClick={onClose}>
      <div className="w-full max-w-2xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
        style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(139,92,246,0.4)" }}
        onClick={(e) => e.stopPropagation()}>
        {step === 1 && (<>
          <h3 className="font-bold text-base">✨ 创建新团队公司</h3>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            按范本填写三项要求，AI 将解析为组织蓝图并一键生成全新 Agent 公司（与其他公司完全隔离、互不影响）
          </p>
          <div className="grid grid-cols-2 gap-2.5">
            <input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="公司名（可选，AI 可代起）" className="input" />
            <input value={industry} onChange={(e) => setIndustry(e.target.value)}
              placeholder="行业（如：文旅 / 电商 / 教育）" className="input" />
          </div>
          <div className="rounded-xl p-3 space-y-1.5 text-[11px]"
            style={{ background: "rgba(139,92,246,0.08)", border: "1px dashed rgba(139,92,246,0.35)", color: "var(--muted)" }}>
            <p className="font-medium text-violet-300">📋 范本提示词（可直接照抄修改）：</p>
            <pre className="whitespace-pre-wrap leading-relaxed">{TG_TEMPLATE}</pre>
          </div>
          <textarea rows={8} value={blueprint} onChange={(e) => setBlueprint(e.target.value)}
            placeholder={"【组织架构】…\n【需要哪些 Agent】…\n【各自能力要求】…"}
            className="input resize-none font-mono !text-[11px]" />
          <AttachmentPicker value={teamAttachments} onChange={setTeamAttachments}
            label="📎 挂参考材料（业务规范 / 岗位 JD / 制度文件，供生成团队参考）" />
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={onClose} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
              style={{ border: "1px solid var(--border)" }}>取消</button>
            <button onClick={doPreview} disabled={!blueprint.trim() || busy}
              className="btn-primary disabled:opacity-40">
              {busy ? "AI 解析中…" : "🔍 解析为组织蓝图"}
            </button>
          </div>
        </>)}
        {step === 2 && preview && (<>
          <h3 className="font-bold text-base">📋 组织蓝图确认</h3>
          <div className="text-sm font-semibold text-violet-300">{preview.company_name}</div>
          {preview.intro && <p className="text-xs" style={{ color: "var(--muted)" }}>{preview.intro}</p>}
          <div>
            <p className="text-xs font-semibold mb-1.5">🏢 部门（{preview.departments?.length || 0}）</p>
            <div className="flex flex-wrap gap-1.5">
              {(preview.departments || []).map((d: any, i: number) => (
                <span key={i} className="text-[11px] px-2.5 py-1 rounded-lg"
                  style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                  🏢 {d.name}{d.description ? ` · ${d.description}` : ""}
                </span>
              ))}
            </div>
          </div>
          <div>
            <p className="text-xs font-semibold mb-1.5">🤖 Agent 团队（{preview.agents?.length || 0}）</p>
            <div className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
              {(preview.agents || []).map((a: any, i: number) => (
                <div key={i} className="rounded-xl p-2.5 flex items-start gap-2.5"
                  style={{ background: "var(--surface-2)", border: a.is_leader ? "1px solid rgba(245,158,11,0.45)" : "1px solid var(--border)" }}>
                  <span className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs font-bold shrink-0"
                    style={{ background: a.is_leader ? "linear-gradient(135deg,#f59e0b,#ef4444)" : "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
                    {(a.name || "?").slice(0, 1)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium">
                      {a.name}
                      {a.is_leader && <span className="ml-1.5 text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-300">负责人</span>}
                      <span className="ml-1.5 text-[10px] font-normal" style={{ color: "var(--muted)" }}>{a.role}{a.department ? ` · ${a.department}` : ""}</span>
                    </p>
                    {a.system_prompt && (
                      <p className="text-[10px] mt-0.5 line-clamp-2" style={{ color: "var(--muted)" }}>{a.system_prompt}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={() => setStep(1)} className="px-4 py-2 rounded-xl text-xs hover:bg-white/[0.06]"
              style={{ border: "1px solid var(--border)" }}>← 返回修改</button>
            <button onClick={doGenerate} disabled={busy}
              className="btn-primary disabled:opacity-40 bg-gradient-to-r from-violet-600 to-indigo-500">
              {busy ? "创建中…" : "🎉 确认无误，一键生成公司"}
            </button>
          </div>
        </>)}
      </div>
    </div>, document.body
  );
}
