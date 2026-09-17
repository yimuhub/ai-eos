"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Schedule = { id: string; name: string; trigger_type: string; action_type: string;
  status: string; next_run_at?: string; last_run_at?: string; run_count: number };

export default function SchedulesPage() {
  const [cid, setCid] = useState("");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState("interval");
  const [seconds, setSeconds] = useState("3600");
  const [dailyTime, setDailyTime] = useState("09:00");
  const [actionType, setActionType] = useState("snapshot_performance");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.get("/companies").then((cs: any[]) => {
      const c = cs[0]?.id;
      if (c) { setCid(c); reload(c); }
    });
  }, []);

  const reload = (c = cid) => api.get(`/schedules?company_id=${c}`).then(setSchedules);

  const create = async () => {
    try {
      const body: any = { name, trigger_type: trigger,
        action_type: actionType };
      if (trigger === "interval") body.seconds = Number(seconds);
      else body.time = dailyTime;
      await api.post(`/schedules?company_id=${cid}`, body);
      setName(""); setMsg("调度已创建"); reload();
    } catch (e) { setMsg(String(e)); }
  };

  const toggle = async (s: Schedule) => {
    if (s.status === "ACTIVE") await api.post(`/schedules/${s.id}/pause`);
    else await api.post(`/schedules/${s.id}/resume`);
    reload();
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4">⏰ 自动化调度</h1>

      <div className="border border-[var(--border)] rounded p-4 mb-6 card space-y-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="任务名称"
          className="w-full bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm" />
        <div className="flex gap-2">
          <select value={trigger} onChange={(e) => setTrigger(e.target.value)} className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm">
            <option value="interval">固定间隔</option>
            <option value="daily">每天定时</option>
          </select>
          {trigger === "interval" ? (
            <input value={seconds} onChange={(e) => setSeconds(e.target.value)} placeholder="秒"
              className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm w-32" />
          ) : (
            <input value={dailyTime} onChange={(e) => setDailyTime(e.target.value)} placeholder="HH:MM"
              className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm w-32" />
          )}
          <select value={actionType} onChange={(e) => setActionType(e.target.value)} className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-sm">
            <option value="snapshot_performance">绩效快照</option>
            <option value="send_message">发协作消息</option>
            <option value="start_workflow">启动工作流</option>
          </select>
        </div>
        <button onClick={create} disabled={!name}
          className="btn-primary disabled:opacity-40">创建调度</button>
      </div>

      <div className="space-y-2">
        {schedules.map((s) => (
          <div key={s.id} className="border border-[var(--border)] rounded p-3 card flex justify-between items-center">
            <div>
              <b className="text-sm">{s.name}</b>
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${s.status === "ACTIVE" ? "bg-green-900/50 text-green-300" : "bg-white/[0.04] text-[var(--foreground)]"}`}>
                {s.status}</span>
              <div className="text-xs text-[var(--muted)]">
                {s.trigger_type} · 动作:{s.action_type} · 已执行{s.run_count}次
                {s.next_run_at && ` · 下次:${s.next_run_at.slice(11, 16)}`}
              </div>
            </div>
            <button onClick={() => toggle(s)} className="px-3 py-1 border border-gray-600 rounded text-xs hover:bg-white/[0.04]">
              {s.status === "ACTIVE" ? "暂停" : "恢复"}
            </button>
          </div>
        ))}
        {!schedules.length && <p className="text-[var(--muted)] text-sm">暂无调度任务</p>}
      </div>
      {msg && <p className="mt-3 text-sm text-green-700">{msg}</p>}
    </div>
  );
}
