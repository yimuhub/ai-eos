"use client";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";

/* ============================================================
   治理与控制：改进审批 · 能力包市场 · 企业模板 · 自治等级
   UI 对标设计体系（深空玻璃卡 / 渐变页头 / 状态徽章）
   ============================================================ */

type Case = { id: string; agent_id: string; what_improved?: string; why?: string; risk?: string; expect_effect?: string; status?: string };
type Install = { id: string; package_key: string; version: string; approved_by?: string };
type Template = { key: string; name: string };

const AVATAR_COLORS = ["#f59e0b,#ef4444", "#6366f1,#8b5cf6", "#22d3ee,#3b82f6",
  "#10b981,#14b8a6", "#ec4899,#f43f5e"];

const STATUS_ZH: Record<string, string> = {
  SUBMITTED: "已提交", UNDER_REVIEW: "审核中", APPROVED: "已批准",
  REJECTED: "已拒绝", DEPLOYED: "已部署", ROLLED_BACK: "已回滚",
};
const statusBadge = (s: string) => {
  const map: Record<string, string> = {
    SUBMITTED: "bg-indigo-500/15 text-indigo-300",
    UNDER_REVIEW: "bg-amber-500/15 text-amber-300",
    APPROVED: "bg-emerald-500/15 text-emerald-300",
    REJECTED: "bg-red-500/15 text-red-300",
    DEPLOYED: "bg-emerald-500/15 text-emerald-300",
    ROLLED_BACK: "bg-white/[0.06] text-[var(--muted)]",
  };
  return (
    <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${map[s] || "bg-white/[0.06] text-[var(--muted)]"}`}>
      {STATUS_ZH[s] || s}
    </span>
  );
};

export default function GovernancePage() {
  const [cases, setCases] = useState<Case[]>([]);
  const [installs, setInstalls] = useState<Install[]>([]);
  const [pkg, setPkg] = useState({ key: "", version: "1.0.0" });
  const [templates, setTemplates] = useState<Template[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<Case[]>("/governance/self-improvement-cases").then(setCases).catch(() => {});
    api.get<{ installations?: Install[] }>("/governance/marketplace/packages")
      .then((r) => setInstalls(r.installations || [])).catch(() => {});
    api.get<{ templates?: Template[] }>("/governance/templates")
      .then((r) => setTemplates(r.templates || [])).catch(() => {});
  }, []);

  async function approve(id: string, status: string) {
    try {
      // 这里原来是裸 fetch 且**完全没带鉴权头**，只有 Content-Type。
      // 需要登录的部署下这个审批按钮点了必然失败（而且失败原因不可见）。
      // 改走统一 api 层后鉴权头自动注入。
      await api.put(`/governance/self-improvement-cases/${id}/${status}`);
      setMsg(`✅ ${STATUS_ZH[status] || status}`);
      api.get<Case[]>("/governance/self-improvement-cases").then(setCases).catch(() => {});
    } catch (e) {
      setMsg(`❌ ${describeError(e, "审批失败")}`);
    }
  }

  async function publishPkg() {
    if (!pkg.key.trim()) return;
    setBusy(true);
    try {
      await api.post("/governance/marketplace/packages", pkg);
      setMsg("✅ 能力包已发布到 Marketplace");
      setPkg({ key: "", version: "1.0.0" });
      api.get("/governance/marketplace/packages").then((r: any) => setInstalls(r.installations || [])).catch(() => {});
    } catch (e: any) { setMsg("❌ " + (e.message || String(e))); }
    setBusy(false);
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(239,68,68,0.10) 0%, rgba(245,158,11,0.08) 100%)", border: "1px solid rgba(239,68,68,0.25)" }}>
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#ef4444,#f59e0b)" }}>⬢</div>
          <div>
            <h1 className="text-xl font-bold text-[var(--foreground)]">治理与控制</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              改进提案审批闸门 · 能力包市场 · 企业模板
            </p>
          </div>
          <div className="ml-auto flex gap-3 text-center">
            <div className="rounded-xl px-4 py-2" style={{ background: "rgba(255,255,255,0.04)" }}>
              <p className="text-lg font-bold text-amber-300">{cases.filter(c => (c.status || "SUBMITTED") === "SUBMITTED").length}</p>
              <p className="text-[10px]" style={{ color: "var(--muted)" }}>待审提案</p>
            </div>
            <div className="rounded-xl px-4 py-2" style={{ background: "rgba(255,255,255,0.04)" }}>
              <p className="text-lg font-bold text-emerald-300">{installs.length}</p>
              <p className="text-[10px]" style={{ color: "var(--muted)" }}>已装能力包</p>
            </div>
          </div>
        </div>
      </div>

      {msg && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        {/* 改进治理 */}
        <div className="card card-static p-5">
          <h3 className="font-semibold mb-3 flex items-center gap-2">
            <span className="w-7 h-7 rounded-lg flex items-center justify-center text-sm"
              style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>🛡️</span>
            改进治理
            <span className="text-[10px] font-normal" style={{ color: "var(--muted)" }}>Agent 改动必须经你批准</span>
          </h3>
          <div className="space-y-2 max-h-[480px] overflow-y-auto pr-1">
            {cases.map((c, i) => {
              const cg = AVATAR_COLORS[i % AVATAR_COLORS.length];
              return (
                <div key={c.id} className="rounded-xl p-3 space-y-1.5"
                  style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                  <div className="flex items-start gap-2.5">
                    <span className="w-7 h-7 rounded-lg flex items-center justify-center text-xs shrink-0"
                      style={{ background: `linear-gradient(135deg,${cg})` }}>🤖</span>
                    <span className="flex-1 text-sm leading-relaxed">{c.what_improved || c.why}</span>
                    {statusBadge(c.status || "SUBMITTED")}
                  </div>
                  {c.risk && (
                    <p className="text-xs px-2.5 py-1 rounded-lg bg-amber-500/10 text-amber-300">⚠️ 风险：{c.risk}</p>
                  )}
                  {c.expect_effect && (
                    <p className="text-xs px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-300">📈 预期：{c.expect_effect}</p>
                  )}
                  {(c.status || "SUBMITTED") === "SUBMITTED" && (
                    <div className="flex gap-1.5 pt-1">
                      <button onClick={() => approve(c.id, "APPROVE")}
                        className="px-3 py-1 rounded-lg text-xs font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">✓ 批准</button>
                      <button onClick={() => approve(c.id, "REJECT")}
                        className="px-3 py-1 rounded-lg text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">✕ 拒绝</button>
                    </div>
                  )}
                </div>
              );
            })}
            {!cases.length && (
              <p className="text-sm py-8 text-center" style={{ color: "var(--muted)" }}>
                暂无改进提案 · Agent 自我改进后会出现在这里等你审批
              </p>
            )}
          </div>
        </div>

        {/* Marketplace / 企业模板 */}
        <div className="space-y-5">
          <div className="card card-static p-5">
            <h3 className="font-semibold mb-3 flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg flex items-center justify-center text-sm"
                style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>📦</span>
              Marketplace 能力包
            </h3>
            <div className="flex gap-2 mb-3">
              <input value={pkg.key} onChange={(e) => setPkg({ ...pkg, key: e.target.value })}
                placeholder="包名（如 night-tour-pack）" className="input flex-1" />
              <input value={pkg.version} onChange={(e) => setPkg({ ...pkg, version: e.target.value })}
                placeholder="版本" className="input w-24 text-center" />
              <button onClick={publishPkg} disabled={busy || !pkg.key.trim()}
                className="btn-primary text-xs disabled:opacity-40 whitespace-nowrap">发布</button>
            </div>
            <div>
              <p className="text-[11px] mb-1.5 font-semibold" style={{ color: "var(--muted)" }}>已安装</p>
              <div className="flex flex-wrap gap-1.5">
                {installs.map((i) => (
                  <span key={i.id} className="text-[11px] px-2.5 py-1 rounded-lg"
                    style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
                    📦 {i.package_key} v{i.version}
                  </span>
                ))}
                {!installs.length && <span className="text-xs py-2" style={{ color: "var(--muted)" }}>暂无安装记录</span>}
              </div>
            </div>
          </div>

          <div className="card card-static p-5">
            <h3 className="font-semibold mb-3 flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg flex items-center justify-center text-sm"
                style={{ background: "linear-gradient(135deg,#10b981,#22d3ee)" }}>🏢</span>
              企业模板
              <span className="text-[10px] font-normal" style={{ color: "var(--muted)" }}>一键复制整套 Agent 架构</span>
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {templates.map((t, i) => {
                const cg = AVATAR_COLORS[i % AVATAR_COLORS.length];
                return (
                  <div key={t.key} className="rounded-xl p-3 flex items-center gap-2.5"
                    style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                    <span className="w-8 h-8 rounded-lg flex items-center justify-center text-sm shrink-0"
                      style={{ background: `linear-gradient(135deg,${cg})` }}>🏢</span>
                    <div className="min-w-0">
                      <p className="text-sm font-medium truncate">{t.name}</p>
                      <p className="text-[10px]" style={{ color: "var(--muted)" }}>{t.key}</p>
                    </div>
                  </div>
                );
              })}
              {!templates.length && (
                <p className="text-xs py-4 col-span-2 text-center" style={{ color: "var(--muted)" }}>
                  暂无企业模板 · 可将当前公司架构存为模板
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
