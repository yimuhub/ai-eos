"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Req = { id: string; action_type: string; status: string; reason?: string; payload?: any; created_at?: string };
type Override = { level: string; enabled: boolean; override_by?: string };

export default function InterventionPage() {
  const [reqs, setReqs] = useState<Req[]>([]);
  const [over, setOver] = useState<Override>({ level: "L0", enabled: false, override_by: "" });
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.get("/intervention/requests?status=WAITING_APPROVAL&limit=20").then(setReqs).catch(() => {});
    api.get("/intervention/overrides").then((r: any) => setOver(r.overrides?.[0] || over)).catch(() => {});
  }, []);

  async function approve(id: string, decision: string) {
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/intervention/requests/${id}/${decision}`,
      { method: "POST" });
    setMsg(`已${decision === "APPROVE" ? "批准" : "拒绝"}`);
    api.get("/intervention/requests?status=WAITING_APPROVAL&limit=20").then(setReqs).catch(() => {});
  }

  async function saveOverride() {
    try {
      const r2 = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/intervention/overrides`, { method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(over) }); if (!r2.ok) throw new Error(`HTTP ${r2.status}`);
      setMsg("✅ 覆盖设置已保存");
    } catch (e: any) { setMsg(e.message || String(e)); }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4 text-[var(--foreground)]">🛑 人工介入</h1>
      <div className="grid grid-cols-2 gap-4">
        {/* 待审批请求 */}
        <div className="card card-static p-4">
          <h3 className="font-medium mb-3 text-[var(--foreground)]">待审批请求</h3>
          {reqs.map((r) => (
            <div key={r.id} className="rounded-xl p-2.5 bg-white/[0.03] border border-[var(--border)] text-sm text-[var(--foreground)] mb-2">
              <div className="flex justify-between">
                <span>{r.action_type}</span>
                <span className="text-xs text-[var(--muted)]">{r.created_at?.slice(0, 16)}</span>
              </div>
              <pre className="text-xs mt-1 card p-1 rounded overflow-auto text-[var(--muted)]">{JSON.stringify(r.payload, null, 2)}</pre>
              <div className="mt-2 flex gap-2">
                <button onClick={() => approve(r.id, "APPROVE")} className="px-2 py-0.5 bg-green-700 hover:bg-green-600 text-white rounded text-xs">批准</button>
                <button onClick={() => approve(r.id, "REJECT")} className="px-2 py-0.5 bg-red-700 hover:bg-red-600 text-white rounded text-xs">拒绝</button>
              </div>
            </div>
          ))}
          {!reqs.length && <p className="text-xs text-[var(--muted)]">无待审批请求</p>}
        </div>

        {/* 全局覆盖 */}
        <div className="card card-static p-4 space-y-2">
          <h3 className="font-medium text-[var(--foreground)]">全局权限覆盖</h3>
          <label className="flex items-center gap-2 text-sm text-[var(--foreground)]">
            <input type="checkbox" checked={over.enabled} onChange={(e) => setOver({ ...over, enabled: e.target.checked })} />
            启用覆盖
          </label>
          <select value={over.level} onChange={(e) => setOver({ ...over, level: e.target.value })}
            className="input">
            <option value="L0">L0 — 每次确认</option>
            <option value="L1">L1 — 每3次</option>
            <option value="L2">L2 — 每10次</option>
            <option value="L3">L3 — 每50次</option>
          </select>
          <input value={over.override_by || ""} onChange={(e) => setOver({ ...over, override_by: e.target.value })} placeholder="覆盖人"
            className="input" />
          <button onClick={saveOverride}
            className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm">保存覆盖</button>
        </div>
      </div>
      {msg && <p className="mt-3 text-sm text-blue-300">{msg}</p>}
    </div>
  );
}
