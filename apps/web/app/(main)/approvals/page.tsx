"use client";
import { useResource } from "@/lib/useResource";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type ApprovalRequest = { id: string; action_type: string; status: string;
  reason?: string | null; requested_by?: string | null;
  payload?: Record<string, unknown> | null };

const STATUS_STYLE: Record<string, string> = {
  PENDING: "bg-amber-500/20 text-amber-300",
  APPROVED: "bg-emerald-500/20 text-emerald-300",
  REJECTED: "bg-red-500/20 text-red-300",
};

export default function ApprovalsPage() {
  const cid = useActiveCompanyId();
  const { data, error, refresh } = useResource(
    cid ? `approvals:${cid}` : null,
    async () => {
      const rows = await api.get<ApprovalRequest[]>(
        `/intervention/requests?company_id=${cid}`);
      return { cid: cid as string,
               rows: Array.isArray(rows) ? rows : [] };
    },
  );
  const active = data && data.cid === cid ? data : null;
  const pending = (active?.rows ?? []).filter((r) => r.status === "PENDING");
  const decided = (active?.rows ?? []).filter((r) => r.status !== "PENDING");

  const decide = async (req: ApprovalRequest, decision: "approve" | "reject") => {
    if (!cid) return;
    try {
      await api.post(`/intervention/requests/${req.id}/decide`,
        { decision });
      notify("success", decision === "approve" ? "已批准" : "已拒绝");
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "操作失败"));
    }
  };

  const payloadLine = (p?: Record<string, unknown> | null) => {
    if (!p) return "";
    const cap = p.capability ? String(p.capability) : "";
    const params = (p.params as Record<string, unknown>) || {};
    const cmd = params.cmd || params.command || params.path || "";
    return [cap, String(cmd)].filter(Boolean).join(" ");
  };

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">待我审批</div>
      <p className="mb-4 text-white/50">
        员工的高危动作、电脑操作审批单都在这里批准或拒绝——批得明明白白，管得清清楚楚。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}

      {pending.length > 0 && (
        <div className="mb-5 grid gap-2">
          {pending.map((r) => (
            <div key={r.id} className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-medium">
                    {r.action_type}
                    <span className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${STATUS_STYLE[r.status] || "bg-white/10"}`}>
                      {r.status}
                    </span>
                  </div>
                  <div className="mt-1 text-white/60">{payloadLine(r.payload) || r.reason}</div>
                  {r.reason && payloadLine(r.payload) && (
                    <div className="text-[12px] text-white/40">理由：{r.reason}</div>
                  )}
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <button onClick={() => decide(r, "approve")}
                          className="rounded-md bg-emerald-600/80 px-3 py-1 hover:bg-emerald-600">批准</button>
                  <button onClick={() => decide(r, "reject")}
                          className="rounded-md bg-red-500/30 px-3 py-1 text-red-200 hover:bg-red-500/40">拒绝</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {pending.length === 0 && cid && (
        <div className="mb-5 rounded-xl border border-sky-500/25 bg-sky-500/[0.06] p-4">
          <div className="font-medium text-sky-200">审批收件箱还是空的——这是好事，也是提醒</div>
          <div className="mt-1.5 space-y-1 text-[12px] text-white/60">
            <div>· 员工执行高危动作（发外呼、动资金、删数据）时会先来这里等你点头；</div>
            <div>· 想看到东西：给员工派一个真实任务，或在「自治运行」调高自治等级，让审批流真正跑起来；</div>
            <div>· 每一笔批准/拒绝都会写进审计台账。</div>
          </div>
          <div className="mt-2 flex gap-2">
            <a href="/" className="rounded-md bg-sky-500/20 px-2.5 py-1 text-[12px] text-sky-200 hover:bg-sky-500/30">去派个任务</a>
            <a href="/autonomous" className="rounded-md bg-white/10 px-2.5 py-1 text-[12px] hover:bg-white/20">自治等级</a>
          </div>
        </div>
      )}

      {decided.length > 0 && (
        <div className="rounded-lg border border-white/10 bg-white/5 p-4">
          <div className="mb-2 font-medium">最近已处理</div>
          {decided.slice(0, 10).map((r) => (
            <div key={r.id} className="flex items-center justify-between py-1 text-white/60">
              <span>{r.action_type} · {payloadLine(r.payload) || r.reason}</span>
              <span className={`rounded px-1.5 py-0.5 text-[11px] ${STATUS_STYLE[r.status] || "bg-white/10"}`}>
                {r.status}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
