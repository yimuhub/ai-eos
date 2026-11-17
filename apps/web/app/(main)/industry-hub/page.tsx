"use client";
import { useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type ComplianceItem = { id: string; key: string; name: string; status: string;
  authority?: string | null; expires_at?: string | null; penalty?: string | null };
type WorkflowRow = { id: string; name: string; status: string };
type MetricRow = { key: string; name: string; unit: string; wired: boolean };
type IntegrationTarget = { system: string; auth_type: string; note?: string };
type InstalledPack = { installation_id: string; package_id: string; key: string;
  name: string; version: string; installed_at?: string; description?: string;
  agents_count: number; workflows: WorkflowRow[]; draft_workflows: number;
  metrics: MetricRow[]; metrics_pending: number; compliance: ComplianceItem[];
  integration_targets: IntegrationTarget[] };
type AvailablePack = { id: string; name: string; version: string;
  description: string; already_installed: boolean };

const COMPLIANCE_STYLE: Record<string, string> = {
  valid: "bg-emerald-500/20 text-emerald-300",
  expiring: "bg-amber-500/20 text-amber-300",
  expired: "bg-red-500/20 text-red-300",
  manual: "bg-white/10 text-white/50",
};
const COMPLIANCE_LABEL: Record<string, string> = {
  valid: "有效", expiring: "临期", expired: "已过期", manual: "待登记有效期",
};

export default function IndustryHubPage() {
  const cid = useActiveCompanyId();
  const [busy, setBusy] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const { data, error, refresh } = useResource(
    cid ? `industry-hub:${cid}` : null,
    async () => {
      const r = await api.get<{ installed: InstalledPack[];
        available: AvailablePack[] }>(`/ops/industry-hub?company_id=${cid}`);
      return { cid: cid as string,
               installed: r?.installed ?? [], available: r?.available ?? [] };
    },
  );
  const active = data && data.cid === cid ? data : null;
  const installed = active?.installed ?? [];
  const available = (active?.available ?? [])
    .filter((a) => !installed.some((i) => i.key === a.id));

  const publishWorkflow = async (packKey: string, wf: WorkflowRow) => {
    if (!cid) return;
    setBusy(wf.id);
    try {
      await api.post(
        `/ops/industry-hub/workflows/${wf.id}/publish?company_id=${cid}`, {});
      notify("success", `「${wf.name}」已发布`);
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "发布失败"));
    } finally {
      setBusy(null);
    }
  };

  const refreshCompliance = async (packKey: string) => {
    if (!cid) return;
    setBusy(packKey + ":compliance");
    try {
      const r = await api.post<{ counts: Record<string, number> }>(
        `/ops/compliance/refresh?company_id=${cid}`, {});
      notify("success",
        `合规状态已刷新：有效 ${r.counts.valid ?? 0} · 临期 ${r.counts.expiring ?? 0} · 过期 ${r.counts.expired ?? 0}`);
    } catch (e) {
      notify("error", describeError(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">行业中心</div>
      <p className="mb-4 text-white/50">
        装进基座的行业包给了你什么，在这一屏看完：预配员工、流程图纸、合规台账、指标与连接器蓝图——逐项接好线，行业包才算真正上岗。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}

      {installed.map((p) => {
        const open = !collapsed[p.key];
        return (
          <div key={p.installation_id}
               className="mb-4 rounded-xl border border-white/10 bg-white/5">
            <div className="flex items-start justify-between gap-3 p-4 pb-2">
              <div>
                <div className="text-[14px] font-medium">
                  {p.name}
                  <span className="ml-2 text-white/40">v{p.version}</span>
                  {p.draft_workflows > 0 && (
                    <span className="ml-2 rounded bg-amber-500/20 px-1.5 py-0.5 text-[11px] text-amber-300">
                      {p.draft_workflows} 条流程待发布
                    </span>
                  )}
                  {p.metrics_pending > 0 && (
                    <span className="ml-2 rounded bg-sky-500/20 px-1.5 py-0.5 text-[11px] text-sky-300">
                      {p.metrics_pending} 个指标待接线
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-white/40">
                  安装于 {p.installed_at?.slice(0, 10) || "—"}
                  {p.description ? ` · ${p.description}` : ""}
                </div>
              </div>
              <button onClick={() => setCollapsed({ ...collapsed, [p.key]: open })}
                      className="shrink-0 rounded-md bg-white/10 px-3 py-1 hover:bg-white/20">
                {open ? "收起" : "展开"}
              </button>
            </div>

            {open && (
              <div className="px-4 pb-4">
                {/* 工作流图纸 */}
                <Section title={`流程图纸（${p.workflows.length}）`}
                         extra={p.draft_workflows > 0 ? "审完一条发布一条；全部确认后即可上岗" : "全部已发布"}>
                  <div className="grid gap-1.5 md:grid-cols-2">
                    {p.workflows.map((w) => (
                      <div key={w.id}
                           className="flex items-center justify-between rounded-md bg-black/25 px-3 py-2">
                        <span className="truncate">{w.name}</span>
                        {w.status === "DRAFT" ? (
                          <button disabled={busy === w.id}
                                  onClick={() => publishWorkflow(p.key, w)}
                                  className="shrink-0 rounded-md bg-emerald-600/80 px-2.5 py-1 text-[12px] hover:bg-emerald-600 disabled:opacity-40">
                            {busy === w.id ? "…" : "发布"}
                          </button>
                        ) : (
                          <span className="shrink-0 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[11px] text-emerald-300">已发布</span>
                        )}
                      </div>
                    ))}
                  </div>
                </Section>

                {/* 合规台账 */}
                {p.compliance.length > 0 && (
                  <Section title={`合规台账（${p.compliance.length}）`}
                           extra={<button onClick={() => refreshCompliance(p.key)}
                                    className="rounded-md bg-white/10 px-2 py-0.5 text-[12px] hover:bg-white/20">刷新状态</button>}>
                    <div className="grid gap-1.5 md:grid-cols-2">
                      {p.compliance.map((c) => (
                        <div key={c.id} className="flex items-center justify-between rounded-md bg-black/25 px-3 py-2">
                          <div className="min-w-0">
                            <div className="truncate">{c.name}</div>
                            {c.penalty && <div className="truncate text-[11px] text-white/40">违规：{c.penalty}</div>}
                          </div>
                          <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${COMPLIANCE_STYLE[c.status] || "bg-white/10"}`}>
                            {COMPLIANCE_LABEL[c.status] || c.status}
                          </span>
                        </div>
                      ))}
                    </div>
                  </Section>
                )}

                {/* 指标 */}
                {p.metrics.length > 0 && (
                  <Section title={`行业指标（${p.metrics.filter((m) => m.wired).length}/${p.metrics.length} 已接线）`}>
                    <div className="flex flex-wrap gap-1.5">
                      {p.metrics.map((m) => (
                        <span key={m.key}
                              className={`rounded-md px-2 py-1 text-[12px] ${m.wired ? "bg-emerald-500/15 text-emerald-200" : "bg-white/8 text-white/50"}`}>
                          {m.name}{m.wired ? "" : " · 待接线"}
                        </span>
                      ))}
                    </div>
                    <div className="mt-1.5 text-[11px] text-white/40">
                      接线方式：装连接器拉数后，在指标中心给每个指标补 spec（实体/聚合/字段）。
                    </div>
                  </Section>
                )}

                {/* 连接器蓝图 */}
                {p.integration_targets.length > 0 && (
                  <Section title="连接器蓝图（数据在哪）">
                    <div className="grid gap-1.5 md:grid-cols-2">
                      {p.integration_targets.map((t, i) => (
                        <div key={i} className="rounded-md bg-black/25 px-3 py-2">
                          <div className="font-medium">{t.system}
                            <span className="ml-2 text-[11px] text-white/40">
                              {t.auth_type === "oauth" ? "OAuth 授权" : t.auth_type === "mcp" ? "MCP 直连" : t.auth_type === "signing" ? "签名 API" : "API Key"}
                            </span>
                          </div>
                          {t.note && <div className="text-[11px] text-white/40">{t.note}</div>}
                        </div>
                      ))}
                    </div>
                    <div className="mt-1.5 text-[11px] text-white/40">
                      去「基座市场」装对应连接器包，装完在「连接器」页完成授权与首次同步。
                    </div>
                  </Section>
                )}

                <div className="mt-3 text-[12px] text-white/40">
                  预配员工 {p.agents_count} 人（在「Agent 团队」页管理；行业包员工不绑模型，首次运行再选）·
                  行业知识已进知识库 · 扩展字段已生效
                </div>
              </div>
            )}
          </div>
        );
      })}

      {!cid && <div />}
      {cid && !installed.length && (
        <div className="mb-4 rounded-lg border border-white/10 bg-white/5 p-4 text-white/50">
          还没有安装行业包。下面是内置目录，安装后这里会展示它注入的一切。
        </div>
      )}

      {cid && available.length > 0 && (
        <div>
          <div className="mb-2 font-medium">可安装的行业包</div>
          <div className="grid gap-2 md:grid-cols-2">
            {available.map((a) => (
              <div key={a.id} className="rounded-lg border border-white/10 bg-white/5 p-3">
                <div className="font-medium">{a.name}
                  <span className="ml-2 text-white/40">v{a.version}</span>
                </div>
                <div className="mt-0.5 text-[12px] text-white/50">{a.description}</div>
                <button
                  onClick={() => { location.href = "/market"; }}
                  className="mt-2 rounded-md bg-white/15 px-3 py-1 hover:bg-white/25">
                  去安装
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Section({ title, extra, children }: {
  title: string; extra?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="mb-3">
      <div className="mb-1.5 flex items-center justify-between">
        <div className="font-medium text-white/80">{title}</div>
        {extra}
      </div>
      {children}
    </div>
  );
}
