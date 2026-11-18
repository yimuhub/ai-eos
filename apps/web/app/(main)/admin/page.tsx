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
type ExpertPackRow = { id: string; name: string; version: string;
  industry: string; description: string;
  experts: { id: string; name: string; category?: string }[];
  seeded_in_company?: number | null };

const KEY_STORE = "aieos_admin_token";  // unlock 后签发的管理会话令牌（12h）

type GateStage = "checking" | "totp" | "key" | "enroll" | "recovery";

export default function AdminPage() {
  // 门禁三因子 + 「验证器优先」登录（用户口径）：
  // * 已绑定 → 日常只输 6 位动态码进台（不用每次输超级密钥）；
  // * 验证器不可用 → 点「用超级密钥重新绑定」走根凭据通道；
  // * 首次 → 超级密钥 → 扫码绑定 → 恢复码。
  const [key, setKey] = useState("");
  const [unlocked, setUnlocked] = useState(false);
  const [stage, setStage] = useState<GateStage>("checking");
  const [qrSvg, setQrSvg] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [newIndustry, setNewIndustry] = useState("");
  const [expertPacks, setExpertPacks] = useState<ExpertPackRow[]>([]);
  const [installPacks, setInstallPacks] = useState<string[]>([]);

  const authHeaders = () => {
    const t = sessionStorage.getItem(KEY_STORE) || "";
    return { "X-Admin-Token": t };
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

  const submitKey = async () => {
    setBusy(true);
    try {
      // 根凭据通道：解除旧绑定（若有）→ 直接进入新绑定
      sessionStorage.setItem(KEY_STORE + "_root", key);
      const r = await api.post<{ qr_svg: string }>(
        "/ops/admin/2fa/enroll", { account: "deployer" },
        { headers: { "X-Super-Key": key } });
      setQrSvg(r.qr_svg);
      setStage("enroll");
      notify("success", "旧验证器已解除，请扫描新二维码绑定");
    } catch (e) {
      notify("error", describeError(e, "超级密钥校验失败"));
    } finally {
      setBusy(false);
    }
  };

  const unlockWithTotp = async () => {
    setBusy(true);
    try {
      const r = await api.post("/ops/admin/unlock", {},
        { headers: { "X-TOTP-Code": totpCode } });
      // 验证器优先进台后，超级密钥仍需存 session 供管理请求头使用；
      // 用户在 totp 屏点的「重新绑定」路径会在 submitKey 里覆盖它
      sessionStorage.setItem(KEY_STORE, key || sessionStorage.getItem(KEY_STORE) || "");
      setUnlocked(true);
      notify("success", r.message || "超级管理后台已解锁");
    } catch (e) {
      notify("error", describeError(e, "动态码校验失败"));
    } finally {
      setBusy(false);
    }
  };

  const startEnroll = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ otpauth_uri: string; qr_svg: string }>(
        "/ops/admin/2fa/enroll", { account: "deployer" },
        { headers: { "X-Super-Key": key } });
      setQrSvg(r.qr_svg);
      setStage("enroll");
    } catch (e) {
      notify("error", describeError(e, "生成绑定二维码失败"));
    } finally {
      setBusy(false);
    }
  };

  const confirmEnroll = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ recovery_codes: string[] }>(
        "/ops/admin/2fa/confirm", { code: totpCode },
        { headers: { "X-Super-Key": key } });
      setRecoveryCodes(r.recovery_codes);
      setStage("recovery");
      // 顺手用同一动态码换管理会话令牌（进台免二次输入）
      try {
        const u = await api.post<{ admin_token: string }>(
          "/ops/admin/unlock", {},
          { headers: { "X-TOTP-Code": totpCode } });
        sessionStorage.setItem(KEY_STORE, u.admin_token);
      } catch { /* 用户点「进入后台」时若过期会重新提示 */ }
    } catch (e) {
      notify("error", describeError(e, "绑定失败"));
    } finally {
      setBusy(false);
    }
  };

  // 挂载分流：只需超管登录态（不带超级密钥/令牌）查绑定状态——
  // 已绑定 → totp 优先屏；未绑定 → 超级密钥首绑屏。
  // 若 sessionStorage 已有未过期会话令牌 → 直接进台（过期由首个请求 403 暴露）。
  useEffect(() => {
    if (sessionStorage.getItem(KEY_STORE)) { setUnlocked(true); return; }
    setBusy(true);
    api.get<{ enrolled: boolean }>("/ops/admin/2fa/status")
      .then((st) => setStage(st.enrolled ? "totp" : "key"))
      .catch(() => setStage("key"))
      .finally(() => setBusy(false));
  }, []);

  const openCompany = async (cid: string) => {
    setSelected(cid);
    try {
      const r = await api.get<CompanyDetail>(`/ops/admin/companies/${cid}`,
        { headers: authHeaders() });
      setDetail(r);
      setNewIndustry(r.company.industry || "");
      try {
        const ep = await api.get<{ packs: ExpertPackRow[] }>(
          `/expert-packs?company_id=${cid}`);
        setExpertPacks(ep?.packs ?? []);
      } catch { /* 目录读取失败不阻塞配置 */ }
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
          <div className="mb-1 text-[15px] font-semibold">🔐 超级管理后台</div>
          <p className="mb-4 text-white/50">
            仅限平台超管进入。门禁三因子：超管身份 + 超级密钥（部署方持有：数据库目录下
            super_key.key 文件，或环境变量 AIEOS_SUPER_KEY）+ Authenticator 动态码。
          </p>

          {stage === "key" && (
            <>
              <input type="password" value={key}
                     onChange={(e) => setKey(e.target.value)}
                     placeholder="输入超级密钥"
                     className="mb-3 w-full rounded-md bg-black/30 px-3 py-2" />
              <button onClick={submitKey} disabled={busy || !key}
                      className="w-full rounded-md bg-violet-600/80 px-3 py-2 hover:bg-violet-600 disabled:opacity-40">
                {busy ? "校验中…" : "下一步"}
              </button>
            </>
          )}

          {stage === "totp" && (
            <>
              <p className="mb-3 text-white/60">
                请打开 Authenticator，输入当前 6 位动态码（30 秒刷新）。
              </p>
              <input inputMode="numeric" value={totpCode} maxLength={6}
                     onChange={(e) => setTotpCode(e.target.value.replace(/[^0-9]/g, ""))}
                     placeholder="000000"
                     className="mb-3 w-full rounded-md bg-black/30 px-3 py-2 text-center text-[18px] tracking-[0.4em]" />
              <button onClick={unlockWithTotp} disabled={busy || totpCode.length !== 6}
                      className="w-full rounded-md bg-violet-600/80 px-3 py-2 hover:bg-violet-600 disabled:opacity-40">
                {busy ? "校验中…" : "验证并进入"}
              </button>
            </>
          )}

          {stage === "enroll" && (
            <>
              <p className="mb-3 text-white/60">
                未绑定验证器。用 Authenticator 扫描下方二维码，输入显示的 6 位动态码完成绑定。
              </p>
              {qrSvg ? (
                <div className="mb-3 flex justify-center rounded-lg bg-white p-3">
                  {/* qr_svg 是 data:image/svg+xml 的 data URI——必须走 img src，
                      不是 HTML 片段（用 innerHTML 会渲染成一坨编码文本） */}
                  <img src={qrSvg} alt="TOTP 绑定二维码" width={200} height={200} />
                </div>
              ) : (
                <button onClick={startEnroll} disabled={busy}
                        className="mb-3 w-full rounded-md bg-white/15 px-3 py-2 hover:bg-white/25">
                  生成绑定二维码
                </button>
              )}
              <input inputMode="numeric" value={totpCode} maxLength={6}
                     onChange={(e) => setTotpCode(e.target.value.replace(/[^0-9]/g, ""))}
                     placeholder="输入动态码确认绑定"
                     className="mb-3 w-full rounded-md bg-black/30 px-3 py-2 text-center tracking-[0.4em]" />
              <div className="mb-3 flex gap-2">
                <button onClick={confirmEnroll} disabled={busy || totpCode.length !== 6}
                        className="flex-1 rounded-md bg-emerald-600/80 px-3 py-2 hover:bg-emerald-600 disabled:opacity-40">
                  确认绑定
                </button>
                <button onClick={() => {
                  notify("error", "未绑定验证器无法进入后台：请完成绑定，或用超级密钥重新绑定后再试");
                }}
                        className="rounded-md bg-white/10 px-3 py-2 hover:bg-white/20">
                  稍后再绑
                </button>
              </div>
            </>
          )}

          {stage === "recovery" && (
            <>
              <p className="mb-2 font-medium text-emerald-300">绑定成功</p>
              <p className="mb-2 text-white/60">
                请把以下恢复码抄写保存——手机丢失时用它们进入（每个只能用一次）：
              </p>
              <div className="mb-3 grid grid-cols-2 gap-1.5 rounded-lg bg-black/30 p-3 font-mono text-white/80">
                {recoveryCodes.map((c, i) => <div key={i}>{c}</div>)}
              </div>
              <button onClick={() => {
                if (!sessionStorage.getItem(KEY_STORE)) {
                  notify("error", "会话令牌缺失，请重新验证");
                  return;
                }
                setUnlocked(true);
                notify("success", "超级管理后台已解锁");
              }}
                      className="w-full rounded-md bg-violet-600/80 px-3 py-2 hover:bg-violet-600">
                我已保存，进入后台
              </button>
            </>
          )}
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
            <div className="mb-1.5 text-white/70">解锁行业专家包（写入该公司专家库，可编辑专属化）</div>
            <div className="flex flex-wrap gap-2">
              {expertPacks.map((p) => (
                <button key={p.id} disabled={busy}
                        onClick={async () => {
                          try {
                            const r = await api.post<{ created: number }>(
                              "/expert-packs/admin/unlock",
                              { pack_id: p.id, company_id: detail.company.id },
                              { headers: authHeaders() });
                            notify("success",
                              `「${p.name}」已解锁（新增 ${r.created} 位专家）`);
                          } catch (e) {
                            notify("error", describeError(e, "解锁失败"));
                          }
                        }}
                        className={`rounded-md px-2.5 py-1 text-[12px] ${(p.seeded_in_company || 0) > 0 ? "bg-emerald-500/20 text-emerald-300" : "bg-white/10 hover:bg-white/20"}`}>
                      {p.name} · {p.experts.length} 专家{(p.seeded_in_company || 0) > 0 ? " ✓" : ""}
                </button>
              ))}
              {!expertPacks.length && <div className="text-white/40">暂无内置专家包。</div>}
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
