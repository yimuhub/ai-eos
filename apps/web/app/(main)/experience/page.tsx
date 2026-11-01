"use client";

// 经验池
//
// 一个 Agent 踩过的坑、验证过的步骤，不该只留在它自己的记忆里。
// 这一页把「经验」当成公司资产来展示：
//   · 成功经验 → 附一条已发布的工作流，下次同类任务直接复用；
//   · 失败教训 → 记录失败次数与规避点，不再重复交学费；
//   · 任意员工都可以把同事的经验「学进记忆」，运行时按 reuse_key 优先注入。
//
// 接口：
//   GET  /experiences?agent_id=&kind=&reuse_key=
//   POST /experiences/{id}/adopt  { agent_id }

import { useMemo, useState } from "react";
import Link from "next/link";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type Experience = {
  id: string;
  agent_id: string;
  author_agent_name?: string | null;
  task_id: string;
  task_title: string;
  reuse_key: string;
  kind: "success" | "failure" | string;
  scope: string;
  status: string;
  summary?: string | null;
  steps: unknown[];
  pitfalls: unknown[];
  lessons: unknown[];
  token_tips: unknown[];
  quality_tips: unknown[];
  tags: string[];
  attempts: number;
  success_count: number;
  failure_count: number;
  tokens_used: number;
  tokens_saved_est: number;
  confidence: number;
  workflow_id?: string | null;
  workflow_version?: number | null;
  adopted_by: string[];
  source: string;
  created_at?: string | null;
};

type ExperienceOverview = {
  company_id: string;
  items: Experience[];
  stats: {
    total: number;
    success: number;
    failure: number;
    adopted: number;
    tokens_saved_est: number;
  };
};

type AgentOption = { id: string; name: string; role?: string | null };

function fmtTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function StatCard({ label, value, sub, color }: {
  label: string; value: string | number; sub?: string; color?: string;
}) {
  return (
    <div className="card">
      <div className="text-xs" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="text-[26px] font-bold tabular-nums mt-0.5" style={{ color: color ?? "var(--foreground)" }}>
        {value}
      </div>
      {sub && <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{sub}</div>}
    </div>
  );
}

