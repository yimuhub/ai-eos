"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, describeError } from "@/lib/api";
import { formatTokenRatio, formatWinRate } from "@/lib/evidence";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type TierRow = {
  cases: number;
  multi_better: number;
  tie: number;
  baseline_better: number;
  unavailable: number;
  verified_cases: number;
  manual_cases: number;
  unavailable_cases: number;
  multi_tokens: number;
  baseline_tokens: number;
  win_rate: number | null;
  manual_win_rate: number | null;
  token_ratio: number | null;
};

type Summary = {
  suite_key: string;
  total: number;
  verified_total: number;
  manual_total: number;
  unavailable_total: number;
  by_tier: Record<string, TierRow>;
  disclaimer: string;
};

type Workflow = { id: string; name: string; status: string; version: number };
type ChatModel = {
  id: string; name: string; model_id: string; company_id: string | null;
  model_type: string | null; enabled: boolean | null;
};
type UsageTotals = {
  input_tokens: number; output_tokens: number; total_tokens: number;
  cost: number; latency_ms: number;
};
type BlindOrder = {
  order: number; mapping: Record<string, string>; available: boolean;
  raw_response: unknown; raw_winner: string | null;
  normalized_winner: string | null; prompt_hash: string;
  error: { type: string; message: string } | null;
};
type Evidence = {
  stage?: string;
  reason?: string;
  source_execution_id?: string;
  source_status?: string;
  source_output_node?: string;
  source_task_ids?: string[];
  baseline_task_id?: string;
  judge_task_id?: string;
  model?: { id: string; name: string; provider_type: string; model_id: string };
  source_steps?: Array<{
    identity: string; id?: string | null; node_key: string; attempt: number;
    node_type: string; status: string; task_id?: string; output_hash: string;
  }>;
  outputs?: { multi: string; baseline: string };
  usage?: {
    source: UsageTotals; baseline: UsageTotals; judge: UsageTotals; total: UsageTotals;
    persisted_rows: Array<Record<string, unknown>>;
  };
};
type EvalRow = {
  id: string; company_id: string; case_key: string; suite_key: string;
  model_tier: string; workflow_id: string | null; verdict: string;
  run_kind: string; status: string; verification: string; stage?: string | null;
  error: string | null; blind_order: string | null;
  scores: {
    multi?: number; baseline?: number; multi_tokens?: number;
    baseline_tokens?: number; multi_latency_ms?: number; baseline_latency_ms?: number;
  } | null;
  hashes: {
    prompt: string | null; workflow_snapshot: string | null;
    multi_output: string | null; baseline_output: string | null;
    content_fingerprint: string | null;
  };
  source_execution_id: string | null;
  source_execution_path: string | null;
  baseline_task_id: string | null;
  judge_model_id: string | null;
  judge_model_hash: string | null;
  judge_prompt_hash: string | null;
  rubric_version: string | null;
  blind_evidence: {
    available: boolean; status: string; mode: string; agreed: boolean | null;
    order1: string | null; order2: string | null; orders: BlindOrder[];
    reason?: string | null;
  } | null;
  evidence: Evidence | null;
  tokens: number; cost: number; latency_ms: number;
  created_at: string | null;
};

const TERMINAL = new Set(["completed", "unavailable", "failed", "manual"]);
const EMPTY_EVAL_FORM = {
  case_key: "", workflow_id: "", model_id: "", prompt: "", input_json: "",
};
const STAGE_LABEL: Record<string, string> = {
  queued: "排队中", source: "执行源工作流", baseline: "执行单 Agent 基线",
  judging: "双向盲评中", completed: "评测完成", unavailable: "评测不可用",
  failed: "评测失败", manual: "手工规则录入",
};
const VERDICT_LABEL: Record<string, string> = {
  multi_better: "多 Agent 更好", baseline_better: "基线更好", tie: "平局",
  unavailable: "不可用",
};

function statusLabel(row: EvalRow) {
  return STAGE_LABEL[row.stage || row.status] || row.status;
}
function shortHash(value: string | null | undefined) {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "—";
}
function rawText(value: unknown) {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function HashRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-white/40">{label}</div>
      <code className="block truncate text-[11px]" title={value || undefined}>{shortHash(value)}</code>
    </div>
  );
}

