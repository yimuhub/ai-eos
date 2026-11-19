"use client";
import { useCallback, useEffect, useState } from "react";
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
type IndustryPackRow = { id: string; name: string; version: string;
  description: string; published?: boolean };
type ContentTab = "companies" | "content" | "logs";
type LogView = "events" | "chats" | "sensitive";
type EventRow = { id: string; created_at?: string; event_type: string;
  entity_type?: string; entity_id?: string; actor_type?: string;
  actor_id?: string; company_id?: string; payload?: Record<string, unknown> };
type ChatThread = { agent_id: string; thread_id?: string; messages: number;
  last_at?: string };
type ChatMsg = { role: string; content: string; created_at?: string };
type SensitiveRow = { id: string; admin_user_id: string; category: string;
  target_id: string; target_desc?: string; reason: string; auth: string;
  created_at?: string };

const KEY_STORE = "aieos_admin_token";  // unlock 后签发的管理会话令牌（12h）

// 日志子页签（模块级常量：避免在 tsx 内联数组上写泛型断言与 JSX 解析打架）
const LOG_TABS: Array<[LogView, string]> = [
  ["events", "系统事件"], ["chats", "聊天记录"], ["sensitive", "敏感访问台账"],
];

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
  const [contentTab, setContentTab] = useState<ContentTab>("companies");
  const [industryPackRows, setIndustryPackRows] = useState<IndustryPackRow[]>([]);
  const [expertPackRows, setExpertPackRows] = useState<ExpertPackRow[]>([]);
  const cid = useActiveCompanyId();
  const [events, setEvents] = useState<EventRow[]>([]);
  const [chatThreads, setChatThreads] = useState<ChatThread[]>([]);
  const [chatMsgs, setChatMsgs] = useState<ChatMsg[] | null>(null);
  const [sensitive, setSensitive] = useState<SensitiveRow[]>([]);
  const [logView, setLogView] = useState<LogView>("events");
  const [gateFor, setGateFor] = useState<{ agentId: string } | null>(null);
  const [gateReason, setGateReason] = useState("");
  const [gateTotp, setGateTotp] = useState("");

  const authHeaders = () => {
    const t = sessionStorage.getItem(KEY_STORE) || "";
    return { "X-Admin-Token": t };
  };

  // 技能库审核队列：auto_pass/review 待人工放行的条目
  const [libQueue, setLibQueue] = useState<{ id: string; name: string; kind: string;
    description: string; brand_refs: string[]; review_status: string }[]>([]);
  const refreshLibQueue = useCallback(async () => {
    if (!unlocked) return;
    try {
      const r = await api.get<typeof libQueue>(
        "/skill-library/admin/queue?status=review", { headers: authHeaders() });
      setLibQueue(Array.isArray(r) ? r : []);
    } catch { setLibQueue([]); }
  }, [unlocked]);
  useEffect(() => { void refreshLibQueue(); }, [refreshLibQueue]);

  const reviewLib = async (id: string, status: "published" | "rejected") => {
    try {
      await api.post(`/skill-library/admin/${id}/review`, { status },
        { headers: authHeaders() });
      notify("success", status === "published" ? "已发布，企业技能库可见" : "已拒绝");
      void refreshLibQueue();
    } catch (e) {
      notify("error", describeError(e, "审核失败"));
    }
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

  // 基座内容总览：内置行业包 + 专家包目录（登录态即可读）
  useEffect(() => {
    api.get<{ packs: IndustryPackRow[] }>("/industry-packs")
      .then((r) => setIndustryPackRows(r?.packs ?? []))
      .catch(() => setIndustryPackRows([]));
    api.get<{ packs: ExpertPackRow[] }>("/expert-packs")
      .then((r) => setExpertPackRows(r?.packs ?? []))
      .catch(() => setExpertPackRows([]));
  }, []);

  const loadLogs = async () => {
    setEvents([]); setChatThreads([]); setSensitive([]);
    try {
      const r = await api.get<{ events: EventRow[] }>(
        `/ops/admin/logs/events?limit=300`, { headers: authHeaders() });
      setEvents(r?.events ?? []);
    } catch (e) { notify("error", describeError(e)); }
    try {
      const r = await api.get<{ threads: ChatThread[] }>(
        `/ops/admin/logs/chats`
        + (cid ? `?company_id=${cid}` : ""), { headers: authHeaders() });
      setChatThreads(r?.threads ?? []);
    } catch { /* 失败静默（tab 上会有全局错误提示） */ }
    try {
      const r = await api.get<{ access: SensitiveRow[] }>(
        `/ops/admin/logs/sensitive-access?limit=200`,
        { headers: authHeaders() });
      setSensitive(r?.access ?? []);
    } catch { /* 静默 */ }
  };

  useEffect(() => {
    if (unlocked && contentTab === "logs") loadLogs();
  }, [unlocked, contentTab]);

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

  const openChatWithGate = async () => {
    if (!gateFor) return;
    setBusy(true);
    try {
      const r = await api.get<{ messages: ChatMsg[]; audited: boolean }>(
        `/ops/admin/logs/chats/${gateFor.agentId}/messages`
        + `?company_id=${cid}&reason=${encodeURIComponent(gateReason)}`,
        { headers: { ...authHeaders(), "X-TOTP-Code": gateTotp } });
      setChatMsgs(r?.messages ?? null);
      setGateFor(null);
      setGateReason(""); setGateTotp("");
      notify("success", "已解锁查看（本次访问已留痕）");
      loadLogs();
    } catch (e) {
      notify("error", describeError(e, "查看被拒绝"));
    } finally {
      setBusy(false);
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
      <p className="mb-3 text-white/50">
        统一运营入口：给用户/公司解锁行业与配置内容。所有操作落审计（admin.* 事件）。
      </p>

      <div className="mb-4 flex gap-2">
        <button onClick={() => setContentTab("companies")}
                className={`rounded-md px-3 py-1.5 ${contentTab === "companies" ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
          公司与授权
        </button>
        <button onClick={() => setContentTab("content")}
                className={`rounded-md px-3 py-1.5 ${contentTab === "content" ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
          基座内容
        </button>
        <button onClick={() => setContentTab("logs")}
                className={`rounded-md px-3 py-1.5 ${contentTab === "logs" ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
          系统日志
        </button>
      </div>

      {contentTab === "content" && (
        <div className="mb-6 grid gap-4">
          <div className="rounded-xl border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">行业包（{industryPackRows.length}）</div>
            <div className="grid gap-2 md:grid-cols-2">
              {industryPackRows.map((p) => {
                const unlocked = (data?.companies ?? [])
                  .filter((c) => c.industry_packs.some((ip) => ip.key === p.id))
                  .map((c) => c.name);
                return (
                  <div key={p.id} className="rounded-lg bg-black/25 p-3">
                    <div className="font-medium">{p.name}
                      <span className="ml-2 text-white/40">v{p.version}</span>
                    </div>
                    <div className="mt-0.5 text-[12px] text-white/50">{p.description}</div>
                    <div className="mt-1.5 text-[12px]">
                      {unlocked.length
                        ? <span className="text-emerald-300">已解锁：{unlocked.join("、")}</span>
                        : <span className="text-white/40">尚无公司解锁</span>}
                    </div>
                  </div>
                );
              })}
              {!industryPackRows.length && <div className="text-white/40">加载中…</div>}
            </div>
          </div>
          <div className="rounded-xl border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">行业专家包（{expertPackRows.length}）</div>
            <div className="grid gap-2 md:grid-cols-2">
              {expertPackRows.map((p) => (
                <div key={p.id} className="rounded-lg bg-black/25 p-3">
                  <div className="font-medium">{p.name}
                    <span className="ml-2 text-white/40">{p.industry} · v{p.version}</span>
                  </div>
                  <div className="mt-0.5 text-[12px] text-white/50">{p.description}</div>
                  <div className="mt-1.5 text-[12px] text-white/40">
                    {p.experts.map((e) => `${e.name}（${e.category}）`).join("、")}
                  </div>
                </div>
              ))}
              {!expertPackRows.length && <div className="text-white/40">加载中…</div>}
            </div>
          </div>
          <div className="rounded-xl border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">技能库审核队列（{libQueue.length}）</div>
            <p className="mb-2 text-[12px] text-white/50">
              外部技能生态导入的条目在这里放行；带品牌引用/疑似注入的不会自动过。
            </p>
            {!libQueue.length && <div className="text-white/40">队列已清空——没有待审条目。</div>}
            <div className="grid gap-2">
              {libQueue.map((x) => (
                <div key={x.id} className="flex items-center justify-between gap-2 rounded-lg bg-black/25 p-3">
                  <div className="min-w-0">
                    <div className="truncate">
                      {x.kind === "expert" ? "👥" : x.kind === "connector" ? "🔌" : "⚡"} {x.name}
                      {x.brand_refs.length > 0 && (
                        <span className="ml-2 rounded bg-red-500/20 px-1.5 py-0.5 text-[10px] text-red-300">
                          品牌引用：{x.brand_refs.slice(0, 3).join(",")}
                        </span>
                      )}
                    </div>
                    <div className="truncate text-[12px] text-white/50">{x.description}</div>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    <button onClick={() => reviewLib(x.id, "published")}
                            className="rounded-md bg-emerald-600/80 px-2.5 py-1 text-[12px] hover:bg-emerald-600">
                      放行
                    </button>
                    <button onClick={() => reviewLib(x.id, "rejected")}
                            className="rounded-md bg-white/10 px-2.5 py-1 text-[12px] hover:bg-white/20">
                      拒绝
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="rounded-lg border border-white/10 bg-white/5 p-3 text-[12px] text-white/50">
            插件与知识包：在「公司与授权」选择公司 → 配置，可逐公司启停；
            插件目录见「基座市场」。
          </div>
        </div>
      )}

      {contentTab === "logs" && (
        <div className="grid gap-4">
          <div className="flex gap-2">
            {LOG_TABS.map(([k, label]) => (
              <button key={k} onClick={() => setLogView(k)}
                      className={`rounded-md px-3 py-1.5 ${logView === k ? "bg-white/15" : "bg-white/5 hover:bg-white/10"}`}>
                {label}
              </button>
            ))}
          </div>

          {logView === "events" && (
            <div className="rounded-xl border border-white/10 bg-white/5 p-4">
              <div className="mb-2 text-[12px] text-white/50">
                event_log 全量系统事件（最近 300 条）：工具调用、工作流、解锁、授权变更……
              </div>
              <div className="max-h-[480px] overflow-y-auto font-mono text-[11px]">
                {events.map((e, i) => (
                  <div key={e.id || i} className="border-b border-white/5 py-1">
                    <span className="text-white/40">{e.created_at?.slice(0, 19).replace("T", " ")}</span>
                    <span className="ml-2 text-cyan-300">{e.event_type}</span>
                    <span className="ml-2 text-white/40">{e.entity_type}:{e.entity_id}</span>
                    <span className="ml-2 text-white/30">{e.actor_type}:{e.actor_id}</span>
                  </div>
                ))}
                {!events.length && <div className="text-white/40">没有匹配的事件。</div>}
              </div>
            </div>
          )}

          {logView === "chats" && (
            <div className="rounded-xl border border-white/10 bg-white/5 p-4">
              <div className="mb-2 text-[12px] text-amber-300/90">
                聊天内容属敏感数据：查看需填写原因 + 输入 Authenticator 当前动态码，每次查看自动留痕。
              </div>
              <div className="grid gap-1.5">
                {chatThreads.map((t, i) => (
                  <div key={i} className="flex items-center justify-between rounded-md bg-black/25 px-3 py-2">
                    <div className="min-w-0">
                      <div className="text-white/80">员工 {t.agent_id.slice(0, 8)}…{t.thread_id ? ` · 线程 ${t.thread_id.slice(0, 8)}…` : " · 单聊"}</div>
                      <div className="text-[11px] text-white/40">{t.messages} 条 · 最近 {t.last_at?.slice(0, 19).replace("T", " ")}</div>
                    </div>
                    <button onClick={() => { setGateFor({ agentId: t.agent_id }); setChatMsgs(null); }}
                            className="shrink-0 rounded-md bg-amber-500/25 px-2.5 py-1 text-[12px] text-amber-200 hover:bg-amber-500/35">
                      查看内容
                    </button>
                  </div>
                ))}
                {!chatThreads.length && <div className="text-white/40">该公司还没有聊天记录。</div>}
              </div>
              {chatMsgs && (
                <div className="mt-3 max-h-[360px] overflow-y-auto rounded-lg bg-black/30 p-3">
                  {chatMsgs.map((m, i) => (
                    <div key={i} className="border-b border-white/5 py-1.5">
                      <span className={`mr-2 font-medium ${m.role === "boss" ? "text-sky-300" : "text-violet-300"}`}>
                        {m.role === "boss" ? "老板" : "员工"}
                      </span>
                      <span className="text-white/70">{m.content}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {logView === "sensitive" && (
            <div className="rounded-xl border border-white/10 bg-white/5 p-4">
              <div className="mb-2 text-[12px] text-white/50">
                敏感访问台账：每一次查看敏感内容的留痕（谁/何时/理由/验证方式）。
              </div>
              <div className="max-h-[480px] overflow-y-auto">
                {sensitive.map((a) => (
                  <div key={a.id} className="border-b border-white/5 py-1.5">
                    <div className="flex items-center justify-between">
                      <span className="text-white/80">{a.category} · {a.target_id.slice(0, 12)}…</span>
                      <span className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] text-white/50">{a.auth}</span>
                    </div>
                    <div className="text-[11px] text-white/40">
                      {a.created_at?.slice(0, 19).replace("T", " ")} · 理由：{a.reason.slice(0, 80)}
                    </div>
                  </div>
                ))}
                {!sensitive.length && <div className="text-white/40">还没有敏感访问记录。</div>}
              </div>
            </div>
          )}
        </div>
      )}

      {gateFor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-amber-500/30 bg-[#1a1d24] p-5">
            <div className="mb-1 text-[14px] font-semibold text-amber-300">查看敏感内容</div>
            <p className="mb-3 text-[12px] text-white/50">
              Agent 聊天记录属敏感数据。请填写查看原因并输入 Authenticator 当前动态码——本次访问将自动留痕。
            </p>
            <textarea value={gateReason}
                      onChange={(e) => setGateReason(e.target.value)}
                      placeholder="查看原因（至少 5 个字，将永久留档）"
                      className="mb-2 h-16 w-full rounded-md bg-black/30 px-3 py-2" />
            <input inputMode="numeric" value={gateTotp} maxLength={6}
                   onChange={(e) => setGateTotp(e.target.value.replace(/[^0-9]/g, ""))}
                   placeholder="6 位动态码"
                   className="mb-3 w-full rounded-md bg-black/30 px-3 py-2 text-center tracking-[0.4em]" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setGateFor(null)}
                      className="rounded-md bg-white/10 px-3 py-1.5 hover:bg-white/20">取消</button>
              <button onClick={openChatWithGate} disabled={busy || gateReason.trim().length < 5 || gateTotp.length !== 6}
                      className="rounded-md bg-amber-600/80 px-3 py-1.5 hover:bg-amber-600 disabled:opacity-40">
                验证并查看
              </button>
            </div>
          </div>
        </div>
      )}

      {contentTab === "companies" && (
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
      )}

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
