"use client";
import { useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type SyncRun = { id: string; status: string; records_upserted: number;
  watermark?: string; error?: string; finished_at?: string; kind: string;
  entity_type: string };
type Connector = { id: string; name: string; display_name?: string;
  description?: string; kind: string; entity_type?: string; enabled: boolean;
  risk: string; config?: Record<string, unknown>;
  last_sync?: SyncRun | null };

export default function ConnectorsPage() {
  const cid = useActiveCompanyId();
  const [busy, setBusy] = useState<string | null>(null);
  const [openRuns, setOpenRuns] = useState<string | null>(null);
  const [runs, setRuns] = useState<SyncRun[]>([]);

  const { data, error, refresh } = useResource(
    cid ? `connectors:${cid}` : null,
    async () => {
      const r = await api.get<{ connectors: Connector[] }>(
        `/connectors?company_id=${cid}`);
      return { cid: cid as string, rows: r?.connectors ?? [] };
    },
  );
  const active = data && data.cid === cid ? data : null;
  const rows = active?.rows ?? [];

  const syncNow = async (c: Connector) => {
    if (!cid) return;
    setBusy(c.id);
    try {
      const r = await api.post<{ ok: boolean; run: SyncRun }>(
        `/connectors/${c.id}/sync?company_id=${cid}`, {});
      notify(r.ok ? "success" : "error",
        r.ok ? `「${c.display_name || c.name}」同步 ${r.run.records_upserted} 条`
             : `同步失败：${r.run.error || "未知"}`);
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "同步失败"));
    } finally {
      setBusy(null);
    }
  };

  const showRuns = async (c: Connector) => {
    if (!cid) return;
    setOpenRuns(openRuns === c.id ? null : c.id);
    if (openRuns !== c.id) {
      try {
        const r = await api.get<{ runs: SyncRun[] }>(
          `/connectors/${c.id}/runs?company_id=${cid}&limit=10`);
        setRuns(r?.runs ?? []);
      } catch (e) {
        notify("error", describeError(e));
      }
    }
  };

  const toggleEnabled = async (c: Connector) => {
    if (!cid) return;
    try {
      await api.patch(`/toolbus/tools/${c.id}?company_id=${cid}&enabled=${!c.enabled}`);
      notify("success", c.enabled ? "已停用" : "已启用");
      await refresh();
    } catch (e) {
      notify("error", describeError(e));
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">连接器</div>
      <p className="mb-4 text-white/50">
        外部系统的数据入口：同步状态、水位与失败原因都在这里。创建连接器在「基座市场」装连接器包，或用 POST /connectors。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}
      <div className="grid gap-3">
        {rows.map((c) => (
          <div key={c.id} className="rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-medium">
                  {c.display_name || c.name}
                  <span className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${c.enabled ? "bg-emerald-500/20 text-emerald-300" : "bg-white/10 text-white/50"}`}>
                    {c.enabled ? "启用" : "停用"}
                  </span>
                </div>
                <div className="mt-0.5 text-white/40">
                  {c.kind} · {c.entity_type} · 风险 {c.risk}
                </div>
              </div>
              <div className="flex shrink-0 gap-1.5">
                <button onClick={() => syncNow(c)} disabled={busy === c.id || !c.enabled}
                        className="rounded-md bg-emerald-600/80 px-3 py-1 hover:bg-emerald-600 disabled:opacity-40">
                  {busy === c.id ? "同步中…" : "立即同步"}
                </button>
                <button onClick={() => showRuns(c)}
                        className="rounded-md bg-white/10 px-3 py-1 hover:bg-white/20">
                  {openRuns === c.id ? "收起" : "同步账"}
                </button>
                <button onClick={() => toggleEnabled(c)}
                        className="rounded-md bg-white/10 px-3 py-1 hover:bg-white/20">
                  {c.enabled ? "停用" : "启用"}
                </button>
              </div>
            </div>
            {c.last_sync && (
              <div className={`mt-2 text-[12px] ${c.last_sync.status === "success" ? "text-emerald-300/80" : "text-red-300/80"}`}>
                上次同步：{c.last_sync.status} · {c.last_sync.records_upserted} 条
                {c.last_sync.watermark ? ` · 水位 ${c.last_sync.watermark}` : ""}
                {c.last_sync.error ? ` · ${c.last_sync.error}` : ""}
              </div>
            )}
            {openRuns === c.id && (
              <div className="mt-3 rounded-lg bg-black/30 p-3 text-[12px]">
                {runs.map((r) => (
                  <div key={r.id} className="flex justify-between py-0.5 text-white/60">
                    <span>{r.finished_at?.slice(0, 19).replace("T", " ") || "-"}</span>
                    <span>{r.status} · {r.records_upserted} 条</span>
                    <span className="max-w-[40%] truncate text-white/40">{r.error || ""}</span>
                  </div>
                ))}
                {!runs.length && <div className="text-white/40">还没有同步记录</div>}
              </div>
            )}
          </div>
        ))}
        {!rows.length && cid && <div className="text-white/40">还没有连接器。去「基座市场」装一个连接器包，或用 API 创建。</div>}
      </div>
    </div>
  );
}
