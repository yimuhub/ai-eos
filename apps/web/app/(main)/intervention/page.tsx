"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getCompanies } from "@/lib/api";
import type { Company } from "@/lib/types";
import { notify } from "@/components/Toast";

// ## 改造说明
//
// 本页此前完全不可用，有 4 处叠加缺陷：
//
// 1. 审批请求走裸 fetch，且 URL 拼成 `/requests/{id}/APPROVE`，
//    而后端实际是 `POST /requests/{request_id}/decide` + body `{decision}` → **404**。
// 2. 裸 fetch 没有带 Authorization 头（绕过了 lib/api.ts 的鉴权注入）→ **401**。
// 3. `setMsg("已批准")` 写在 await 之后但**无视响应结果**，
//    请求失败也照样显示"已批准"——这就是"系统会说谎"最典型的形态。
// 4. 列表查询传 `status=WAITING_APPROVAL`，而后端状态值是
//    PENDING/APPROVED/REJECTED；且未传必填的 company_id → **422**。
//
// 现在：全部走 api 层（鉴权 + 公司作用域 + 超时 + 错误规范化），
// 状态值对齐后端，成功由响应决定，失败给出可操作的原因。

type Req = {
  id: string;
  action_type: string;
  status: string;
  reason?: string;
  requested_by?: string;
  /** LLM 生成的自由结构，前端只做展示（JSON.stringify） */
  payload?: Record<string, unknown>;
  created_at?: string;
};

type Policy = {
  level: number;
  approval_actions?: string[];
  is_default?: boolean;
};

type OverrideRow = {
  id: string;
  target_type: string;
  target_id: string;
  original?: Record<string, unknown>;
  override?: Record<string, unknown>;
  reason?: string;
  created_at: string;
};

/** 与后端 models/intervention.py 的 requires_approval() 语义保持一致 */
const LEVELS: { v: number; name: string; desc: string }[] = [
  { v: 0, name: "L0 · 全需确认", desc: "所有动作都必须你点头才执行" },
  { v: 1, name: "L1 · 关键确认", desc: "仅任务完成、成果验收、升级请求需要确认" },
  { v: 2, name: "L2 · 均衡（默认）", desc: "常规事务自治，关键节点上报" },
  { v: 3, name: "L3 · 高度自治", desc: "仅升级请求需要确认" },
  { v: 4, name: "L4 · 全自动", desc: "不再拦截任何动作（请谨慎启用）" },
];

const ACTION_LABELS: Record<string, string> = {
  task_create: "创建任务",
  task_complete: "完成任务",
  meeting_start: "召开会议",
  artifact_approve: "成果验收",
  escalate: "升级请求",
  computer_command: "🖥 高危本机操作（V5.5 审批门）",
  external_agent: "📤 外部 Agent 外发（V5.5 A3 审批门）",
};

