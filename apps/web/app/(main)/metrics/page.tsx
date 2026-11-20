"use client";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import {
  autonomyBreakdown,
  formatAutonomy,
  type AutonomyLedger,
} from "@/lib/evidence";

type Metric = { id: string; key: string; name: string; unit: string;
  direction: string; description?: string; spec?: Record<string, unknown> | null };

export default function MetricsPage() {
  const cid = useActiveCompanyId();
  const [form, setForm] = useState({ key: "", name: "", unit: "",
    entity: "order", op: "count", field: "" });
  const [evalResult, setEvalResult] = useState<{ key: string; value?: number;
    grouped?: Record<string, number> | null } | null>(null);

  /* 自主率账本：公司口径，分母相加（不对每个执行的比率求平均） */
  const [days, setDays] = useState(30);
  const [autonomy, setAutonomy] = useState<AutonomyLedger | null>(null);
  const [autonomyError, setAutonomyError] = useState("");

  useEffect(() => {
    if (!cid) { setAutonomy(null); return; }
    let alive = true;
    (async () => {
      try {
        const r = await api.get<AutonomyLedger>(`/metrics/autonomy?company_id=${cid}&days=${days}`);
        if (alive) { setAutonomy(r); setAutonomyError(""); }
      } catch (e) {
        // 没有工作流跑过时这里不该阻断指标页：就地降级为一句说明
        if (alive) { setAutonomy(null); setAutonomyError(describeError(e, "自主率取数失败")); }
      }
    })();
    return () => { alive = false; };
  }, [cid, days]);

  const { data, error, refresh } = useResource(
    cid ? `metrics:${cid}` : null,
    async () => {
      const r = await api.get<{ metrics: Metric[] }>(`/metrics?company_id=${cid}`);
      return { cid: cid as string, rows: r?.metrics ?? [] };
    },
  );
  const active = data && data.cid === cid ? data : null;
  const rows = active?.rows ?? [];

  const create = async () => {
    if (!cid) return;
    try {
      const spec: Record<string, unknown> = { entity: form.entity, op: form.op };
      if (form.op !== "count") spec.field = form.field;
      await api.post(`/metrics?company_id=${cid}`, {
        key: form.key, name: form.name, unit: form.unit, spec });
      notify("success", `指标「${form.name}」已定义`);
      setForm({ key: "", name: "", unit: "", entity: form.entity,
                op: form.op, field: "" });
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "定义失败"));
    }
  };

  const evaluate = async (m: Metric) => {
    if (!cid) return;
    try {
      const r = await api.post<{ key: string; value?: number;
        grouped?: Record<string, number> | null }>(
        `/metrics/${m.key}/evaluate?company_id=${cid}`, {});
      setEvalResult({ ...r, key: m.key });
    } catch (e) {
      notify("error", describeError(e, "求值失败"));
    }
  };

  const remove = async (m: Metric) => {
    if (!cid) return;
    try {
      await api.del(`/metrics/${m.key}?company_id=${cid}`);
      notify("success", `已删除 ${m.key}`);
      setEvalResult(null);
      await refresh();
    } catch (e) {
      notify("error", describeError(e));
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">指标中心</div>
      <p className="mb-4 text-white/50">
        指标 = 结构化口径（实体+聚合+过滤）。AI 经 metrics_query 工具取数永远走这里——数字对得上，不靠猜。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}

      {cid && (
        <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="font-medium">AI 自主率</span>
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}
                    className="rounded-md bg-black/30 px-2 py-1 text-[12px]">
              {[7, 30, 90].map((d) => <option key={d} value={d}>近 {d} 天</option>)}
            </select>
            <span className="text-[12px] text-white/40">
              口径：AI 完成步骤 ÷（AI 步骤 + 人工节点 + 手记介入）
            </span>
          </div>
          {autonomyError ? (
            <div className="text-[12px] text-white/40">{autonomyError}</div>
          ) : (
            <>
              <div className="text-[20px] font-semibold tabular-nums">
                {formatAutonomy(autonomy)}
              </div>
              <div className="text-[12px] text-white/50">
                {autonomy ? `覆盖 ${autonomy.executions ?? 0} 次执行 · ${autonomyBreakdown(autonomy)}` : "—"}
              </div>
            </>
          )}
        </div>
      )}

      {cid && (
        <div className="mb-5 rounded-lg border border-white/10 bg-white/5 p-4">
          <div className="mb-2 font-medium">定义新指标</div>
          <div className="flex flex-wrap gap-2">
            <input value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })}
                   placeholder="key（如 total_sales）" className="rounded-md bg-black/30 px-2 py-1.5" />
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                   placeholder="名称" className="rounded-md bg-black/30 px-2 py-1.5" />
            <input value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })}
                   placeholder="单位" className="w-20 rounded-md bg-black/30 px-2 py-1.5" />
            <select value={form.entity} onChange={(e) => setForm({ ...form, entity: e.target.value })}
                    className="rounded-md bg-black/30 px-2 py-1.5">
              {["lead", "contact", "account", "order", "product", "voucher",
                "employee", "workorder"].map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            <select value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}
                    className="rounded-md bg-black/30 px-2 py-1.5">
              {["count", "sum", "avg", "min", "max"].map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            {form.op !== "count" && (
              <input value={form.field} onChange={(e) => setForm({ ...form, field: e.target.value })}
                     placeholder="字段路径（payload.amount）" className="rounded-md bg-black/30 px-2 py-1.5" />
            )}
            <button onClick={create} className="rounded-md bg-emerald-600/80 px-3 py-1.5 hover:bg-emerald-600">
              定义
            </button>
          </div>
        </div>
      )}

      <div className="grid gap-2">
        {rows.map((m) => (
          <div key={m.id} className="flex items-center justify-between rounded-lg border border-white/10 bg-white/5 p-3">
            <div>
              <div className="font-medium">{m.name}
                <span className="ml-2 text-white/40">{m.key} · {m.unit || "无单位"} · {m.direction === "lower_better" ? "越低越好" : "越高越好"}</span>
              </div>
              {m.description && <div className="text-[12px] text-white/50">{m.description}</div>}
            </div>
            <div className="flex shrink-0 gap-1.5">
              <button onClick={() => evaluate(m)}
                      className="rounded-md bg-white/15 px-3 py-1 hover:bg-white/25">求值</button>
              <button onClick={() => remove(m)}
                      className="rounded-md bg-red-500/20 px-3 py-1 text-red-300 hover:bg-red-500/30">删除</button>
            </div>
          </div>
        ))}
        {!rows.length && cid && (
          <div className="rounded-xl border border-sky-500/25 bg-sky-500/[0.06] p-4">
            <div className="font-medium text-sky-200">三步点亮指标中心</div>
            <div className="mt-1.5 space-y-1 text-[12px] text-white/60">
              <div>① 去「行业中心」装一个行业包 → 会预配一组行业指标（如餐饮的门店客流）；</div>
              <div>② 去「连接器」接数据源（或先用手工报数）；</div>
              <div>③ 回这里给每个指标补 spec（实体/聚合/字段）→ 员工就能用 metrics_query 取数了。</div>
            </div>
            <div className="mt-2 flex gap-2">
              <a href="/industry-hub" className="rounded-md bg-sky-500/20 px-2.5 py-1 text-[12px] text-sky-200 hover:bg-sky-500/30">去行业中心</a>
              <a href="/connectors" className="rounded-md bg-white/10 px-2.5 py-1 text-[12px] hover:bg-white/20">去连接器</a>
            </div>
          </div>
        )}
      </div>

      {evalResult && (
        <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-4">
          <div className="font-medium text-emerald-300">
            {evalResult.key} =
            {evalResult.grouped
              ? Object.entries(evalResult.grouped).map(([k, v]) => `${k}:${v}`).join("  ")
              : (evalResult.value ?? "—")}
          </div>
        </div>
      )}
    </div>
  );
}