function RunEvidence({ row }: { row: EvalRow }) {
  const evidence = row.evidence || {};
  const blind = row.blind_evidence;
  const usage = evidence.usage;
  return (
    <div className="mt-3 space-y-3 rounded-md border border-white/10 bg-black/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{statusLabel(row)}</span>
        <span className="text-white/50">
          {TERMINAL.has(row.status) ? (VERDICT_LABEL[row.verdict] || row.verdict) : "结果待完成"}
        </span>
        {row.verification === "verified_blind" && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">已验证双向盲评</span>
        )}
        {row.verification === "manual_unverified" && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">手工 / 未验证</span>
        )}
        {row.status === "unavailable" && (
          <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-300">不可用</span>
        )}
      </div>
      {row.error && <div className="text-[12px] text-red-300">{row.error}</div>}

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <HashRow label="Prompt SHA256" value={row.hashes.prompt} />
        <HashRow label="工作流快照 SHA256" value={row.hashes.workflow_snapshot} />
        <HashRow label="多 Agent 产出 SHA256" value={row.hashes.multi_output} />
        <HashRow label="基线产出 SHA256" value={row.hashes.baseline_output} />
        <HashRow label="内容指纹" value={row.hashes.content_fingerprint} />
        <HashRow label="Rubric 版本" value={row.rubric_version} />
        <HashRow label="评审模型 ID" value={row.judge_model_id} />
        <HashRow label="评审模型 SHA256" value={row.judge_model_hash} />
        <HashRow label="评审提示词 SHA256" value={row.judge_prompt_hash} />
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[12px] text-white/60">
        {row.source_execution_id ? (
          <span>
            源执行 <code>{row.source_execution_id}</code>
            {row.source_execution_path && (
              <> · <Link className="text-emerald-300 underline" href={row.source_execution_path}>打开源工作流</Link></>
            )}
          </span>
        ) : <span>无源执行（手工记录）</span>}
        {row.baseline_task_id && <span>基线任务 <code>{row.baseline_task_id}</code></span>}
        {evidence.judge_task_id && <span>评审任务 <code>{evidence.judge_task_id}</code></span>}
      </div>
      {!!evidence.source_task_ids?.length && (
        <div className="text-[11px] text-white/45">
          源任务：{evidence.source_task_ids.map((id) => <code key={id} className="mr-2">{id}</code>)}
        </div>
      )}

      <div>
        <div className="mb-1 text-[12px] text-white/55">双向盲评证据</div>
        {!blind || !blind.orders.length ? (
          <div className="text-[12px] text-white/40">
            {row.verification === "manual_unverified"
              ? "手工粘贴产出只做规则复算，不隐藏来源，也没有双向盲评。"
              : `尚无可用盲评证据${blind?.reason ? `（${blind.reason}）` : ""}`}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="text-white/35">
                <tr><th className="py-1">顺序</th><th>映射</th><th>原始返回</th><th>归一化</th><th>Prompt SHA256</th><th>状态</th></tr>
              </thead>
              <tbody>
                {blind.orders.map((order) => (
                  <tr key={order.order} className="border-t border-white/5 align-top">
                    <td className="py-1">{order.order}</td>
                    <td>A→{order.mapping.A} / B→{order.mapping.B}</td>
                    <td className="max-w-40 break-all">{rawText(order.raw_response)}</td>
                    <td>{order.normalized_winner || "—"}</td>
                    <td><code title={order.prompt_hash}>{shortHash(order.prompt_hash)}</code></td>
                    <td>{order.available ? "有效" : `不可用：${order.error?.message || "格式错误"}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-1 text-white/45">
              一致性：{blind.agreed === null ? "未形成" : blind.agreed ? "一致" : "有效分歧（按平局记录，不伪造胜负）"}
            </div>
          </div>
        )}
      </div>

      {usage && (
        <div className="grid gap-2 text-[11px] sm:grid-cols-4">
          {(["source", "baseline", "judge", "total"] as const).map((key) => (
            <div key={key} className="rounded bg-white/5 p-2">
              <div className="text-white/40">{{ source: "源工作流", baseline: "基线", judge: "盲评", total: "合计" }[key]}</div>
              <div>{usage[key].total_tokens} tokens</div>
              <div className="text-white/45">{usage[key].latency_ms} ms · ¥{usage[key].cost.toFixed(6)}</div>
            </div>
          ))}
        </div>
      )}

      {!!evidence.source_steps?.length && (
        <details>
          <summary className="cursor-pointer text-white/50">源执行步骤与任务（{evidence.source_steps.length}）</summary>
          <div className="mt-1 space-y-1">
            {evidence.source_steps.map((step) => (
              <div key={step.identity} className="break-all text-[11px] text-white/45">
                <code>{step.id || step.identity}</code> · {step.node_key} · 第 {step.attempt} 次 · {step.status}
                {step.task_id ? ` · task ${step.task_id}` : ""}
              </div>
            ))}
          </div>
        </details>
      )}
      {evidence.outputs && (
        <details>
          <summary className="cursor-pointer text-white/50">查看实际产出</summary>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 text-[11px]">{evidence.outputs.multi}</pre>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 text-[11px]">{evidence.outputs.baseline}</pre>
          </div>
        </details>
      )}
    </div>
  );
}

export default function EvalsPage() {
  const cid = useActiveCompanyId();
  const cidRef = useRef(cid);
  const generationRef = useRef(0);
  cidRef.current = cid;
  const requestIsCurrent = useCallback((requestCid: string, generation: number) => (
    cidRef.current === requestCid && generationRef.current === generation
  ), []);
  const [suite, setSuite] = useState("default");
  const workflows = useResource<Workflow[]>(
    cid ? `eval-workflows:${cid}` : null,
    () => api.get<Workflow[]>(`/workflows?company_id=${cid}`),
  );
  const models = useResource<ChatModel[]>(
    cid ? `eval-models:${cid}` : null,
    async () => {
      const rows = await api.get<ChatModel[]>(`/models?company_id=${cid}`);
      return rows.filter((m) => m.enabled !== false && (!m.model_type || m.model_type === "text"));
    },
  );
  const summary = useResource<Summary>(
    cid ? `evals:${cid}:${suite}` : null,
    () => api.get<Summary>(`/evals/summary?company_id=${cid}&suite_key=${encodeURIComponent(suite)}`),
  );
  const history = useResource<{ items: EvalRow[]; total: number }>(
    cid ? `eval-history:${cid}:${suite}` : null,
    () => api.get<{ items: EvalRow[]; total: number }>(
      `/evals?company_id=${cid}&suite_key=${encodeURIComponent(suite)}&limit=50`),
  );

  const [formState, setFormState] = useState({
    company_id: cid, values: EMPTY_EVAL_FORM,
  });
  const form = formState.company_id === cid ? formState.values : EMPTY_EVAL_FORM;
  const setForm = (values: typeof form) => setFormState({ company_id: cid, values });
  const [busy, setBusy] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [active, setActive] = useState<EvalRow | null>(null);
  const refreshRef = useRef(history.refresh);
  refreshRef.current = history.refresh;

  useEffect(() => {
    generationRef.current += 1;
    setBusy(false);
    setActive(null);
    setActiveId(null);
    setFormState((current) => current.company_id === cid ? current : {
      company_id: cid, values: EMPTY_EVAL_FORM,
    });
  }, [cid]);

  const pollId = active?.company_id === cid ? activeId : null;
  useEffect(() => {
    if (!pollId || !cid) return;
    const requestCid = cid;
    const generation = generationRef.current;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const row = await api.get<EvalRow>(`/evals/${pollId}`, {
          signal: controller.signal, silent: true,
        });
        if (stopped || !requestIsCurrent(requestCid, generation)
            || row.company_id !== requestCid) return;
        setActive(row);
        if (TERMINAL.has(row.status)) {
          refreshRef.current();
          return;
        }
        timer = setTimeout(poll, 1500);
      } catch (e) {
        if (stopped || !requestIsCurrent(requestCid, generation)) return;
        notify("error", describeError(e, "读取评测进度失败"));
      }
    };
    void poll();
    return () => {
      stopped = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [pollId, cid, requestIsCurrent]);

  const launch = useCallback(async () => {
    if (!cid || busy || workflows.loading || models.loading) return;
    const workflowId = form.workflow_id || workflows.data?.[0]?.id || "";
    const modelId = form.model_id || models.data?.find((m) => m.company_id === cid)?.id || models.data?.[0]?.id || "";
    if (!form.case_key.trim() || !workflowId || !modelId || !form.prompt.trim()) {
      notify("error", "case key、源工作流、对话模型和 prompt 都要填写");
      return;
    }
    let inputData: Record<string, unknown> = {};
    if (form.input_json.trim()) {
      try {
        const parsed = JSON.parse(form.input_json);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
        inputData = parsed;
      } catch {
        notify("error", "额外输入必须是 JSON 对象");
        return;
      }
    }
    const requestCid = cid;
    const generation = generationRef.current;
    setBusy(true);
    try {
      const row = await api.post<EvalRow>("/evals/run", {
        company_id: requestCid, suite_key: suite, case_key: form.case_key.trim(),
        workflow_id: workflowId, model_id: modelId, prompt: form.prompt,
        input_data: inputData,
      });
      if (!requestIsCurrent(requestCid, generation) || row.company_id !== requestCid) return;
      setActive(row);
      setActiveId(row.id);
      history.refresh();
      summary.refresh();
      notify(row.status === "unavailable" ? "error" : "success",
        row.status === "unavailable" ? row.error || "评测不可用" : "真实评测已启动");
    } catch (e) {
      if (!requestIsCurrent(requestCid, generation)) return;
      notify("error", describeError(e, "启动评测失败"));
    } finally {
      if (requestIsCurrent(requestCid, generation)) setBusy(false);
    }
  }, [cid, busy, form, suite, workflows.data, workflows.loading, models.data, models.loading, history, summary, requestIsCurrent]);

  const retry = useCallback(async (row: EvalRow) => {
    if (!cid || busy) return;
    const requestCid = cid;
    const generation = generationRef.current;
    setBusy(true);
    try {
      const retried = await api.post<EvalRow>(`/evals/${row.id}/retry`);
      if (!requestIsCurrent(requestCid, generation) || retried.company_id !== requestCid) return;
      setActive(retried);
      setActiveId(retried.id);
      history.refresh();
      summary.refresh();
      notify("success", "已创建新的评测重试记录");
    } catch (e) {
      if (!requestIsCurrent(requestCid, generation)) return;
      notify("error", describeError(e, "重试评测失败"));
    } finally {
      if (requestIsCurrent(requestCid, generation)) setBusy(false);
    }
  }, [cid, busy, history, summary, requestIsCurrent]);

  const [manual, setManual] = useState({
    case_key: "", model_tier: "unknown", multi_text: "", baseline_text: "",
    multi_tokens: 0, baseline_tokens: 0, multi_latency_ms: 0, baseline_latency_ms: 0,
  });
  const recordManual = useCallback(async () => {
    if (!cid || busy) return;
    if (!manual.case_key.trim() || !manual.multi_text.trim() || !manual.baseline_text.trim()) {
      notify("error", "case key、两份产出都要填");
      return;
    }
    const requestCid = cid;
    const generation = generationRef.current;
    setBusy(true);
    try {
      const row = await api.post<EvalRow>("/evals/compare", {
        company_id: requestCid, suite_key: suite, case_key: manual.case_key.trim(),
        model_tier: manual.model_tier, multi_text: manual.multi_text,
        baseline_text: manual.baseline_text,
        multi_tokens: manual.multi_tokens, baseline_tokens: manual.baseline_tokens,
        multi_latency_ms: manual.multi_latency_ms,
        baseline_latency_ms: manual.baseline_latency_ms,
      });
      if (!requestIsCurrent(requestCid, generation) || row.company_id !== requestCid) return;
      setActive(row);
      setActiveId(null);
      history.refresh();
      summary.refresh();
      notify("success", "已录入手工规则记录（未经盲评）");
    } catch (e) {
      if (!requestIsCurrent(requestCid, generation)) return;
      notify("error", describeError(e, "录入失败"));
    } finally {
      if (requestIsCurrent(requestCid, generation)) setBusy(false);
    }
  }, [cid, busy, manual, suite, history, summary, requestIsCurrent]);

  return (
    <div className="mx-auto max-w-6xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">评测中心</div>
      <p className="mb-4 text-white/50">
        真实评测由服务端执行源工作流、单 Agent 基线和双向盲评，并保存哈希、用量与执行证据。
        手工粘贴只复算规则分，不冒充盲评。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}
      {cid && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-white/50">评测集</span>
            <input value={suite} onChange={(e) => setSuite(e.target.value || "default")}
              className="rounded-md bg-black/30 px-2 py-1.5" placeholder="suite_key" />
            <span className="text-[12px] text-white/40">同一 suite 下的 case 才会汇总到一起</span>
          </div>

          <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-medium">启动真实评测</span>
              <span className="text-[12px] text-white/40">服务端异步执行，可轮询进度</span>
            </div>
            {!models.data?.length && (
              <div className="mb-2 rounded bg-red-500/10 p-2 text-red-300">没有可用对话模型，无法启动真实评测。</div>
            )}
            <div className="grid gap-2 md:grid-cols-2">
              <input value={form.case_key} onChange={(e) => setForm({ ...form, case_key: e.target.value })}
                placeholder="case key（如 行业周报-01）" className="rounded-md bg-black/30 px-2 py-1.5" />
              <select value={form.workflow_id || workflows.data?.[0]?.id || ""}
                onChange={(e) => setForm({ ...form, workflow_id: e.target.value })}
                className="rounded-md bg-black/30 px-2 py-1.5">
                {!workflows.data?.length && <option value="">暂无工作流</option>}
                {workflows.data?.map((wf) => <option key={wf.id} value={wf.id}>{wf.name} · v{wf.version}</option>)}
              </select>
              <select value={form.model_id || models.data?.[0]?.id || ""}
                onChange={(e) => setForm({ ...form, model_id: e.target.value })}
                className="rounded-md bg-black/30 px-2 py-1.5">
                {models.data?.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.model_id}</option>)}
              </select>
              <input value={form.input_json} onChange={(e) => setForm({ ...form, input_json: e.target.value })}
                placeholder='额外输入 JSON（可选，如 {"topic":"AI"}）' className="rounded-md bg-black/30 px-2 py-1.5" />
            </div>
            <textarea value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })}
              rows={5} placeholder="评测 prompt（源工作流通过 {{input.prompt}} 读取）"
              className="mt-2 w-full rounded-md bg-black/30 px-2 py-1.5" />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => void launch()}
                disabled={busy || workflows.loading || models.loading || !workflows.data?.length || !models.data?.length}
                className="rounded-md bg-emerald-600/80 px-3 py-1.5 hover:bg-emerald-600 disabled:opacity-40">
                {busy ? "提交中…" : workflows.loading || models.loading ? "加载评测配置…" : "执行真实评测"}
              </button>
              <span className="text-[11px] text-white/40">prompt 与快照哈希固定后才执行；模型不可用时明确返回不可用。</span>
            </div>
            {active?.company_id === cid && (
              <>
                {active.run_kind === "workflow" &&
                  (active.status === "unavailable" || active.status === "failed") && (
                  <button type="button" onClick={() => void retry(active)} disabled={busy}
                    className="mb-2 rounded bg-white/10 px-2 py-1 text-[11px] hover:bg-white/15 disabled:opacity-40">
                    重试（保留原记录）
                  </button>
                )}
                <RunEvidence row={active} />
              </>
            )}
          </div>

          <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-medium">已验证盲评汇总</span>
              <span className="text-[12px] text-white/40">
                共 {summary.data?.total ?? 0} 条 · 已验证 {summary.data?.verified_total ?? 0} · 手工 {summary.data?.manual_total ?? 0} · 不可用 {summary.data?.unavailable_total ?? 0}
              </span>
            </div>
            {summary.error && <div className="text-red-300">{summary.error}</div>}
            {!summary.data?.total ? (
              <div className="text-[12px] text-white/40">这个评测集还没有记录。</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead className="text-white/40"><tr className="text-left">
                    <th className="py-1">模型档</th><th>case</th><th>已验证</th><th>手工</th><th>不可用</th>
                    <th>盲评胜率</th><th>手工规则胜率</th><th>token 比</th>
                  </tr></thead>
                  <tbody>{Object.entries(summary.data.by_tier).map(([tier, t]) => (
                    <tr key={tier} className="border-t border-white/5">
                      <td className="py-1 font-medium">{tier}</td><td>{t.cases}</td>
                      <td>{t.verified_cases}</td><td>{t.manual_cases}</td><td>{t.unavailable_cases}</td>
                      <td>{formatWinRate(t.win_rate)}</td><td>{formatWinRate(t.manual_win_rate)}</td>
                      <td>{formatTokenRatio(t.token_ratio)}</td>
                    </tr>
                  ))}</tbody>
                </table>
                <div className="mt-2 text-[11px] text-white/40">{summary.data.disclaimer}</div>
              </div>
            )}
          </div>

          <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">最近记录</div>
            {history.error && <div className="text-red-300">{history.error}</div>}
            {!history.data?.items.length ? (
              <div className="text-[12px] text-white/40">暂无记录。</div>
            ) : (
              <div className="space-y-1">
                {history.data.items.map((row) => (
                  <button key={row.id} type="button" onClick={() => {
                    setActive(row);
                    setActiveId(TERMINAL.has(row.status) ? null : row.id);
                  }}
                    className="flex w-full flex-wrap items-center gap-2 border-b border-white/5 py-1.5 text-left hover:bg-white/5">
                    <span className="font-medium">{row.case_key}</span>
                    <span className="text-white/45">{row.model_tier}</span>
                    <span>{TERMINAL.has(row.status) ? (VERDICT_LABEL[row.verdict] || row.verdict) : "结果待完成"}</span>
                    <span className={row.status === "unavailable" || row.status === "failed" ? "text-red-300" : "text-white/45"}>
                      {statusLabel(row)}
                    </span>
                    {row.verification === "manual_unverified" && <span className="text-amber-300">手工 / 未验证</span>}
                  </button>
                ))}
              </div>
            )}
          </div>

          <details className="rounded-lg border border-white/10 bg-white/5 p-4">
            <summary className="cursor-pointer font-medium text-amber-200">手工对照录入（未经盲评，不计入盲评胜率）</summary>
            <p className="mt-2 text-[12px] text-white/45">
              仅对粘贴的两份产出复算六维规则分；不隐藏来源、不调用评审模型，也不记录伪造的 A/B 顺序。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <input value={manual.case_key} onChange={(e) => setManual({ ...manual, case_key: e.target.value })}
                placeholder="case key" className="rounded-md bg-black/30 px-2 py-1.5" />
              <input value={manual.model_tier} onChange={(e) => setManual({ ...manual, model_tier: e.target.value })}
                placeholder="模型档" className="w-32 rounded-md bg-black/30 px-2 py-1.5" />
              {(["multi_tokens", "baseline_tokens", "multi_latency_ms", "baseline_latency_ms"] as const).map((key) => (
                <input key={key} type="number" min={0} step={1} value={manual[key]}
                  onChange={(e) => setManual({ ...manual, [key]: Number(e.target.value) })}
                  placeholder={key} className="w-32 rounded-md bg-black/30 px-2 py-1.5" />
              ))}
            </div>
            <div className="mt-2 grid gap-2 md:grid-cols-2">
              <textarea rows={5} value={manual.multi_text} onChange={(e) => setManual({ ...manual, multi_text: e.target.value })}
                placeholder="手工粘贴：多 Agent 产出" className="rounded-md bg-black/30 px-2 py-1.5 font-mono text-[12px]" />
              <textarea rows={5} value={manual.baseline_text} onChange={(e) => setManual({ ...manual, baseline_text: e.target.value })}
                placeholder="手工粘贴：基线产出" className="rounded-md bg-black/30 px-2 py-1.5 font-mono text-[12px]" />
            </div>
            <button type="button" onClick={() => void recordManual()} disabled={busy}
              className="mt-2 rounded-md bg-white/10 px-3 py-1.5 hover:bg-white/15 disabled:opacity-40">复算并录入手工记录</button>
          </details>
        </>
      )}
    </div>
  );
}
