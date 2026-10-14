"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EVENT_TYPE_ZH, zh } from "@/lib/i18n";

type Ev = { id: string; event_type: string; actor_type: string | null; entity_id: string | null; payload: any; created_at: string };

export default function LogsPage() {
  const [events, setEvents] = useState<Ev[]>([]);
  const [typeFilter, setTypeFilter] = useState("");

  const load = useCallback(async () => {
    const q = `/events?limit=200${typeFilter ? `&event_type=${typeFilter}` : ""}`;
    setEvents(await api.get(q));
  }, [typeFilter]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold tracking-tight">工作记录</h1>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}
          className="input w-auto !py-1.5 text-xs">
          <option value="">全部事件</option>
          {[["task.created", "任务创建"], ["task.status_changed", "状态变更"],
            ["agent.started_task", "开始工作"], ["agent.completed_task", "完成任务"],
            ["orchestration.started", "协同启动"], ["orchestration.failed", "协同失败"],
            ["message.sent", "消息发送"], ["asset.created", "成果入库"]].map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        </select>
      </div>
      <div className="card-lg divide-y divide-[var(--border)] max-h-[80vh] overflow-auto">
        {events.map((e) => (
          <div key={e.id} className="px-4 py-2.5 text-xs flex items-start justify-between gap-4">
            <div>
              <span className={`mr-2 ${e.event_type.includes("fail") ? "text-red-400" : e.event_type.includes("completed") ? "text-green-400" : "text-blue-300"}`}>
                ● {zh(EVENT_TYPE_ZH, e.event_type)}
              </span>
              <span className="text-[var(--muted)]">{JSON.stringify(e.payload).slice(0, 100)}</span>
            </div>
            <span className="text-[var(--muted)] shrink-0">{new Date(e.created_at).toLocaleString()}</span>
          </div>
        ))}
        {!events.length && <p className="p-6 text-center text-xs text-[var(--muted)]">暂无事件</p>}
      </div>
    </div>
  );
}
