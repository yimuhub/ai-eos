"use client";
import { useCallback, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import {
  formatTokenRatio,
  formatWinRate,
  verdictLabel,
} from "@/lib/evidence";

type TierRow = {
  cases: number;
  multi_better: number;
  tie: number;
  baseline_better: number;
  multi_tokens: number;
  baseline_tokens: number;
  win_rate: number | null;
  token_ratio: number | null;
};

type Summary = {
  suite_key: string;
  total: number;
  by_tier: Record<string, TierRow>;
  disclaimer: string;
};

type EvalRow = {
  id: string; case_key: string; model_tier: string; verdict: string;
  scores: {
    multi?: number; baseline?: number;
    multi_tokens?: number; baseline_tokens?: number;
  } | null;
  created_at: string | null;
};

export default function EvalsPage() {
  const cid = useActiveCompanyId();
  const [suite, setSuite] = useState("default");

  // 按"公司 + suite"取汇总；suite 是普通字符串输入，所以不用防抖，
  // 只在失焦/回车时提交（见下方 onKeyDown）。
  const { data, error, refresh } = useResource<{ cid: string; rows: Summary } | null>(
    cid ? `evals:${cid}:${suite}` : null,
    async () => {
      const r = await api.get<Summary>(
        `/evals/summary?company_id=${cid}&suite_key=${encodeURIComponent(suite)}`);
      return { cid: cid as string, rows: r };
    },
  );
  const active = data && data.cid === cid ? data.rows : null;

  /* 录入一组盲评（确定性六维由后端复算，这里只负责把两份产出交上去） */
  const [form, setForm] = useState({
    case_key: "", model_tier: "large",
    multi_text: "", baseline_text: "",
    multi_tokens: 0, baseline_tokens: 0,
  });
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<EvalRow | null>(null);

  const record = useCallback(async () => {
    if (!cid || busy) return;
    if (!form.case_key.trim() || !form.multi_text.trim() || !form.baseline_text.trim()) {
      notify("error", "case key、两份产出都要填");
      return;
    }
    setBusy(true);
    try {
      const r = await api.post<EvalRow>("/evals/compare", {
        company_id: cid, suite_key: suite,
        case_key: form.case_key.trim(), model_tier: form.model_tier,
        multi_text: form.multi_text, baseline_text: form.baseline_text,
        multi_tokens: Number(form.multi_tokens) || 0,
        baseline_tokens: Number(form.baseline_tokens) || 0,
      });
      setLast(r);
      notify("success", `已录入：${verdictLabel(r.verdict)}`);
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "录入失败"));
    } finally {
      setBusy(false);
    }
  }, [cid, busy, form, suite, refresh]);

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">评测中心</div>
      <p className="mb-4 text-white/50">
        同一题、两份产出：多 Agent 流水线 vs 单次基线，隐藏来源做规则复算。
        胜率只统计有明确胜负的 case，平局不进分母——这是机制，不是结论。
      </p>

      {!cid && <div className="text-white/40">先选择公司。</div>}

      {cid && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-white/50">评测集</span>
            <input
              value={suite}
              onChange={(e) => setSuite(e.target.value || "default")}
              className="rounded-md bg-black/30 px-2 py-1.5"
              placeholder="suite_key"
            />
            <span className="text-[12px] text-white/40">
              同一 suite 下的 case 才会汇总到一起
            </span>
            {error && <span className="text-[12px] text-red-300">{describeError(error, "汇总失败")}</span>}
          </div>

          <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-medium">按模型档汇总</span>
              <span className="text-[12px] text-white/40">共 {active?.total ?? 0} 条</span>
            </div>
            {!active || active.total === 0 ? (
              <div className="text-[12px] text-white/40">
                这个评测集还没有记录。下面录一组，或跑离线脚本 tools/run_eval.py 批量灌入。
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead className="text-white/40">
                    <tr className="text-left">
                      <th className="py-1">模型档</th>
                      <th>case</th>
                      <th>多 Agent 胜</th>
                      <th>平局</th>
                      <th>基线胜</th>
                      <th>胜率</th>
                      <th>token 比</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(active.by_tier).map(([tier, t]) => (
                      <tr key={tier} className="border-t border-white/5">
                        <td className="py-1 font-medium">{tier}</td>
                        <td className="tabular-nums">{t.cases}</td>
                        <td className="tabular-nums">{t.multi_better}</td>
                        <td className="tabular-nums">{t.tie}</td>
                        <td className="tabular-nums">{t.baseline_better}</td>
                        <td className="tabular-nums">{formatWinRate(t.win_rate)}</td>
                        <td className="tabular-nums">{formatTokenRatio(t.token_ratio)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {active.disclaimer && (
                  <div className="mt-2 text-[11px] text-white/40">{active.disclaimer}</div>
                )}
              </div>
            )}
          </div>

          <div className="rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">录入一组对照</div>
            <div className="mb-2 flex flex-wrap gap-2">
              <input value={form.case_key}
                onChange={(e) => setForm({ ...form, case_key: e.target.value })}
                placeholder="case key（如 行业周报-01）"
                className="rounded-md bg-black/30 px-2 py-1.5" />
              <select value={form.model_tier}
                onChange={(e) => setForm({ ...form, model_tier: e.target.value })}
                className="rounded-md bg-black/30 px-2 py-1.5">
                {["small", "medium", "large", "unknown"].map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
              <input type="number" min={0} value={form.multi_tokens}
                onChange={(e) => setForm({ ...form, multi_tokens: Number(e.target.value) })}
                placeholder="多 Agent token"
                className="w-32 rounded-md bg-black/30 px-2 py-1.5" />
              <input type="number" min={0} value={form.baseline_tokens}
                onChange={(e) => setForm({ ...form, baseline_tokens: Number(e.target.value) })}
                placeholder="基线 token"
                className="w-32 rounded-md bg-black/30 px-2 py-1.5" />
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              <textarea value={form.multi_text} rows={6}
                onChange={(e) => setForm({ ...form, multi_text: e.target.value })}
                placeholder="多 Agent 流水线的产出（整段粘贴）"
                className="rounded-md bg-black/30 px-2 py-1.5 font-mono text-[12px]" />
              <textarea value={form.baseline_text} rows={6}
                onChange={(e) => setForm({ ...form, baseline_text: e.target.value })}
                placeholder="单次基线的产出（整段粘贴）"
                className="rounded-md bg-black/30 px-2 py-1.5 font-mono text-[12px]" />
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => void record()} disabled={busy}
                className="rounded-md bg-emerald-600/80 px-3 py-1.5 hover:bg-emerald-600 disabled:opacity-40">
                {busy ? "复算中…" : "复算并录入"}
              </button>
              <span className="text-[11px] text-white/40">
                六维规则分由后端复算，同一个输入永远得到同一个分。
              </span>
            </div>
            {last && (
              <div className="mt-2 text-[12px] text-white/60">
                刚录入 {last.case_key}（{last.model_tier}）：{verdictLabel(last.verdict)}
                {last.scores
                  ? ` · 多 Agent ${Number(last.scores.multi ?? 0).toFixed(2)} / 基线 ${Number(last.scores.baseline ?? 0).toFixed(2)}`
                  : ""}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
