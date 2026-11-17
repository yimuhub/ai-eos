"use client";
import { useEffect, useMemo, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type Package = {
  id: string; type: string; key: string; name: string; version: string;
  permissions_required?: string[]; dependencies?: string[];
  description?: string | null;
};
type PackInfo = {
  id: string; name: string; version: string; description: string;
  custom_fields: number; workflow_templates: number; metric_defs: number;
  agent_roster: number; published: boolean;
};
type InstallResp = {
  installed: boolean; installation_id?: string; package?: string;
  version?: string; artifacts?: { kind: string; name?: string }[];
  notes?: string[];
  requires_confirmation?: { permissions_required?: string[] };
};
type InstallRow = { package_id: string; package_key?: string;
  version?: string; installed_at?: string };

const CATEGORY_LABELS: Record<string, string> = {
  tool: "工具", connector: "连接器", workflow_template: "工作流模板",
  model_provider: "模型供应商", skill_pack: "技能包", industry_pack: "行业包",
  AGENT: "旧·Agent", SKILL: "旧·技能", WORKFLOW: "旧·工作流",
  DEPARTMENT_TEMPLATE: "旧·组织模板",
};

export default function MarketPage() {
  const cid = useActiveCompanyId();
  const [tab, setTab] = useState<"packages" | "packs">("packages");
  const [category, setCategory] = useState<string>("all");
  const [confirming, setConfirming] = useState<Package | null>(null);
  const [confirmPerms, setConfirmPerms] = useState(false);
  const [result, setResult] = useState<InstallResp | null>(null);
  const [busy, setBusy] = useState(false);

  const { data, error, refresh } = useResource(
    cid ? `market:${cid}` : null,
    async () => {
      const pkgs = await api.get<Package[]>("/governance/marketplace/packages");
      const packs = await api.get<{ packs: PackInfo[] }>("/industry-packs");
      let installs: InstallRow[] = [];
      try {
        installs = await api.get<InstallRow[]>(
          `/governance/installations?company_id=${cid}`);
      } catch { /* 治理接口对非 MANAGE 角色可能 403——安装状态仅作展示 */ }
      return { pkgs: Array.isArray(pkgs) ? pkgs : [],
               packs: packs?.packs ?? [],
               installs: Array.isArray(installs) ? installs : [] };
    },
  );
  const pkgs = useMemo(
    () => (data && "pkgs" in data ? data.pkgs : []), [data]);
  const packs = useMemo(
    () => (data && "packs" in data ? data.packs : []), [data]);
  const installCount = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of (data && "installs" in data ? data.installs : [])) {
      m[r.package_id] = (m[r.package_id] || 0) + 1;
    }
    return m;
  }, [data]);

  useEffect(() => {
    if (error) notify("error", error || "加载市场失败");
  }, [error]);

  const filtered = useMemo(() => {
    if (!pkgs) return [];
    return category === "all" ? pkgs : pkgs.filter((p) => p.type === category);
  }, [pkgs, category]);

  const install = async (pkg: Package) => {
    if (!cid) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await api.post<InstallResp>(
        `/governance/marketplace/packages/${pkg.id}/install`
        + `?company_id=${cid}&confirm_permissions=${confirmPerms}`, {});
      setResult(r);
      if (r.installed) {
        setResult(r);  // 保留弹窗展示产物清单，用户自己关闭
        setConfirmPerms(false);
        notify("success", `「${pkg.name}」已安装`
          + (r.artifacts?.length ? `（${r.artifacts.length} 项产物）` : ""));
        await refresh();
      }
    } catch (e) {
      notify("error", describeError(e, "安装失败"));
    } finally {
      setBusy(false);
    }
  };

  const publishPack = async (p: PackInfo) => {
    try {
      const r = await api.post<{ updated: boolean }>(
        "/industry-packs/publish", { pack_id: p.id });
      notify("success", r?.updated
        ? `「${p.name}」已更新到 v${p.version}` : `「${p.name}」已发布进市场`);
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "发布失败（需要平台超管）"));
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">基座市场</div>
      <p className="mb-4 text-white/50">
        给公司装「积木」：工具、连接器、工作流模板、行业包——安装即产生真实产物（员工/工作流/指标/工具），全部可回滚。
      </p>

      <div className="mb-4 flex gap-2">
        <button
          onClick={() => setTab("packages")}
          className={`rounded-md px-3 py-1.5 ${tab === "packages" ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
          插件市场
        </button>
        <button
          onClick={() => setTab("packs")}
          className={`rounded-md px-3 py-1.5 ${tab === "packs" ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
          行业包
        </button>
      </div>

      {tab === "packages" && (
        <>
          <div className="mb-3 flex flex-wrap gap-2">
            {["all", "tool", "connector", "workflow_template",
              "model_provider", "industry_pack"].map((c) => (
              <button key={c}
                onClick={() => setCategory(c)}
                className={`rounded-full px-3 py-1 text-white/80 ${category === c ? "bg-white/20" : "bg-white/5 hover:bg-white/10"}`}>
                {c === "all" ? "全部" : CATEGORY_LABELS[c] || c}
              </button>
            ))}
          </div>
          {!cid && <div className="text-white/40">先选择一家公司再安装。</div>}
          <div className="grid gap-3 md:grid-cols-2">
            {filtered.map((p) => (
              <div key={p.id}
                   className="rounded-lg border border-white/10 bg-white/5 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-medium">
                      {p.name}
                      <span className="ml-2 text-white/40">v{p.version}</span>
                      {(installCount[p.id] || 0) > 0 && (
                        <span className="ml-2 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[11px] text-emerald-300">
                          已安装 ×{installCount[p.id]}
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-white/40">
                      {CATEGORY_LABELS[p.type] || p.type} · {p.key}
                    </div>
                  </div>
                  <button
                    disabled={!cid || busy}
                    onClick={() => { setConfirming(p); setConfirmPerms(false); setResult(null); }}
                    className="shrink-0 rounded-md bg-white/15 px-3 py-1.5 hover:bg-white/25 disabled:opacity-40">
                    安装
                  </button>
                </div>
                {p.description && (
                  <div className="mt-2 text-white/60">{p.description}</div>
                )}
                {!!p.permissions_required?.length && (
                  <div className="mt-2 text-[12px] text-amber-300/80">
                    权限：{p.permissions_required.join("、")}
                  </div>
                )}
              </div>
            ))}
            {!filtered.length && (
              <div className="text-white/40">这个分类还没有包。发布：POST /governance/marketplace/packages</div>
            )}
          </div>
        </>
      )}

      {tab === "packs" && (
        <div className="grid gap-3 md:grid-cols-2">
          {packs.map((p) => (
            <div key={p.id}
                 className="rounded-lg border border-white/10 bg-white/5 p-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-medium">
                    {p.name}
                    <span className="ml-2 text-white/40">v{p.version}</span>
                    {p.published && (
                      <span className="ml-2 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[11px] text-emerald-300">已进市场</span>
                    )}
                  </div>
                  <div className="mt-0.5 text-white/60">{p.description}</div>
                </div>
                <div className="flex shrink-0 flex-col gap-1.5">
                  <button
                    onClick={() => publishPack(p)}
                    className="rounded-md bg-white/10 px-3 py-1 hover:bg-white/20">
                    发布
                  </button>
                  {p.published && (
                    <button
                      onClick={async () => {
                        try {
                          const rows = await api.get<Package[]>(
                            "/governance/marketplace/packages");
                          const pkg = (rows || []).find(
                            (x) => x.type === "industry_pack" && x.key === p.id);
                          if (pkg) { setConfirming(pkg); setConfirmPerms(false); setResult(null); setTab("packages"); }
                        } catch (e) {
                          notify("error", describeError(e));
                        }
                      }}
                      className="rounded-md bg-white/15 px-3 py-1 hover:bg-white/25">
                      安装到公司
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 text-[12px] text-white/50">
                {p.custom_fields} 扩展字段 · {p.agent_roster} 预配员工 ·{" "}
                {p.workflow_templates} 工作流 · {p.metric_defs} 指标
              </div>
            </div>
          ))}
          {!packs.length && <div className="text-white/40">加载中…</div>}
        </div>
      )}

      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
             onClick={() => setConfirming(null)}>
          <div className="w-full max-w-lg rounded-xl border border-white/15 bg-[#1a1d24] p-5"
               onClick={(e) => e.stopPropagation()}>
            <div className="mb-1 text-[14px] font-semibold">
              安装「{confirming.name}」
            </div>
            <p className="mb-3 text-white/50">
              该包将以本公司身份运行，请确认权限清单。
            </p>
            <div className="mb-3 rounded-lg bg-white/5 p-3 text-[12px]">
              <div className="text-amber-300/90 mb-1">权限清单</div>
              <div className="text-white/70">
                {confirming.permissions_required?.length
                  ? confirming.permissions_required.join("、")
                  : "（未声明特殊权限）"}
              </div>
            </div>
            <label className="mb-3 flex items-center gap-2 text-white/70">
              <input type="checkbox" checked={confirmPerms}
                     onChange={(e) => setConfirmPerms(e.target.checked)} />
              我已确认上述权限
            </label>
            {result && (
              <div className="mb-3 rounded-lg bg-white/5 p-3 text-[12px]">
                {result.installed ? (
                  <>
                    <div className="text-emerald-300">
                      已安装 {result.artifacts?.length ?? 0} 项产物
                    </div>
                    <ul className="mt-1 list-disc pl-4 text-white/60">
                      {result.artifacts?.map((a, i) => (
                        <li key={i}>{a.kind}：{a.name || a.kind}</li>
                      ))}
                    </ul>
                    {result.notes?.map((n, i) => (
                      <div key={i} className="mt-1 text-white/50">{n}</div>
                    ))}
                  </>
                ) : (
                  <div className="text-white/60">需要先勾选权限确认。</div>
                )}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button onClick={() => setConfirming(null)}
                      className="rounded-md bg-white/10 px-3 py-1.5 hover:bg-white/20">
                关闭
              </button>
              <button disabled={!confirmPerms || busy}
                      onClick={() => install(confirming)}
                      className="rounded-md bg-emerald-600/80 px-3 py-1.5 hover:bg-emerald-600 disabled:opacity-40">
                确认安装
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
