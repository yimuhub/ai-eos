"use client";
import { useEffect, useState } from "react";
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
type CompanyExpert = { id: string; name: string; category?: string;
  pack_id: string; skills_count: number; interviews: number;
  skills: { name: string; description?: string; category?: string;
            level?: number }[] };

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
  const [experts, setExperts] = useState<CompanyExpert[]>([]);
  type LibItem = { id: string; kind: string; name: string; description: string;
    industry_tags: string[]; trigger_keywords: string[]; install_count: number };
  const [libSkills, setLibSkills] = useState<LibItem[]>([]);
  const [libFilter, setLibFilter] = useState<string>("");
  const [selExpert, setSelExpert] = useState<string>("");
  const [transcript, setTranscript] = useState("");
  const [digestResult, setDigestResult] = useState<string | null>(null);

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

  // 专家列表：进页面取一次；访谈提交后刷新
  useEffect(() => {
    if (!cid) return;
    api.get<{ experts: CompanyExpert[] }>(
      `/interviews/experts?company_id=${cid}`)
      .then((r) => setExperts(r?.experts ?? []))
      .catch(() => setExperts([]));
    // 技能库：已发布的平台条目（跨行业通用层 + 各行业层）
    api.get<LibItem[]>("/skill-library")
      .then((r) => setLibSkills(Array.isArray(r) ? r : []))
      .catch(() => setLibSkills([]));
  }, [cid]);

  const installLibSkill = async (item: LibItem) => {
    if (!cid) return;
    setBusy(`lib:${item.id}`);
    try {
      const r = await api.post<{ kind: string; granted_agents?: number;
        expert_template_id?: string; hint?: string }>(
        `/skill-library/${item.id}/install`, { company_id: cid });
      if (item.kind === "skill") {
        notify("success", `「${item.name}」已装给 ${r.granted_agents ?? 0} 名员工（方法论将进入其系统提示词）`);
      } else if (item.kind === "expert") {
        notify("success", "专家团队已进「专家库」，可在上面选人访谈");
      } else {
        notify("info", r.hint || "连接器蓝图已记录");
      }
      setLibSkills((prev) => prev.map((x) => x.id === item.id
        ? { ...x, install_count: x.install_count + 1 } : x));
    } catch (e) {
      notify("error", describeError(e, "安装失败"));
    } finally {
      setBusy(null);
    }
  };

  const runInterview = async () => {
    if (!cid || !selExpert || transcript.trim().length < 20) {
      notify("error", "先选专家并粘贴访谈全文（≥20 字）");
      return;
    }
    setBusy("interview");
    try {
      const r = await api.post<{ interview_id: string }>(
        `/interviews?company_id=${cid}`, {
          transcript, expert_template_id: selExpert,
          department: "访谈登记" });
      const d = await api.post<{ ok: boolean; status: string;
        digest?: { summary?: string; applied?: { skills_added: number;
        prompt_rules: number } }; error?: string }>(
        `/interviews/${r.interview_id}/digest?company_id=${cid}`, {});
      if (d.ok) {
        setDigestResult(
          `✅ ${d.digest?.summary || "提炼完成"}（新增技能 ${d.digest?.applied?.skills_added ?? 0} 条 / 人设要点 ${d.digest?.applied?.prompt_rules ?? 0} 条）`);
        notify("success", "总控已把访谈沉淀进专家");
      } else {
        setDigestResult(`⛔ 提炼失败：${d.error || "未知"}`);
        notify("error", "提炼失败，访谈已留档");
      }
      setTranscript("");
      const er = await api.get<{ experts: CompanyExpert[] }>(
        `/interviews/experts?company_id=${cid}`);
      setExperts(er?.experts ?? []);
    } catch (e) {
      notify("error", describeError(e, "访谈提交失败"));
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

      {cid && experts.length > 0 && (
        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 font-medium">专家与访谈</div>
          <p className="mb-3 text-[12px] text-white/50">
            把各部门的日常工作总结（文字或语音转写）交给总控，它会自动把做法沉淀进对应专家的技能与规则——专家越用越懂你的生意。
          </p>
          <div className="mb-2 flex flex-wrap gap-1.5">
            {experts.map((e) => (
              <button key={e.id}
                      onClick={() => setSelExpert(e.id)}
                      className={`rounded-md px-2.5 py-1 text-[12px] ${selExpert === e.id ? "bg-violet-500/30 text-violet-200" : "bg-white/8 text-white/60 hover:bg-white/15"}`}>
                {e.name}（{e.category || "通用"}）· {e.skills_count} 技能
                {e.interviews > 0 ? ` · 访谈×${e.interviews}` : ""}
              </button>
            ))}
          </div>
          {selExpert && (
            <>
              <textarea value={transcript}
                        onChange={(e) => setTranscript(e.target.value)}
                        placeholder="粘贴访谈全文：部门日常工作、流程、经验、常踩的坑……（语音请先转文字）"
                        className="mb-2 h-28 w-full rounded-md bg-black/30 px-3 py-2" />
              <button onClick={runInterview} disabled={busy === "interview"}
                      className="rounded-md bg-violet-600/80 px-3 py-1.5 hover:bg-violet-600 disabled:opacity-40">
                {busy === "interview" ? "总控提炼中…" : "提交给总控提炼"}
              </button>
              {digestResult && (
                <div className="mt-2 rounded-md bg-emerald-500/10 px-3 py-2 text-emerald-200">
                  {digestResult}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {cid && libSkills.length > 0 && (
        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 font-medium">技能库（平台精选 · 已审核发布）</div>
          <p className="mb-2.5 text-[12px] text-white/50">
            提示型技能装到员工身上后，其方法论会进入员工系统提示词，跟着行业包的岗位一起上岗。
          </p>
          <div className="mb-2 flex flex-wrap gap-1.5">
            <button onClick={() => setLibFilter("")}
                    className={`rounded-md px-2.5 py-1 text-[12px] ${libFilter === "" ? "bg-white/25 text-white" : "bg-white/8 text-white/60 hover:bg-white/15"}`}>
              全部（{libSkills.length}）
            </button>
            {Array.from(new Set(libSkills.flatMap((x) => x.industry_tags))).map((t) => (
              <button key={t} onClick={() => setLibFilter(t)}
                      className={`rounded-md px-2.5 py-1 text-[12px] ${libFilter === t ? "bg-sky-500/30 text-sky-200" : "bg-white/8 text-white/60 hover:bg-white/15"}`}>
                {t}
              </button>
            ))}
          </div>
          <div className="grid gap-1.5 md:grid-cols-2">
            {libSkills.filter((x) => !libFilter || x.industry_tags.includes(libFilter))
              .map((x) => (
              <div key={x.id} className="flex items-center justify-between gap-2 rounded-md bg-black/25 px-3 py-2">
                <div className="min-w-0">
                  <div className="truncate">
                    {x.kind === "expert" ? "👥 " : x.kind === "connector" ? "🔌 " : "⚡ "}
                    {x.name}
                    {x.install_count > 0 && <span className="ml-1.5 text-[10px] text-white/40">已装 ×{x.install_count}</span>}
                  </div>
                  <div className="truncate text-[11px] text-white/40">{x.description}</div>
                </div>
                <button disabled={busy === `lib:${x.id}`}
                        onClick={() => installLibSkill(x)}
                        className="shrink-0 rounded-md bg-emerald-600/80 px-2.5 py-1 text-[12px] hover:bg-emerald-600 disabled:opacity-40">
                  {busy === `lib:${x.id}` ? "…" : x.kind === "skill" ? "装给全员" : "安装"}
                </button>
              </div>
            ))}
          </div>
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