function Bullets({ title, items, tone }: { title: string; items: unknown[]; tone?: string }) {
  if (!items?.length) return null;
  return (
    <div>
      <div className="text-[11px] font-medium mb-1" style={{ color: tone ?? "var(--muted)" }}>{title}</div>
      <ul className="space-y-1">
        {items.map((item, i) => (
          <li key={i} className="flex gap-2 text-xs leading-5">
            <span style={{ color: "var(--muted)" }}>·</span>
            <span>{typeof item === "string" ? item : JSON.stringify(item)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ExperiencePage() {
  const cid = useActiveCompanyId();
  const [kind, setKind] = useState("");
  const [agentId, setAgentId] = useState("");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [adoptFor, setAdoptFor] = useState("");
  const [adopting, setAdopting] = useState(false);

  const agents = useResource<AgentOption[]>(
    cid ? `agents:${cid}` : null,
    () => api.get<AgentOption[]>("/agents"),
  );

  const params = useMemo(() => {
    const q = new URLSearchParams();
    if (agentId) q.set("agent_id", agentId);
    if (kind) q.set("kind", kind);
    q.set("limit", "200");
    return q.toString();
  }, [agentId, kind]);

  const exp = useResource<ExperienceOverview>(
    cid ? `experiences:${cid}:${params}` : null,
    () => api.get<ExperienceOverview>(`/experiences?${params}`),
  );

  const items = useMemo(() => exp.data?.items ?? [], [exp.data]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((e) =>
      [e.task_title, e.reuse_key, e.summary ?? "", ...(e.tags ?? []), e.author_agent_name ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(q),
    );
  }, [items, query]);

  const selected = useMemo(
    () => filtered.find((e) => e.id === selectedId) ?? items.find((e) => e.id === selectedId) ?? null,
    [filtered, items, selectedId],
  );

  const stats = exp.data?.stats;

  async function adopt() {
    if (!selected || adopting) return;
    const target = adoptFor || selected.agent_id;
    if (!target) {
      notify("error", "请选择要学习的员工");
      return;
    }
    setAdopting(true);
    try {
      const res = await api.post<Experience>(`/experiences/${selected.id}/adopt`, {
        agent_id: target,
      });
      const name = agents.data?.find((a) => a.id === target)?.name ?? "该员工";
      notify("success", `已把这条经验学进 ${name} 的记忆（当前 ${res.adopted_by?.length ?? 0} 人掌握）`);
      exp.refresh();
    } catch (e) {
      notify("error", describeError(e, "学习经验失败"));
    } finally {
      setAdopting(false);
    }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-5">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">🧩 经验池</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            成功路径固化成工作流，失败教训变成规避清单；同事的经验可以一键学进记忆。
          </p>
        </div>
        <button onClick={() => exp.refresh()} className="btn-ghost text-xs">刷新</button>
      </div>

      {!cid && <p className="text-sm" style={{ color: "var(--muted)" }}>加载中…</p>}

      {exp.error && (
        <div
          className="flex items-center gap-2.5 px-4 py-3 rounded-xl text-sm"
          style={{ background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.3)", color: "#fca5a5" }}
        >
          <span>🔌</span>
          <span className="flex-1">读取经验池失败：{exp.error}</span>
          <button onClick={() => exp.refresh()} className="btn-ghost !py-1 text-xs">重试</button>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
        <StatCard label="经验总数" value={stats?.total ?? 0} sub="已发布的沉淀" />
        <StatCard label="成功路径" value={stats?.success ?? 0} color="#34d399" sub="可复用工作流" />
        <StatCard label="失败教训" value={stats?.failure ?? 0} color="#f87171" sub="规避清单" />
        <StatCard label="已掌握人次" value={stats?.adopted ?? 0} color="#a5b4fc" sub="跨员工学习" />
        <StatCard
          label="预估省下 Token"
          value={(stats?.tokens_saved_est ?? 0).toLocaleString()}
          color="#fbbf24"
          sub="按重试浪费估算"
        />
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <input
          className="input text-xs w-full sm:w-[260px]"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索任务、复用键、标签或员工…"
          aria-label="搜索经验"
        />
        <select value={kind} onChange={(e) => setKind(e.target.value)} className="input w-auto text-xs" aria-label="经验类型">
          <option value="">全部类型</option>
          <option value="success">成功经验</option>
          <option value="failure">失败教训</option>
        </select>
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)} className="input w-auto text-xs" aria-label="员工">
          <option value="">全部员工</option>
          {(agents.data ?? []).map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
        {(kind || agentId || query) && (
          <button
            onClick={() => { setKind(""); setAgentId(""); setQuery(""); }}
            className="btn-ghost !py-1 text-xs"
          >
            清空筛选
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-3 items-start">
        <div className="lg:col-span-2 space-y-2">
          {exp.loading && items.length === 0 && (
            <p className="text-xs" style={{ color: "var(--muted)" }}>加载中…</p>
          )}
          {!exp.loading && filtered.length === 0 && (
            <div className="card card-static p-4 text-xs" style={{ color: "var(--muted)" }}>
              {items.length === 0
                ? "还没有沉淀经验。员工完成任务（成功或失败两次以上）后会自动生成。"
                : "没有匹配的经验，换个关键词试试。"}
            </div>
          )}
          {filtered.map((e) => {
            const active = e.id === selected?.id;
            const isSuccess = e.kind === "success";
            return (
              <button
                key={e.id}
                onClick={() => { setSelectedId(e.id); setAdoptFor(""); }}
                className="w-full text-left card card-static p-3 space-y-1.5 transition-colors"
                style={{ borderColor: active ? "var(--accent)" : "var(--border)" }}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-1.5 flex-wrap min-w-0">
                    <span
                      className="badge shrink-0"
                      style={{
                        borderColor: isSuccess ? "rgba(52,211,153,0.45)" : "rgba(248,113,113,0.45)",
                        color: isSuccess ? "#6ee7b7" : "#fca5a5",
                      }}
                    >
                      {isSuccess ? "成功" : "失败"}
                    </span>
                    <span className="text-xs font-medium truncate">{e.task_title}</span>
                  </div>
                  <span className="text-[10px] shrink-0" style={{ color: "var(--muted)" }}>
                    {Math.round(e.confidence * 100)}%
                  </span>
                </div>
                <div className="text-[11px] leading-5 line-clamp-2" style={{ color: "var(--muted)" }}>
                  {e.summary || "—"}
                </div>
                <div className="flex items-center gap-2 flex-wrap text-[10px]" style={{ color: "var(--muted)" }}>
                  <span>{e.author_agent_name || "—"}</span>
                  <span>· 尝试 {e.attempts} 次</span>
                  <span>· {fmtTime(e.created_at)}</span>
                  {e.workflow_id && <span style={{ color: "#a5b4fc" }}>· 工作流 v{e.workflow_version}</span>}
                </div>
              </button>
            );
          })}
        </div>

        <div className="lg:col-span-3">
          {!selected ? (
            <div className="card card-static p-6 text-xs text-center" style={{ color: "var(--muted)" }}>
              从左侧选一条经验查看完整步骤。
            </div>
          ) : (
            <div className="card card-static p-4 space-y-3">
              <div className="flex items-start justify-between gap-3 flex-wrap">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className="badge"
                      style={{
                        borderColor: selected.kind === "success" ? "rgba(52,211,153,0.45)" : "rgba(248,113,113,0.45)",
                        color: selected.kind === "success" ? "#6ee7b7" : "#fca5a5",
                      }}
                    >
                      {selected.kind === "success" ? "成功经验" : "失败教训"}
                    </span>
                    <h2 className="font-medium text-sm">{selected.task_title}</h2>
                  </div>
                  <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                    {selected.author_agent_name || "—"} · 复用键 <code>{selected.reuse_key}</code>
                  </div>
                </div>
                {selected.workflow_id && (
                  <Link
                    href={`/workflow-builder?open=${selected.workflow_id}`}
                    className="btn-ghost text-xs shrink-0"
                  >
                    打开工作流 v{selected.workflow_version} →
                  </Link>
                )}
              </div>

              {selected.summary && (
                <p className="text-sm leading-6">{selected.summary}</p>
              )}

              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-center">
                {[
                  { k: "尝试", v: selected.attempts },
                  { k: "成功", v: selected.success_count },
                  { k: "失败", v: selected.failure_count },
                  { k: "Token", v: selected.tokens_used.toLocaleString() },
                ].map((x) => (
                  <div key={x.k} className="rounded-lg px-2 py-1.5"
                    style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                    <div className="text-[10px]" style={{ color: "var(--muted)" }}>{x.k}</div>
                    <div className="text-sm font-semibold tabular-nums">{x.v}</div>
                  </div>
                ))}
              </div>

              <Bullets title="可复用步骤" items={selected.steps} />
              <Bullets title="踩过的坑" items={selected.pitfalls} tone="#fca5a5" />
              <Bullets title="经验教训" items={selected.lessons} />
              <Bullets title="省 Token 的做法" items={selected.token_tips} tone="#fcd34d" />
              <Bullets title="提质量的做法" items={selected.quality_tips} tone="#6ee7b7" />

              {(selected.tags?.length ?? 0) > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {selected.tags.map((t, i) => (
                    <span key={i} className="text-[10px] px-2 py-0.5 rounded"
                      style={{ background: "rgba(99,102,241,0.12)", color: "#c7d2fe" }}>
                      #{t}
                    </span>
                  ))}
                </div>
              )}

              <div className="pt-2 border-t space-y-2" style={{ borderColor: "var(--border)" }}>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                    已掌握 {selected.adopted_by?.length ?? 0} 人 · 选一位员工学进记忆
                  </span>
                  <select
                    value={adoptFor || selected.agent_id}
                    onChange={(e) => setAdoptFor(e.target.value)}
                    className="input w-auto text-xs"
                    aria-label="选择要学习的员工"
                  >
                    {(agents.data ?? []).map((a) => (
                      <option key={a.id} value={a.id}>{a.name}</option>
                    ))}
                    {!agents.data?.some((a) => a.id === selected.agent_id) && (
                      <option value={selected.agent_id}>{selected.author_agent_name || "原作者"}</option>
                    )}
                  </select>
                  <button
                    onClick={adopt}
                    disabled={adopting}
                    className="btn-primary !py-1.5 !px-4 text-xs disabled:opacity-40"
                  >
                    {adopting ? "学习中…" : "学进记忆"}
                  </button>
                </div>
                <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                  学习后，同类任务运行时这条经验会优先注入；不修改原经验。
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
