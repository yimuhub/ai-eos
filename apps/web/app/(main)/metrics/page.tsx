"use client";
import { useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type Metric = { id: string; key: string; name: string; unit: string;
  direction: string; description?: string; spec?: Record<string, unknown> | null };

export default function MetricsPage() {
  const cid = useActiveCompanyId();
  const [form, setForm] = useState({ key: "", name: "", unit: "",
    entity: "order", op: "count", field: "" });
  const [evalResult, setEvalResult] = useState<{ key: string; value?: number;
    grouped?: Record<string, number> | null } | null>(null);

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
        {!rows.length && cid && <div className="text-white/40">还没有指标。装行业包会预配一组待接线指标，或在这里手工定义。</div>}
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
