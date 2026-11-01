"use client";
import { useEffect, useMemo, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

type Schedule = { id: string; name: string; trigger_type: string; action_type: string;
  status: string; next_run_at?: string; last_run_at?: string; run_count: number };

export default function SchedulesPage() {
  const cid = useActiveCompanyId();
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState("interval");
  const [seconds, setSeconds] = useState("3600");
  const [dailyTime, setDailyTime] = useState("09:00");
  const [actionType, setActionType] = useState("snapshot_performance");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [msg, setMsg] = useState("");

  // 调度列表交给统一数据层：缓存首屏、切公司自动重取
  // bundle 带 cid：新公司的数据回来之前，不继续展示旧公司的调度
  const { data, error, refresh } = useResource(
    cid ? `schedules:${cid}` : null,
    async () => {
      const rows = await api.get<Schedule[]>(`/schedules?company_id=${cid}`);
      return { cid: cid as string, rows: Array.isArray(rows) ? rows : [] };
    },
  );
  const active = data && data.cid === cid ? data : null;
  const schedules = useMemo<Schedule[]>(() => active?.rows ?? [], [active]);
  const loading = cid === undefined || (cid !== null && !active && !error);

  // 加载失败沿用原来的 Toast 提示（页面不再静默空白）
  useEffect(() => {
    if (error) notify("error", error || "加载调度失败");
  }, [error]);

  const create = async () => {
    if (!name.trim()) { setMsg("请先填写调度名称"); return; }
    if (!cid) return;
    try {
      const body: any = { name, trigger_type: trigger,
        action_type: actionType };
      if (trigger === "interval") body.seconds = Number(seconds);
      else body.time = dailyTime;
      if (attachments.length) body.attachments = attachments;
      await api.post(`/schedules?company_id=${cid}`, body);
      setName(""); setAttachments([]); setMsg("调度已创建");
      await refresh();
    } catch (e) { setMsg(`创建失败：${describeError(e)}`); }
  };
  const toggle = async (s: Schedule) => {
    try {
      if (s.status === "ACTIVE") await api.post(`/schedules/${s.id}/pause`);
      else await api.post(`/schedules/${s.id}/resume`);
      await refresh();
      notify("success", s.status === "ACTIVE" ? "已暂停" : "已恢复");
    } catch (e) {
      // 原实现没有 catch：失败会变成未处理的 Promise 拒绝，界面毫无反馈
      notify("error", describeError(e, "切换调度状态失败"));
    }
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
        <AttachmentPicker value={attachments} onChange={setAttachments} label="📎 关联附件（任务说明 / 数据文件）" />
        <button onClick={create} disabled={!name}
          className="btn-primary disabled:opacity-40">创建调度</button>
      </div>

      <div className="space-y-2">
        {/* 加载态：原先没有，用户看到的"暂无调度任务"其实是"还没加载完" */}
        {loading && (
          <div className="space-y-2" aria-busy="true" aria-label="正在加载调度">
            {[0, 1, 2].map((i) => (
              <div key={i} className="border border-[var(--border)] rounded p-3 card">
                <div className="skeleton h-4 w-40 rounded" />
                <div className="skeleton h-3 w-64 rounded mt-2" />
              </div>
            ))}
          </div>
        )}

        {!loading && schedules.map((s) => (
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
        {!loading && !schedules.length && (
          <p className="text-[var(--muted)] text-sm">
            {msg || (cid === null
              // 没有公司就没有调度可谈，要明确告诉用户，而不是给一个空列表
              ? "还没有 AI 公司，请先在指挥中心创建团队"
              : "暂无调度任务——在上方填写名称并创建第一个定时任务")}
          </p>
        )}
      </div>
      {msg && schedules.length > 0 && <p className="mt-3 text-sm text-green-700">{msg}</p>}
    </div>
  );
}
