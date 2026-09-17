"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Ranked = { agent_name: string; score: number; tasks_total: number; tasks_completed: number; tasks_failed: number; meeting_spoke_count: number; messages_sent: number };

export default function PerformancePage() {
  const [cid, setCid] = useState("");
  const [ranked, setRanked] = useState<Ranked[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.get("/companies").then((cs: any[]) => {
      const c = cs[0]?.id;
      if (c) setCid(c);
    });
  }, []);

  const snapshot = async () => {
    try {
      const r = await api.post(`/performance/company/${cid}/snapshot`);
      setRanked(r.ranked);
      setErr("");
    } catch (e) { setErr(String(e)); }
  };

  const loadHistory = async () => {
    const h = await api.get(`/performance/company/${cid}/history?limit=20`);
    setHistory(h);
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4">📊 Agent 绩效排行</h1>
      {!cid && <p className="text-[var(--muted)]">加载中…</p>}
      {cid && (
        <>
          <button onClick={snapshot} className="mb-4 px-4 py-1.5 bg-blue-600 text-white rounded text-sm">
            生成绩效快照
          </button>
          <div className="space-y-2 mb-6">
            {ranked.map((r, i) => (
              <div key={i} className="border border-[var(--border)] rounded p-3 card flex items-center gap-4">
                <span className={`text-xl font-bold w-8 ${i === 0 ? "text-yellow-500" : i === 1 ? "text-[var(--muted)]" : "text-orange-300"}`}>
                  {i + 1}
                </span>
                <div className="flex-1">
                  <b>{r.agent_name}</b>
                  <div className="text-xs text-[var(--muted)]">任务{r.tasks_total}/完成{r.tasks_completed}/失败{r.tasks_failed}
                    · 会议发言{r.meeting_spoke_count} · 消息{r.messages_sent}</div>
                </div>
                <div className="text-2xl font-bold">{r.score}</div>
              </div>
            ))}
          </div>
          <button onClick={loadHistory} className="px-3 py-1 border border-gray-600 rounded text-xs hover:bg-white/[0.04]">查看历史快照</button>
          <div className="mt-2 space-y-1 text-xs">
            {history.map((h, i) => (
              <div key={i}>快照 @ {h.period_end.slice(0, 16)} — agent:{h.agent_id.slice(0, 8)} 分数 {h.score}</div>
            ))}
          </div>
        </>
      )}
      {err && <p className="text-red-600 text-sm mt-3">{err}</p>}
    </div>
  );
}
