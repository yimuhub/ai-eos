"use client";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

/** 超级管理后台（V6.1.1）：门禁 = 平台超管身份 + 超级密钥（X-Super-Key）。
 *  密钥存 sessionStorage——关浏览器即失效，不落 localStorage。 */

type UserRow = { id: string; email: string; username: string;
  is_superadmin: boolean; created_at?: string };
type CompanyRow = { id: string; name: string; industry?: string | null;
  owner?: { id: string; email: string; username: string };
  industry_packs: { key: string; name: string; version?: string }[];
  flags: string[] };
type Overview = { users: UserRow[]; companies: CompanyRow[] };
type CompanyDetail = { company: { id: string; name: string; industry?: string | null };
  industry_packs: { key: string; name: string; version?: string }[];
  tools: { id: string; name: string; source: string; enabled: boolean }[];
  compliance: { key: string; name: string; status: string }[];
  metrics: { key: string; name: string; wired: boolean }[] };

const KEY_STORE = "aieos_admin_key";

export default function AdminPage() {
  // 会话内恢复：sessionStorage 有密钥即直接进台——服务端对每个管理请求
  // 仍真校验 X-Super-Key（错密钥会在第一个请求就被 403），前端只是少打一遍。
  const [key, setKey] = useState("");
  const [unlocked, setUnlocked] = useState(
    typeof sessionStorage !== "undefined" && !!sessionStorage.getItem(KEY_STORE));
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [newIndustry, setNewIndustry] = useState("");
  const [installPacks, setInstallPacks] = useState<string[]>([]);

  const authHeaders = () => {
    const k = sessionStorage.getItem(KEY_STORE) || "";
    return { "X-Super-Key": k };
  };

  const { data, error, refresh } = useResource(
    unlocked ? "admin-overview" : null,
    async () => {
      const r = await api.get<Overview>("/ops/admin/overview",
        { headers: authHeaders() });
      return r;
    },
  );

  useEffect(() => {
    if (error) notify("error", error);
  }, [error]);

  const unlock = async () => {
    setBusy(true);
    try {
      await api.post("/ops/admin/unlock", {},
        { headers: { "X-Super-Key": key } });
      sessionStorage.setItem(KEY_STORE, key);
      setUnlocked(true);
      notify("success", "超级管理后台已解锁");
    } catch (e) {
      notify("error", describeError(e, "密钥校验失败"));
    } finally {
      setBusy(false);
    }
  };

  const openCompany = async (cid: string) => {
    setSelected(cid);
    try {
      const r = await api.get<CompanyDetail>(`/ops/admin/companies/${cid}`,
        { headers: authHeaders() });
      setDetail(r);
      setNewIndustry(r.company.industry || "");
    } catch (e) {
      notify("error", describeError(e));
    }
  };

  const applyEntitlements = async (body: Record<string, unknown>) => {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await api.post<{ actions: string[] }>(
        `/ops/admin/companies/${selected}/entitlements`,
        body, { headers: authHeaders() });
      notify("success", "已生效：" + (r.actions?.join("；") || "无变更"));
      await openCompany(selected);
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "配置失败"));
    } finally {
      setBusy(false);
    }
  };

  if (!unlocked) {
    return (
      <div className="mx-auto max-w-md px-4 py-16 text-[13px]">
        <div className="rounded-xl border border-white/10 bg-white/5 p-6">
          <div className="mb-1 text-[15px] font-semibold">🔒 超级管理后台</div>
          <p className="mb-4 text-white/50">
            仅限平台超管凭「超级密钥」进入。密钥由部署方持有（storage/super_key.key 或 AIEOS_SUPER_KEY）。
          </p>
          <input type="password" value={key}
                 onChange={(e) => setKey(e.target.value)}
                 placeholder="输入超级密钥"
                 className="mb-3 w-full rounded-md bg-black/30 px-3 py-2" />
          <button onClick={unlock} disabled={busy || !key}
                  className="w-full rounded-md bg-violet-600/80 px-3 py-2 hover:bg-violet-600 disabled:opacity-40">
            {busy ? "校验中…" : "解锁"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">超级管理后台</div>
      <p className="mb-4 text-white/50">
        给用户/公司解锁行业与配置内容。所有操作落审计（admin.* 事件）。
      </p>

      <div className="mb-6 rounded-xl border border-white/10 bg-white/5">
        <div className="border-b border-white/10 px-4 py-2.5 font-medium">公司与授权</div>
        <div className="divide-y divide-white/5">
          {(data?.companies ?? []).map((c) => (
            <div key={c.id} className="flex items-center justify-between px-4 py-3">
              <div>
                <div className="font-medium">
                  {c.name}
                  {c.industry && (
                    <span className="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-[11px]">
                      {c.industry}
                    </span>
                  )}
                </div>
                <div className="text-[12px] text-white/40">
                  负责人：{c.owner?.username || "—"} · 已装行业包：
                  {c.industry_packs.length
                    ? c.industry_packs.map((p) => p.name).join("、") : "无"}
                </div>
              </div>
              <button onClick={() => openCompany(c.id)}
                      className="shrink-0 rounded-md bg-white/15 px-3 py-1 hover:bg-white/25">
                配置
              </button>
            </div>
          ))}
          {!(data?.companies ?? []).length && (
            <div className="px-4 py-3 text-white/40">还没有公司。</div>
          )}
        </div>
      </div>

      {selected && detail && (
        <div className="rounded-xl border border-violet-500/30 bg-violet-500/5 p-4">
          <div className="mb-3 font-medium">
            配置：{detail.company.name}
            <button onClick={() => { setSelected(null); setDetail(null); }}
                    className="float-right rounded-md bg-white/10 px-2 py-0.5 text-[12px] hover:bg-white/20">关闭</button>
          </div>

          <div className="mb-4">
            <div className="mb-1.5 text-white/70">行业类别</div>
            <div className="flex gap-2">
              <input value={newIndustry} onChange={(e) => setNewIndustry(e.target.value)}
                     placeholder="如 restaurant / manufacturing（空 = 未定向）"
                     className="flex-1 rounded-md bg-black/30 px-3 py-2" />
              <button disabled={busy}
                      onClick={() => applyEntitlements({ industry: newIndustry })}
                      className="rounded-md bg-white/15 px-3 hover:bg-white/25">保存</button>
            </div>
          </div>

          <div className="mb-4">
            <div className="mb-1.5 text-white/70">解锁行业包（自动发布并装进该公司）</div>
            <div className="flex flex-wrap gap-2">
              {["manufacturing", "wholesale_retail", "ecommerce", "restaurant",
                "construction", "professional_services", "clinic", "education",
                "logistics", "real_estate"].map((id) => {
                const has = detail.industry_packs.some((p) => p.key === id);
                return (
                  <button key={id} disabled={busy}
                          onClick={() => applyEntitlements({ packs_install: [id] })}
                          className={`rounded-md px-2.5 py-1 text-[12px] ${has ? "bg-emerald-500/20 text-emerald-300" : "bg-white/10 hover:bg-white/20"}`}>
                    {id}{has ? " ✓" : ""}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="mb-4">
            <div className="mb-1.5 text-white/70">收回（停用 flag；勾选=连产物一起卸载）</div>
            <div className="grid gap-1.5">
              {detail.industry_packs.map((p) => (
                <div key={p.key} className="flex items-center justify-between rounded-md bg-black/25 px-3 py-2">
                  <span>{p.name}（{p.key}）</span>
                  <button disabled={busy}
                          onClick={() => applyEntitlements({
                            packs_remove: [{ key: p.key, uninstall: true }] })}
                          className="rounded-md bg-red-500/30 px-2.5 py-1 text-red-200 hover:bg-red-500/40">
                    收回并卸载
                  </button>
                </div>
              ))}
              {!detail.industry_packs.length && <div className="text-white/40">无已装行业包。</div>}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-lg bg-black/25 p-3">
              <div className="mb-1 text-white/70">合规台账（{detail.compliance.length}）</div>
              {detail.compliance.map((c) => (
                <div key={c.key} className="flex justify-between text-[12px] text-white/50">
                  <span className="truncate">{c.name}</span>
                  <span>{c.status}</span>
                </div>
              ))}
              {!detail.compliance.length && <div className="text-white/40">无</div>}
            </div>
            <div className="rounded-lg bg-black/25 p-3">
              <div className="mb-1 text-white/70">指标（{detail.metrics.length}）</div>
              {detail.metrics.slice(0, 6).map((m) => (
                <div key={m.key} className="flex justify-between text-[12px] text-white/50">
                  <span className="truncate">{m.name}</span>
                  <span>{m.wired ? "已接线" : "待接线"}</span>
                </div>
              ))}
              {!detail.metrics.length && <div className="text-white/40">无</div>}
            </div>
            <div className="rounded-lg bg-black/25 p-3">
              <div className="mb-1 text-white/70">插件工具（{detail.tools.length}）</div>
              {detail.tools.slice(0, 6).map((t) => (
                <div key={t.id} className="flex justify-between text-[12px] text-white/50">
                  <span className="truncate">{t.name}</span>
                  <span>{t.enabled ? "启用" : "停用"}</span>
                </div>
              ))}
              {!detail.tools.length && <div className="text-white/40">无</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