export default function InterventionPage() {
  const [companyId, setCompanyId] = useState<string | null>(null);
  const [reqs, setReqs] = useState<Req[]>([]);
  const [overrides, setOverrides] = useState<OverrideRow[]>([]);
  const [policy, setPolicy] = useState<Policy>({ level: 2 });
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [loadError, setLoadError] = useState("");

  // ---------- 读取：公司 → 策略 / 待审批 / 覆盖记录 ----------
  const load = useCallback(async (cid: string) => {
    setLoadError("");
    const [policyRes, reqRes, ovRes] = await Promise.allSettled([
      api.get<Policy>(`/intervention/policy?company_id=${cid}`),
      api.get<Req[]>(`/intervention/requests?company_id=${cid}&status=PENDING`),
      api.get<OverrideRow[]>(`/intervention/overrides?company_id=${cid}`),
    ]);

    if (policyRes.status === "fulfilled") {
      setPolicy(policyRes.value);
    } else {
      // 单独失败不应让整页变成空白：保留默认值并把原因说出来
      setPolicy({ level: 2, is_default: true });
      setLoadError(describe(policyRes.reason, "读取自动化等级失败"));
    }

    if (reqRes.status === "fulfilled") setReqs(reqRes.value || []);
    else setLoadError(describe(reqRes.reason, "读取待审批请求失败"));

    if (ovRes.status === "fulfilled") setOverrides(ovRes.value || []);
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const cs = await getCompanies<Company>();
        const cid = cs?.[0]?.id || null;
        setCompanyId(cid);
        if (cid) await load(cid);
        else setLoadError("你还没有创建 AI 公司，请先到「公司管理」创建。");
      } catch (e) {
        setLoadError(describe(e, "读取公司信息失败"));
      } finally {
        setLoading(false);
      }
    })();
  }, [load]);

  // ---------- 审批：成功以响应为准，不再无条件报喜 ----------
  async function decide(id: string, decision: "approve" | "reject") {
    setBusyId(id);
    try {
      const res = await api.post<{ id: string; status: string; execution?: { ok: boolean; error?: string; result?: any } }>(
        `/intervention/requests/${id}/decide`,
        { decision },
      );
      if (decision === "approve" && res?.execution) {
        notify(res.execution.ok ? "success" : "error",
          res.execution.ok
            ? "已批准，高危命令已自动执行完成"
            : `已批准但执行失败：${res.execution.error || "未知原因"}`);
      } else {
        notify("success", decision === "approve" ? "已批准，Agent 将继续执行" : "已拒绝");
      }
      // 用后端返回的真实状态回填，避免本地乐观更新与后端不一致
      setReqs((prev) =>
        prev.map((r) => (r.id === id ? { ...r, status: res?.status || r.status } : r)),
      );
      // 已决策的不再属于待办列表
      setReqs((prev) => prev.filter((r) => r.id !== id));
      if (companyId) await load(companyId);
    } catch (e) {
      notify("error", describe(e, "审批失败"));
    } finally {
      setBusyId(null);
    }
  }

  // ---------- 策略：真正写回后端并回读确认 ----------
  async function saveLevel(level: number) {
    if (!companyId) return;
    setSavingPolicy(true);
    try {
      const res = await api.put<Policy>(`/intervention/policy?company_id=${companyId}`, {
        level,
        project_id: null,
      });
      setPolicy(res || { level });
      notify("success", `自动化等级已设为 ${LEVELS.find((l) => l.v === level)?.name || `L${level}`}`);
    } catch (e) {
      notify("error", describe(e, "保存自动化等级失败"));
    } finally {
      setSavingPolicy(false);
    }
  }

  // ---------- V5.5 P0：高危本机操作审批门开关 ----------
  const ccEnabled = (policy.approval_actions || []).includes("computer_command");
  // ---------- V5.5 A3：外部 Agent 外发审批门开关 ----------
  const extEnabled = (policy.approval_actions || []).includes("external_agent");
  async function toggleExt(enabled: boolean) {
    if (!companyId) return;
    setSavingPolicy(true);
    try {
      const actions = new Set(policy.approval_actions || []);
      if (enabled) actions.add("external_agent"); else actions.delete("external_agent");
      const res = await api.put<Policy>(`/intervention/policy?company_id=${companyId}`, {
        level: policy.level ?? 2,
        project_id: null,
        approval_actions: Array.from(actions),
      });
      setPolicy(res || policy);
      notify("success", enabled
        ? "已开启：外部 Agent 外发执行需你批准后才执行"
        : "已关闭外部 Agent 外发审批门");
    } catch (e) {
      notify("error", describe(e, "保存外发审批门开关失败"));
    } finally {
      setSavingPolicy(false);
    }
  }
  async function toggleCc(enabled: boolean) {
    if (!companyId) return;
    setSavingPolicy(true);
    try {
      const actions = new Set(policy.approval_actions || []);
      if (enabled) actions.add("computer_command"); else actions.delete("computer_command");
      const res = await api.put<Policy>(`/intervention/policy?company_id=${companyId}`, {
        level: policy.level ?? 2,
        project_id: null,
        approval_actions: Array.from(actions),
      });
      setPolicy(res || policy);
      notify("success", enabled
        ? "已开启：高危本机操作（shell/写文件/启动应用）需你批准后才执行"
        : "已关闭高危操作审批门");
    } catch (e) {
      notify("error", describe(e, "保存审批门开关失败"));
    } finally {
      setSavingPolicy(false);
    }
  }

  const current = LEVELS.find((l) => l.v === policy.level);

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-4">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-[var(--foreground)]">🛑 人工介入</h1>
          <p className="text-xs mt-1 text-[var(--muted)]">
            决定 Agent 在什么情况下必须停下来等你确认。等级越高，自治越多，你的介入越少。
          </p>
        </div>
        <button
          onClick={() => companyId && load(companyId)}
          className="btn-ghost text-xs"
          disabled={!companyId || loading}
        >
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {loadError && (
        <div className="rounded-xl border px-3.5 py-2.5 text-sm"
          style={{ borderColor: "rgba(251,191,36,0.45)", background: "rgba(251,191,36,0.1)" }}>
          ⚠️ {loadError}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ---------- 待审批请求 ---------- */}
        <section className="card card-static p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium text-[var(--foreground)]">待审批请求</h3>
            <span className="badge text-xs">{reqs.length} 条</span>
          </div>

          {loading ? (
            <p className="text-xs text-[var(--muted)]">正在加载…</p>
          ) : !reqs.length ? (
            <div className="text-xs text-[var(--muted)] py-6 text-center leading-6">
              当前没有需要你确认的请求。
              <br />
              当 Agent 触发需要审批的动作时，会出现在这里。
            </div>
          ) : (
            <div className="space-y-2">
              {reqs.map((r) => (
                <div
                  key={r.id}
                  className="rounded-xl p-3 border text-sm text-[var(--foreground)]"
                  style={{ background: "rgba(255,255,255,0.03)", borderColor: "var(--border)" }}
                >
                  <div className="flex justify-between items-start gap-2">
                    <span className="font-medium">
                      {ACTION_LABELS[r.action_type] || r.action_type}
                    </span>
                    <span className="text-xs shrink-0 text-[var(--muted)]">{r.status}</span>
                  </div>

                  {r.reason && (
                    <p className="text-xs mt-1 text-[var(--muted)]">理由：{r.reason}</p>
                  )}

                  {r.payload && (
                    <pre className="text-xs mt-2 rounded-lg p-2 overflow-auto max-h-40 text-[var(--muted)]"
                      style={{ background: "rgba(0,0,0,0.25)" }}>
                      {JSON.stringify(r.payload, null, 2)}
                    </pre>
                  )}

                  <div className="mt-2.5 flex gap-2">
                    <button
                      onClick={() => decide(r.id, "approve")}
                      disabled={busyId === r.id}
                      className="px-3 py-1 rounded-lg text-xs text-white disabled:opacity-50"
                      style={{ background: "#15803d" }}
                    >
                      {busyId === r.id ? "处理中…" : "批准"}
                    </button>
                    <button
                      onClick={() => decide(r.id, "reject")}
                      disabled={busyId === r.id}
                      className="px-3 py-1 rounded-lg text-xs text-white disabled:opacity-50"
                      style={{ background: "#b91c1c" }}
                    >
                      拒绝
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* ---------- 自动化等级 ---------- */}
        <section className="card card-static p-4">
          <h3 className="font-medium text-[var(--foreground)] mb-1">自动化等级</h3>
          <p className="text-xs mb-3 text-[var(--muted)]">
            当前：
            <span className="text-[var(--foreground)]">
              {current ? current.name : `L${policy.level}`}
            </span>
            {policy.is_default && "（系统默认值，尚未自定义）"}
          </p>

          <div className="space-y-2">
            {LEVELS.map((l) => {
              const active = l.v === policy.level;
              return (
                <button
                  key={l.v}
                  onClick={() => saveLevel(l.v)}
                  disabled={!companyId || savingPolicy}
                  className="w-full text-left rounded-xl px-3 py-2.5 border transition-all disabled:opacity-50"
                  style={{
                    borderColor: active ? "var(--accent)" : "var(--border)",
                    background: active ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)",
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-[var(--foreground)]">{l.name}</span>
                    {active && <span className="text-xs" style={{ color: "var(--accent-2)" }}>✓ 生效中</span>}
                  </div>
                  <p className="text-xs mt-0.5 text-[var(--muted)]">{l.desc}</p>
                </button>
              );
            })}
          </div>

          {savingPolicy && (
            <p className="text-xs mt-2 text-[var(--muted)]">正在保存…</p>
          )}
        </section>

        {/* ---------- V5.5 P0：高危本机操作审批门 ---------- */}
        <section className="card card-static p-4">
          <h3 className="font-medium text-[var(--foreground)] mb-1">
            🖥 高危本机操作审批门
          </h3>
          <p className="text-xs mb-3 text-[var(--muted)]">
            开启后，Agent 执行 <b>shell 命令 / 写文件 / 启动应用</b> 前必须先拿到你的批准
            （批准后自动执行）。一人公司老板的成本与安全底线。
          </p>
          <div className="flex items-center justify-between gap-3 rounded-xl px-3 py-2.5 border"
            style={{ borderColor: ccEnabled ? "var(--accent)" : "var(--border)",
                    background: ccEnabled ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)" }}>
            <div>
              <div className="text-sm font-medium text-[var(--foreground)]">
                {ccEnabled ? "✓ 高危操作需你批准" : "× 直接执行（不拦截）"}
              </div>
              <p className="text-xs mt-0.5 text-[var(--muted)]">
                覆盖能力：run_shell · run_write · run_app
              </p>
            </div>
            <button
              onClick={() => toggleCc(!ccEnabled)}
              disabled={!companyId || savingPolicy}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-white disabled:opacity-40"
              style={{ background: ccEnabled ? "#b91c1c" : "#15803d" }}>
              {ccEnabled ? "关闭" : "开启"}
            </button>
          </div>
        </section>

        {/* ---------- V5.5 A3：外部 Agent 外发审批门 ---------- */}
        <section className="card card-static p-4">
          <h3 className="font-medium text-[var(--foreground)] mb-1">
            📤 外部 Agent 外发审批门
          </h3>
          <p className="text-xs mb-3 text-[var(--muted)]">
            开启后，向<b>外部 Agent 服务</b>（外部连接）发任务执行前，
            必须先拿到你的批准——外发内容离开本机前最后一道闸。
          </p>
          <div className="flex items-center justify-between gap-3 rounded-xl px-3 py-2.5 border"
            style={{ borderColor: extEnabled ? "var(--accent)" : "var(--border)",
                    background: extEnabled ? "rgba(99,102,241,0.12)" : "rgba(255,255,255,0.02)" }}>
            <div>
              <div className="text-sm font-medium text-[var(--foreground)]">
                {extEnabled ? "✓ 外发需你批准" : "× 直接外发（不拦截）"}
              </div>
              <p className="text-xs mt-0.5 text-[var(--muted)]">
                覆盖动作：外部 Agent execute（external_agent）
              </p>
            </div>
            <button
              onClick={() => toggleExt(!extEnabled)}
              disabled={!companyId || savingPolicy}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-white disabled:opacity-40"
              style={{ background: extEnabled ? "#b91c1c" : "#15803d" }}>
              {extEnabled ? "关闭" : "开启"}
            </button>
          </div>
        </section>
      </div>

      {/* ---------- 覆盖记录 ---------- */}
      <section className="card card-static p-4">
        <h3 className="font-medium text-[var(--foreground)] mb-3">人工覆盖记录</h3>
        {!overrides.length ? (
          <p className="text-xs text-[var(--muted)]">
            暂无覆盖记录。当你手动推翻 Agent 的决定时，会在这里留下可追溯的痕迹。
          </p>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-xs">
              <thead className="text-[var(--muted)]">
                <tr className="text-left">
                  <th className="py-1.5 pr-3 font-normal">时间</th>
                  <th className="py-1.5 pr-3 font-normal">对象</th>
                  <th className="py-1.5 pr-3 font-normal">原决定</th>
                  <th className="py-1.5 pr-3 font-normal">覆盖为</th>
                  <th className="py-1.5 font-normal">原因</th>
                </tr>
              </thead>
              <tbody className="text-[var(--foreground)]">
                {overrides.map((o) => (
                  <tr key={o.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1.5 pr-3 whitespace-nowrap text-[var(--muted)]">
                      {o.created_at?.slice(0, 16).replace("T", " ")}
                    </td>
                    <td className="py-1.5 pr-3">{o.target_type}</td>
                    <td className="py-1.5 pr-3 max-w-[180px] truncate">
                      {brief(o.original)}
                    </td>
                    <td className="py-1.5 pr-3 max-w-[180px] truncate">
                      {brief(o.override)}
                    </td>
                    <td className="py-1.5 max-w-[240px] truncate text-[var(--muted)]">
                      {o.reason || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

/** 把任意错误转成一句用户能读懂的话 */
function describe(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return fallback;
}

function brief(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  const s = JSON.stringify(v);
  return s.length > 60 ? s.slice(0, 60) + "…" : s;
}
