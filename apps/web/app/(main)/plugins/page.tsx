"use client";

// 插件库：把「本机操作能力」做成可安装的插件，并统一管理三档审批。
//
// 设计取舍：
// - 商业版默认不预装任何插件；agent-desktop 属于免费能力，只能从官方 npm 包安装。
// - 一键安装只发固定包名，页面不提供任意命令输入，避免把安装入口变成远程执行。
// - 三档审批（默认 / 自动 / 完全控制）与硬拦截来自后端策略，页面只做展示与切换；
//   完全控制必须限时，倒计时结束后由后端自动降回默认审批。
// - 操作日志复用 /computer-agent/logs，与设备操作台看到的是同一份审计记录。

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";

type Permission = { name: string; required: boolean; detail: string };

type Installation = {
  id: string;
  plugin_id: string;
  version: string | null;
  status: string;
  source: string;
  install_path: string | null;
  executable: string | null;
  platform: string | null;
  enabled: boolean;
  last_health_ok: boolean | null;
  last_health_at: string | null;
  last_health_error: string | null;
  config: unknown;
  created_at: string | null;
};

type CatalogItem = {
  id: string;
  title: string;
  vendor: string;
  version: string;
  size: string;
  license: string;
  category: string;
  price: string;
  platforms: string;
  tagline: string;
  description: string;
  capabilities: string[];
  permissions: Permission[];
  installable: boolean;
  installed: boolean;
  installation: Installation | null;
};

type Catalog = {
  plugins: CatalogItem[];
  installed_count: number;
  installable_count: number;
};

type PolicyMode = "default" | "auto" | "full";

type Policy = {
  id: string | null;
  mode: PolicyMode;
  auto_dirs: string[];
  auto_commands: string[];
  auto_apps: string[];
  full_control_until: string | null;
  notify_on_finish: boolean;
  updated_by: string | null;
  configured: boolean;
};

type HardBlock = { kind: string; label: string; examples: string[] };

type LogRow = {
  id: string;
  agent_id: string | null;
  action: string;
  allowed: boolean;
  deny_reason?: string | null;
  mock: boolean;
  result: unknown;
  created_at: string;
};

type ActionResp = {
  ok: boolean;
  message: string;
  installation?: Installation | null;
  detected?: Record<string, unknown> | null;
};

type Decision = {
  decision: string;
  mode: string;
  reason: string;
  matched: string | null;
};

type Tab = "library" | "installed" | "policy" | "audit";

const TABS: { key: Tab; label: string }[] = [
  { key: "library", label: "插件库" },
  { key: "installed", label: "已安装" },
  { key: "policy", label: "电脑权限" },
  { key: "audit", label: "操作日志" },
];

const MODE_META: Record<PolicyMode, {
  label: string;
  tag: string;
  desc: string;
  tone: string;
}> = {
  default: {
    label: "默认审批",
    tag: "推荐",
    desc: "只读操作直接执行；写文件、执行命令、启动应用逐次弹出审批。最稳妥，适合刚启用电脑操作的团队。",
    tone: "#818cf8",
  },
  auto: {
    label: "自动审批",
    tag: "",
    desc: "白名单目录、命令与应用自动放行；白名单之外仍然逐次审批。适合已经跑顺的固定流程。",
    tone: "#34d399",
  },
  full: {
    label: "完全控制",
    tag: "高风险",
    desc: "不逐次审批，全盘读写与执行。仍保留灾难性操作、凭据读取、终端宿主键入的硬拦截，并按会话限时。",
    tone: "#f87171",
  },
};

const TRIAL_ACTIONS = [
  { value: "accessibility_snapshot", label: "读界面结构", params: '{"app": "Finder", "window": 1}' },
  { value: "find_element", label: "找元素", params: '{"app": "Safari", "query": "搜索"}' },
  { value: "click_element", label: "点击元素", params: '{"ref": "Finder|1|3.1"}' },
  { value: "set_value", label: "填入文本", params: '{"ref": "Safari|1|3.1", "value": "hello"}' },
  { value: "run_shell", label: "执行命令", params: '{"command": "git status"}' },
  { value: "run_write", label: "写文件", params: '{"path": "~/Documents/report.md"}' },
  { value: "run_app", label: "启动应用", params: '{"app": "Calculator"}' },
  { value: "type_text", label: "键入文本", params: '{"text": "hello", "app": "Notes"}' },
  { value: "screenshot", label: "截屏", params: "{}" },
] as const;

function splitList(text: string): string[] {
  return text
    .split(/[\n,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseUtc(iso: string): number {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`).getTime();
}

function formatRemaining(until: string | null, now: number): string | null {
  if (!until) return null;
  const ms = parseUtc(until) - now;
  if (!Number.isFinite(ms)) return null;
  if (ms <= 0) return "已到期";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = `${m}`.padStart(2, "0");
  const ss = `${s}`.padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function shortTime(iso: string): string {
  return iso.slice(5, 16).replace("T", " ");
}

function PluginBadge({ item }: { item: CatalogItem }) {
  if (item.installed) {
    return <span className="text-[10px] px-2 py-0.5 rounded-full"
      style={{ background: "rgba(16,185,129,0.14)", color: "#6ee7b7", border: "1px solid rgba(16,185,129,0.3)" }}>已安装</span>;
  }
  if (!item.installable) {
    return <span className="text-[10px] px-2 py-0.5 rounded-full"
      style={{ background: "rgba(148,163,184,0.12)", color: "var(--muted)", border: "1px solid var(--border)" }}>即将上线</span>;
  }
  return <span className="text-[10px] px-2 py-0.5 rounded-full"
    style={{ background: "rgba(99,102,241,0.14)", color: "#c7d2fe", border: "1px solid rgba(99,102,241,0.3)" }}>免费</span>;
}

export default function PluginsPage() {
  const cid = useActiveCompanyId();
  const [tab, setTab] = useState<Tab>("library");
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("全部");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const catalogRes = useResource<Catalog>(
    cid ? `plugins:catalog:${cid}` : null,
    () => api.get<Catalog>(`/plugins/catalog?company_id=${cid}`),
  );
  const policyRes = useResource<Policy>(
    cid ? `plugins:policy:${cid}` : null,
    () => api.get<Policy>(`/computer-agent/policy?company_id=${cid}`),
  );
  const hardRes = useResource<HardBlock[]>(
    cid ? `plugins:hard:${cid}` : null,
    () => api.get<HardBlock[]>(`/computer-agent/hard-blocks?company_id=${cid}`),
  );
  const logsRes = useResource<LogRow[]>(
    cid ? `plugins:logs:${cid}` : null,
    () => api.get<LogRow[]>(`/computer-agent/logs?company_id=${cid}&limit=50`),
  );

  const catalog = catalogRes.data;
  const policy = policyRes.data;
  const hardBlocks = hardRes.data ?? [];
  const logs = logsRes.data ?? [];

  useEffect(() => setMounted(true), []);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // 白名单草稿：策略从后端回来时同步一次，之后由用户编辑。
  const [dirsText, setDirsText] = useState("");
  const [commandsText, setCommandsText] = useState("");
  const [appsText, setAppsText] = useState("");
  const policyStamp = policy
    ? `${policy.mode}|${policy.updated_by ?? ""}|${policy.auto_dirs.join(";")}|${policy.auto_commands.join(";")}|${policy.auto_apps.join(";")}`
    : "";
  useEffect(() => {
    if (!policy) return;
    setDirsText(policy.auto_dirs.join("\n"));
    setCommandsText(policy.auto_commands.join("\n"));
    setAppsText(policy.auto_apps.join("\n"));
    // policyStamp 已经把需要同步的字段拼成字符串，避免数组引用变化导致反复重置
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policyStamp]);

  const [fullMinutes, setFullMinutes] = useState(60);
  const [trialAction, setTrialAction] = useState<string>("run_shell");
  const [trialParams, setTrialParams] = useState<string>('{"command": "git status"}');
  const [trialResult, setTrialResult] = useState<Decision | null>(null);
  const [logFilter, setLogFilter] = useState<"all" | "ok" | "blocked">("all");

  const plugins = useMemo(() => catalog?.plugins ?? [], [catalog]);
  const categories = useMemo(
    () => ["全部", ...Array.from(new Set(plugins.map((p) => p.category)))],
    [plugins],
  );
  const visible = useMemo(() => {
    const key = q.trim().toLowerCase();
    return plugins.filter((p) => {
      const inCat = category === "全部" || p.category === category;
      const hit = !key || `${p.title} ${p.vendor} ${p.tagline} ${p.category} ${p.capabilities.join(" ")}`
        .toLowerCase().includes(key);
      return inCat && hit;
    });
  }, [plugins, q, category]);

  const installed = plugins.filter((p) => p.installed);
  const remaining = formatRemaining(policy?.full_control_until ?? null, now);
  const fullActive = !!remaining && remaining !== "已到期";

  const refreshAll = () => {
    catalogRes.refresh();
    policyRes.refresh();
    logsRes.refresh();
  };

  function ok(text: string) {
    setNotice({ kind: "ok", text });
  }
  function fail(e: unknown, fallback: string) {
    setNotice({ kind: "err", text: describeError(e, fallback) });
  }

  async function runPluginAction(
    pluginId: string,
    action: "install" | "detect" | "health",
  ) {
    if (!cid || busy) return;
    setBusy(`${pluginId}:${action}`);
    setNotice(null);
    const verb = action === "install" ? "安装" : action === "detect" ? "检测" : "健康检查";
    try {
      const r = await api.post<ActionResp>(
        `/plugins/${pluginId}/${action}?company_id=${cid}`);
      ok(`${verb}完成：${r.message}`);
      refreshAll();
      if (action === "install") setTab("installed");
    } catch (e) {
      fail(e, `${verb}失败`);
    } finally {
      setBusy(null);
    }
  }

  async function togglePlugin(item: CatalogItem, enabled: boolean) {
    if (!cid) return;
    setBusy(`${item.id}:toggle`);
    try {
      const qs = enabled ? "true" : "false";
      const r = await api.patch<ActionResp>(
        `/plugins/${item.id}?enabled=${qs}&company_id=${cid}`);
      ok(r.message);
      refreshAll();
    } catch (e) {
      fail(e, enabled ? "启用失败" : "禁用失败");
    } finally {
      setBusy(null);
    }
  }

  async function unregister(item: CatalogItem) {
    if (!cid) return;
    const managed = item.installation?.source === "managed";
    const okGo = await confirmAction({
      title: managed ? `卸载 ${item.title}？` : `撤销 ${item.title} 的登记？`,
      description: managed
        ? "将从本机卸载 npm 全局包，并删除登记。员工随后会失去这项能力。"
        : "只删除登记，不会改动你手动安装的程序。",
      bullets: managed
        ? ["执行 npm uninstall -g", "删除插件登记与最近健康状态", "审计日志不受影响"]
        : ["删除插件登记与最近健康状态", "已手动安装的可执行文件保持不变"],
      confirmText: managed ? "卸载" : "撤销登记",
      danger: true,
    });
    if (!okGo) return;
    setBusy(`${item.id}:remove`);
    try {
      const removePackage = managed ? "true" : "false";
      const r = await api.del<ActionResp>(
        `/plugins/${item.id}?company_id=${cid}&remove_package=${removePackage}`);
      ok(r.message);
      setDetailId(null);
      refreshAll();
    } catch (e) {
      fail(e, "撤销失败");
    } finally {
      setBusy(null);
    }
  }

  async function switchMode(mode: PolicyMode) {
    if (!cid || busy) return;
    if (mode === "full") {
      if (fullActive) return;
      const confirmed = await confirmAction({
        title: "开启完全控制？",
        description: `接下来 ${fullMinutes} 分钟内，数字员工不再逐次征求你的同意，可直接读写全盘文件与执行命令。`,
        bullets: [
          "到期后自动降回「默认审批」，不会静默续期",
          "凭据目录、灾难性命令、向终端宿主键入仍然硬拦截",
          `本次会话时长：${fullMinutes} 分钟`,
        ],
        confirmText: "开启完全控制",
        danger: true,
      });
      if (!confirmed) return;
      setBusy("policy:full");
      try {
        await api.post<Policy>(
          `/computer-agent/full-control?company_id=${cid}&minutes=${fullMinutes}`);
        ok(`已开启完全控制，${fullMinutes} 分钟后自动降级`);
        policyRes.refresh();
      } catch (e) {
        fail(e, "开启失败");
      } finally {
        setBusy(null);
      }
      return;
    }
    setBusy(`policy:${mode}`);
    try {
      await api.put<Policy>(`/computer-agent/policy?company_id=${cid}`, { mode });
      ok(`已切换到「${MODE_META[mode].label}」`);
      policyRes.refresh();
    } catch (e) {
      fail(e, "切换失败");
    } finally {
      setBusy(null);
    }
  }

  async function exitFullControl() {
    if (!cid) return;
    setBusy("policy:exit");
    try {
      await api.post<Policy>(`/computer-agent/exit-full-control?company_id=${cid}`);
      ok("已退出完全控制，回到默认审批");
      policyRes.refresh();
    } catch (e) {
      fail(e, "退出失败");
    } finally {
      setBusy(null);
    }
  }

  async function saveWhitelist() {
    if (!cid) return;
    setBusy("policy:whitelist");
    try {
      await api.put<Policy>(`/computer-agent/policy?company_id=${cid}`, {
        auto_dirs: splitList(dirsText),
        auto_commands: splitList(commandsText),
        auto_apps: splitList(appsText),
      });
      ok("白名单已保存");
      policyRes.refresh();
    } catch (e) {
      fail(e, "保存失败");
    } finally {
      setBusy(null);
    }
  }

  async function saveNotify(on: boolean) {
    if (!cid) return;
    setBusy("policy:notify");
    try {
      await api.put<Policy>(`/computer-agent/policy?company_id=${cid}`, {
        notify_on_finish: on,
      });
      policyRes.refresh();
    } catch (e) {
      fail(e, "保存失败");
    } finally {
      setBusy(null);
    }
  }

  async function runTrial() {
    if (!cid) return;
    let params: unknown;
    try {
      params = JSON.parse(trialParams || "{}");
    } catch {
      setNotice({ kind: "err", text: "参数不是合法 JSON" });
      return;
    }
    setBusy("policy:trial");
    setTrialResult(null);
    try {
      setTrialResult(await api.post<Decision>(
        `/computer-agent/evaluate?company_id=${cid}`,
        { action_type: trialAction, params },
      ));
    } catch (e) {
      fail(e, "试算失败");
    } finally {
      setBusy(null);
    }
  }

  function pickTrial(value: string) {
    setTrialAction(value);
    const preset = TRIAL_ACTIONS.find((a) => a.value === value);
    if (preset) setTrialParams(preset.params);
    setTrialResult(null);
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      ok("已复制到剪贴板");
    } catch {
      setNotice({ kind: "err", text: "复制失败，请手动选中复制" });
    }
  }

  const detail = detailId ? plugins.find((p) => p.id === detailId) ?? null : null;

  const filteredLogs = logs.filter((l) =>
    logFilter === "all" ? true : logFilter === "ok" ? l.allowed : !l.allowed);
  const blockedCount = logs.filter((l) => !l.allowed).length;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">⊞ 插件库</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              让数字员工真正上机 · 本机操作 · 三档审批 · 全程审计
            </p>
          </div>
          <div className="flex items-center gap-2">
            {policy && (
              <span className="text-[11px] px-2.5 py-1 rounded-full"
                style={{
                  color: MODE_META[policy.mode].tone,
                  border: `1px solid ${MODE_META[policy.mode].tone}44`,
                  background: `${MODE_META[policy.mode].tone}14`,
                }}>
                当前：{fullActive ? `完全控制 · 剩余 ${remaining}` : MODE_META[policy.mode].label}
              </span>
            )}
            <button className="btn-ghost !py-1.5 !px-3 text-xs"
              onClick={() => setTab("policy")}>电脑权限</button>
            <button className="btn-ghost !py-1.5 !px-3 text-xs"
              onClick={() => setTab("audit")}>操作日志</button>
          </div>
        </div>
      </div>

      {/* Tab 栏 */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-3.5 py-1.5 rounded-full text-xs transition-all border ${
              tab === t.key ? "bg-indigo-600 text-white border-indigo-600" : ""}`}
            style={tab === t.key ? {} : { borderColor: "rgba(99,102,241,0.25)", color: "var(--muted)" }}>
            {t.label}
            {t.key === "installed" && installed.length > 0 ? ` ${installed.length}` : ""}
            {t.key === "audit" && logs.length > 0 ? ` ${logs.length}` : ""}
          </button>
        ))}
      </div>

      {notice && (
        <div className="mb-4 rounded-xl px-4 py-2.5 text-xs"
          style={{
            background: notice.kind === "ok" ? "rgba(16,185,129,0.10)" : "rgba(239,68,68,0.10)",
            border: `1px solid ${notice.kind === "ok" ? "rgba(16,185,129,0.3)" : "rgba(239,68,68,0.3)"}`,
            color: notice.kind === "ok" ? "#6ee7b7" : "#fca5a5",
          }}>
          {notice.text}
        </div>
      )}

      {!cid && (
        <div className="card card-static text-center py-10">
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            还没有选择公司。请先在工作台或顶栏选择公司。
          </p>
        </div>
      )}

      {cid && tab === "library" && (
        <>
          <div className="rounded-xl px-4 py-3 mb-4 text-xs leading-relaxed"
            style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.25)", color: "#fcd34d" }}>
            <b>商业版默认不预装插件。</b>「电脑操作」属于免费能力，按需从插件库安装；
            一键安装只从官方 npm 包获取，不接受任意包名与命令。
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            {[
              { k: "已安装插件", v: catalog?.installed_count ?? 0, tip: "个" },
              { k: "可安装插件", v: catalog?.installable_count ?? 0, tip: "个" },
              { k: "电脑动作", v: logs.length, tip: "次" },
              { k: "安全拦截", v: blockedCount, tip: "次" },
            ].map((s) => (
              <div key={s.k} className="card card-static !p-3.5">
                <p className="text-[10px]" style={{ color: "var(--muted)" }}>{s.k}</p>
                <p className="text-xl font-semibold mt-1 tabular-nums">
                  {s.v}<span className="text-[11px] font-normal ml-1" style={{ color: "var(--muted)" }}>{s.tip}</span>
                </p>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="搜索插件、能力或厂商"
              className="input !py-2 text-xs max-w-xs" />
            <div className="flex gap-1.5 flex-wrap">
              {categories.map((c) => (
                <button key={c} onClick={() => setCategory(c)}
                  className={`px-2.5 py-1 rounded-full text-[11px] border transition-colors ${
                    category === c ? "bg-white/10" : ""}`}
                  style={category === c
                    ? { borderColor: "rgba(99,102,241,0.5)", color: "#c7d2fe" }
                    : { borderColor: "var(--border)", color: "var(--muted)" }}>
                  {c}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {visible.map((p) => {
              const installing = busy === `${p.id}:install`;
              return (
                <div key={p.id} className="card p-4 space-y-3 min-w-0">
                  <div className="flex items-start gap-3">
                    <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                      style={{ background: "rgba(99,102,241,0.14)", color: "#c7d2fe" }}>⊞</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="font-semibold text-sm leading-tight">{p.title}</p>
                        <PluginBadge item={p} />
                      </div>
                      <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                        {p.vendor} · v{p.version}
                      </p>
                    </div>
                  </div>
                  <p className="text-xs leading-relaxed" style={{ color: "var(--foreground)" }}>
                    {p.tagline}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {p.capabilities.slice(0, 3).map((c) => (
                      <span key={c} className="text-[10px] px-2 py-0.5 rounded-full"
                        style={{ background: "var(--surface-2)", color: "var(--muted)" }}>{c}</span>
                    ))}
                    {p.capabilities.length > 3 && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full"
                        style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                        +{p.capabilities.length - 3}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center justify-between gap-2 pt-1">
                    <span className="text-[10px] truncate" style={{ color: "var(--muted)" }}>
                      {p.size} · {p.license}
                    </span>
                    <div className="flex items-center gap-2 shrink-0">
                      <button className="btn-ghost !py-1 !px-2.5 text-[11px]"
                        onClick={() => setDetailId(p.id)}>
                        {p.installed ? "管理" : "了解"}
                      </button>
                      {p.installable && !p.installed && (
                        <button className="btn-primary !py-1 !px-3 text-[11px] disabled:opacity-40"
                          disabled={!!busy}
                          onClick={() => runPluginAction(p.id, "install")}>
                          {installing ? "安装中…" : "安装"}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
            {!visible.length && (
              <div className="card card-static col-span-full text-center py-8">
                <p className="text-sm" style={{ color: "var(--muted)" }}>没有匹配的插件</p>
              </div>
            )}
          </div>
        </>
      )}

      {cid && tab === "installed" && (
        <div className="card card-static overflow-hidden">
          <div className="px-4 py-3 flex items-center justify-between gap-3"
            style={{ borderBottom: "1px solid var(--border)" }}>
            <div>
              <p className="font-semibold text-sm">本机插件</p>
              <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                安装与检测发生在运行后端的这台机器上；禁用后员工会立即失去对应能力。
              </p>
            </div>
            <button className="btn-ghost !py-1.5 !px-3 text-xs" onClick={() => setTab("library")}>
              去插件库
            </button>
          </div>
          {installed.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ color: "var(--muted)" }}>
                    <th className="text-left px-4 py-2 font-medium">插件</th>
                    <th className="text-left px-4 py-2 font-medium">版本</th>
                    <th className="text-left px-4 py-2 font-medium">状态</th>
                    <th className="text-left px-4 py-2 font-medium">最近体检</th>
                    <th className="text-right px-4 py-2 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {installed.map((p) => {
                    const inst = p.installation;
                    const enabled = inst?.enabled ?? true;
                    return (
                      <tr key={p.id} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="px-4 py-3">
                          <p className="font-medium">{p.title}</p>
                          <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                            {inst?.executable || inst?.install_path || p.vendor}
                          </p>
                        </td>
                        <td className="px-4 py-3 tabular-nums">{inst?.version || p.version}</td>
                        <td className="px-4 py-3">
                          <span style={{ color: enabled ? "#6ee7b7" : "var(--muted)" }}>
                            {enabled ? "已启用" : "已禁用"}
                          </span>
                          <span className="ml-1 text-[10px]" style={{ color: "var(--muted)" }}>
                            · {inst?.source === "managed" ? "托管安装" : "命令行登记"}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          {inst?.last_health_at ? (
                            <span style={{ color: inst.last_health_ok ? "#6ee7b7" : "#fca5a5" }}>
                              {inst.last_health_ok ? "正常" : "异常"} · {shortTime(inst.last_health_at)}
                            </span>
                          ) : (
                            <span style={{ color: "var(--muted)" }}>未检查</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end gap-2 flex-wrap">
                            <button className="btn-ghost !py-1 !px-2 text-[11px]"
                              disabled={!!busy}
                              onClick={() => runPluginAction(p.id, "health")}>
                              {busy === `${p.id}:health` ? "检查中…" : "体检"}
                            </button>
                            <button className="btn-ghost !py-1 !px-2 text-[11px]"
                              disabled={!!busy}
                              onClick={() => togglePlugin(p, !enabled)}>
                              {enabled ? "禁用" : "启用"}
                            </button>
                            <button className="btn-ghost !py-1 !px-2 text-[11px]"
                              style={{ color: "#fca5a5" }}
                              onClick={() => setDetailId(p.id)}>
                              详情
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-10">
              <p className="text-sm mb-2" style={{ color: "var(--muted)" }}>还没有安装插件</p>
              <button className="btn-primary !py-1.5 !px-3 text-xs" onClick={() => setTab("library")}>
                去插件库挑选
              </button>
            </div>
          )}
          {installed.some((p) => p.installation?.last_health_error) && (
            <div className="px-4 py-2.5 text-[11px]"
              style={{ borderTop: "1px solid var(--border)", color: "#fca5a5" }}>
              {installed
                .filter((p) => p.installation?.last_health_error)
                .map((p) => `${p.title}: ${p.installation?.last_health_error}`)
                .join(" · ")}
            </div>
          )}
        </div>
      )}

      {cid && tab === "policy" && (
        <div className="space-y-5">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {(Object.keys(MODE_META) as PolicyMode[]).map((m) => {
              const meta = MODE_META[m];
              const active = policy?.mode === m && (m !== "full" || fullActive);
              return (
                <button key={m} onClick={() => switchMode(m)}
                  disabled={!!busy || (m === "full" && fullActive)}
                  className="card card-static !p-4 text-left transition-all disabled:opacity-60"
                  style={active ? { borderColor: `${meta.tone}88`, background: `${meta.tone}0f` } : {}}>
                  <div className="flex items-center gap-2">
                    <span className="w-4 h-4 rounded-full flex items-center justify-center text-[10px]"
                      style={{
                        border: `1px solid ${active ? meta.tone : "var(--border)"}`,
                        background: active ? meta.tone : "transparent",
                        color: "#0b1020",
                      }}>
                      {active ? "✓" : ""}
                    </span>
                    <span className="font-semibold text-sm">{meta.label}</span>
                    {meta.tag && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full"
                        style={{ background: `${meta.tone}1f`, color: meta.tone }}>{meta.tag}</span>
                    )}
                  </div>
                  <p className="text-[11px] leading-relaxed mt-2" style={{ color: "var(--muted)" }}>
                    {meta.desc}
                  </p>
                </button>
              );
            })}
          </div>

          {fullActive && (
            <div className="rounded-xl px-4 py-3 flex items-center gap-3 flex-wrap"
              style={{ background: "rgba(239,68,68,0.10)", border: "1px solid rgba(239,68,68,0.35)" }}>
              <span className="text-xs" style={{ color: "#fca5a5" }}>
                完全控制进行中 · 剩余 <b className="tabular-nums">{remaining}</b>，到期自动回到默认审批
              </span>
              <button className="btn-primary !py-1 !px-3 text-[11px] ml-auto"
                disabled={busy === "policy:exit"}
                onClick={exitFullControl}>
                {busy === "policy:exit" ? "退出中…" : "立即退出"}
              </button>
            </div>
          )}

          {policy && !policy.configured && (
            <div className="rounded-xl px-4 py-3 text-xs"
              style={{ background: "rgba(148,163,184,0.08)", border: "1px solid var(--border)", color: "var(--muted)" }}>
              当前公司还没有保存过电脑权限策略，执行路径会沿用旧审批规则；
              任选一个模式或保存白名单后即生效。
            </div>
          )}

          {!fullActive && policy?.mode === "full" && (
            <div className="rounded-xl px-4 py-3 text-xs"
              style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.25)", color: "#fcd34d" }}>
              上一次完全控制会话已到期，当前实际按「默认审批」执行。需要再次开启请重新确认。
            </div>
          )}

          <div className="card card-static">
            <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">
              <div>
                <h2 className="font-semibold text-sm">自动审批白名单</h2>
                <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                  仅在「自动审批」模式生效；含 ; &amp; | ` $ &lt; &gt; 等元字符的命令不会自动放行。
                </p>
              </div>
              <button className="btn-primary !py-1.5 !px-3 text-xs"
                disabled={!!busy}
                onClick={saveWhitelist}>
                {busy === "policy:whitelist" ? "保存中…" : "保存白名单"}
              </button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="text-[11px] block mb-1" style={{ color: "var(--muted)" }}>
                  目录（每行一个，如 ~/Documents/work）
                </label>
                <textarea rows={5} value={dirsText}
                  onChange={(e) => setDirsText(e.target.value)}
                  placeholder="~/Documents/work"
                  className="input font-mono text-[11px]" />
              </div>
              <div>
                <label className="text-[11px] block mb-1" style={{ color: "var(--muted)" }}>
                  命令前缀（每行一个，如 git status）
                </label>
                <textarea rows={5} value={commandsText}
                  onChange={(e) => setCommandsText(e.target.value)}
                  placeholder="git status"
                  className="input font-mono text-[11px]" />
              </div>
              <div>
                <label className="text-[11px] block mb-1" style={{ color: "var(--muted)" }}>
                  应用（每行一个，如 Calculator）
                </label>
                <textarea rows={5} value={appsText}
                  onChange={(e) => setAppsText(e.target.value)}
                  placeholder="Calculator"
                  className="input font-mono text-[11px]" />
              </div>
            </div>
            <label className="inline-flex items-center gap-2 text-xs mt-3 cursor-pointer select-none"
              style={{ color: "var(--muted)" }}>
              <input type="checkbox"
                checked={policy?.notify_on_finish ?? true}
                disabled={busy === "policy:notify"}
                onChange={(e) => saveNotify(e.target.checked)}
                className="accent-indigo-500 w-3.5 h-3.5" />
              长任务完成后通知我
            </label>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <div className="card card-static">
              <h2 className="font-semibold text-sm mb-1">策略试算</h2>
              <p className="text-[11px] mb-3" style={{ color: "var(--muted)" }}>
                不执行任何动作，只预演一次操作会被放行、审批还是硬拦截。
              </p>
              <div className="flex gap-2 mb-2 flex-wrap">
                {TRIAL_ACTIONS.map((a) => (
                  <button key={a.value} onClick={() => pickTrial(a.value)}
                    className={`px-2.5 py-1 rounded-full text-[11px] border transition-colors ${
                      trialAction === a.value ? "bg-white/10" : ""}`}
                    style={trialAction === a.value
                      ? { borderColor: "rgba(99,102,241,0.5)", color: "#c7d2fe" }
                      : { borderColor: "var(--border)", color: "var(--muted)" }}>
                    {a.label}
                  </button>
                ))}
              </div>
              <textarea rows={3} value={trialParams}
                onChange={(e) => setTrialParams(e.target.value)}
                className="input font-mono text-[11px]" />
              <div className="flex items-center gap-3 mt-2">
                <button className="btn-ghost !py-1.5 !px-3 text-xs"
                  disabled={!!busy} onClick={runTrial}>
                  {busy === "policy:trial" ? "试算中…" : "试算"}
                </button>
                {trialResult && (
                  <span className="text-[11px]"
                    style={{
                      color: trialResult.decision === "blocked" ? "#fca5a5"
                        : trialResult.decision === "allow" ? "#6ee7b7" : "#fcd34d",
                    }}>
                    {trialResult.decision === "blocked" ? "硬拦截"
                      : trialResult.decision === "allow" ? "直接放行"
                      : trialResult.decision === "approval" ? "需要审批" : "沿用旧规则"}
                    {" · "}{trialResult.reason}
                  </span>
                )}
              </div>
            </div>

            <div className="card card-static">
              <h2 className="font-semibold text-sm mb-1">所有模式一律硬拦截</h2>
              <p className="text-[11px] mb-3" style={{ color: "var(--muted)" }}>
                由后端代码写死，任何审批模式都无法关闭，前端只能查看。
              </p>
              <div className="space-y-3">
                {hardBlocks.map((g) => (
                  <div key={g.kind}>
                    <p className="text-[11px] font-medium mb-1">{g.label}</p>
                    <div className="flex flex-wrap gap-1">
                      {g.examples.map((x) => (
                        <span key={x} className="text-[10px] px-1.5 py-0.5 rounded font-mono"
                          style={{ background: "rgba(239,68,68,0.08)", color: "#fca5a5", border: "1px solid rgba(239,68,68,0.18)" }}>
                          {x}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
                {!hardBlocks.length && (
                  <p className="text-[11px]" style={{ color: "var(--muted)" }}>加载中…</p>
                )}
              </div>
            </div>
          </div>

          {policy?.mode === "full" && fullActive && (
            <div className="card card-static">
              <label className="text-[11px] block mb-1" style={{ color: "var(--muted)" }}>
                下一次开启完全控制的时长
              </label>
              <select className="input !py-1.5 text-xs max-w-xs"
                value={fullMinutes}
                onChange={(e) => setFullMinutes(Number(e.target.value))}>
                <option value={30}>30 分钟</option>
                <option value={60}>1 小时</option>
                <option value={120}>2 小时</option>
                <option value={240}>4 小时</option>
              </select>
            </div>
          )}
        </div>
      )}

      {cid && tab === "audit" && (
        <div className="card card-static">
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <h2 className="font-semibold text-sm">操作日志</h2>
            <div className="flex gap-1.5">
              {([["all", "全部"], ["ok", "成功"], ["blocked", "被拦截"]] as const).map(([k, label]) => (
                <button key={k} onClick={() => setLogFilter(k)}
                  className={`px-2.5 py-1 rounded-full text-[11px] border transition-colors ${
                    logFilter === k ? "bg-white/10" : ""}`}
                  style={logFilter === k
                    ? { borderColor: "rgba(99,102,241,0.5)", color: "#c7d2fe" }
                    : { borderColor: "var(--border)", color: "var(--muted)" }}>
                  {label}
                </button>
              ))}
            </div>
            <span className="text-[11px] ml-auto" style={{ color: "var(--muted)" }}>
              最近 50 条 · 只追加，不可删除
            </span>
          </div>
          <div className="space-y-1 max-h-[560px] overflow-y-auto">
            {filteredLogs.map((l) => (
              <details key={l.id} className="rounded-xl px-3 py-2"
                style={{ background: "var(--surface-2)" }}>
                <summary className="text-xs cursor-pointer flex items-center gap-2 list-none">
                  <span style={{ color: l.allowed ? "#6ee7b7" : "#fca5a5" }}>
                    {l.allowed ? "✓" : "✕"}
                  </span>
                  <span className="font-medium">{l.action}</span>
                  <span className="text-[10px] truncate" style={{ color: "var(--muted)" }}>
                    {l.deny_reason || ""}
                  </span>
                  <span className="ml-auto text-[10px] tabular-nums shrink-0"
                    style={{ color: "var(--muted)" }}>
                    {shortTime(l.created_at)}
                  </span>
                </summary>
                <pre className="text-[10px] mt-1 whitespace-pre-wrap overflow-auto max-h-32"
                  style={{ color: "var(--muted)" }}>
                  {l.deny_reason || JSON.stringify(l.result, null, 2)}
                </pre>
              </details>
            ))}
            {!filteredLogs.length && (
              <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>
                暂无记录
              </p>
            )}
          </div>
        </div>
      )}

      {mounted && detail && createPortal(
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.72)", backdropFilter: "blur(6px)" }}
          onMouseDown={(e) => { if (e.target === e.currentTarget) setDetailId(null); }}>
          <div role="dialog" aria-modal="true" aria-label={detail.title}
            className="w-full max-w-2xl max-h-[86vh] rounded-2xl flex flex-col overflow-hidden animate-fade-up"
            style={{ background: "var(--surface)", border: "1px solid var(--border)", boxShadow: "0 30px 80px -20px rgba(0,0,0,0.8)" }}>
            <div className="px-5 py-4 flex items-start gap-3 shrink-0"
              style={{ borderBottom: "1px solid var(--border)", background: "linear-gradient(120deg, rgba(99,102,241,0.10), transparent 70%)" }}>
              <span className="w-11 h-11 rounded-xl flex items-center justify-center text-xl shrink-0"
                style={{ background: "rgba(99,102,241,0.14)", color: "#c7d2fe" }}>⊞</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <h2 className="font-semibold text-base">{detail.title}</h2>
                  <PluginBadge item={detail} />
                </div>
                <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                  {detail.vendor} · v{detail.version} · {detail.size} · {detail.license}
                </p>
              </div>
              <button className="btn-ghost !py-1 !px-2 text-xs" onClick={() => setDetailId(null)}>
                关闭
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
              <p className="text-xs leading-relaxed">{detail.description}</p>

              <div>
                <p className="text-[11px] font-medium mb-1.5">能力</p>
                <div className="flex flex-wrap gap-1.5">
                  {detail.capabilities.map((c) => (
                    <span key={c} className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: "var(--surface-2)", color: "var(--muted)" }}>{c}</span>
                  ))}
                </div>
              </div>

              <div>
                <p className="text-[11px] font-medium mb-1.5">需要的系统权限</p>
                <div className="space-y-1.5">
                  {detail.permissions.map((perm) => (
                    <div key={perm.name} className="rounded-xl px-3 py-2"
                      style={{ background: "var(--surface-2)" }}>
                      <p className="text-[11px]">
                        {perm.name}
                        <span className="ml-1 text-[10px]"
                          style={{ color: perm.required ? "#fcd34d" : "var(--muted)" }}>
                          {perm.required ? "必需" : "可选"}
                        </span>
                      </p>
                      <p className="text-[10px] mt-0.5 leading-relaxed" style={{ color: "var(--muted)" }}>
                        {perm.detail}
                      </p>
                    </div>
                  ))}
                </div>
              </div>

              {detail.installable && (
                <div>
                  <p className="text-[11px] font-medium mb-1.5">安装方式</p>
                  <div className="rounded-xl p-3 space-y-2"
                    style={{ background: "var(--surface-2)" }}>
                    <p className="text-[10px] leading-relaxed" style={{ color: "var(--muted)" }}>
                      一键安装会在运行后端的机器上执行固定的 npm 全局安装命令；
                      也可以自己安装后回到这里点「检测已安装」完成登记。
                    </p>
                    <div className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 font-mono text-[11px]"
                      style={{ background: "rgba(0,0,0,0.25)", border: "1px solid var(--border)" }}>
                      <code className="truncate">npm i -g agent-desktop</code>
                      <button className="btn-ghost !py-0.5 !px-2 text-[10px] ml-auto shrink-0"
                        onClick={() => copy("npm i -g agent-desktop")}>
                        复制
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {detail.installation && (
                <div>
                  <p className="text-[11px] font-medium mb-1.5">安装状态</p>
                  <div className="rounded-xl px-3 py-2 text-[11px] space-y-1"
                    style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                    <p>来源：{detail.installation.source === "managed" ? "托管安装" : "命令行登记"}</p>
                    {detail.installation.executable && (
                      <p className="font-mono break-all">可执行文件：{detail.installation.executable}</p>
                    )}
                    {detail.installation.platform && (
                      <p>平台：{detail.installation.platform}</p>
                    )}
                    <p>
                      健康：{detail.installation.last_health_ok === null
                        ? "未检查"
                        : detail.installation.last_health_ok ? "正常" : "异常"}
                      {detail.installation.last_health_error
                        ? ` · ${detail.installation.last_health_error}` : ""}
                    </p>
                  </div>
                </div>
              )}
            </div>

            <div className="px-5 py-3 flex items-center gap-2 flex-wrap shrink-0"
              style={{ borderTop: "1px solid var(--border)" }}>
              {detail.installable && !detail.installed && (
                <>
                  <button className="btn-primary !py-1.5 !px-3 text-xs disabled:opacity-40"
                    disabled={!!busy}
                    onClick={() => runPluginAction(detail.id, "install")}>
                    {busy === `${detail.id}:install` ? "安装中…" : "一键安装"}
                  </button>
                  <button className="btn-ghost !py-1.5 !px-3 text-xs"
                    disabled={!!busy}
                    onClick={() => runPluginAction(detail.id, "detect")}>
                    {busy === `${detail.id}:detect` ? "检测中…" : "检测已安装"}
                  </button>
                </>
              )}
              {detail.installed && (
                <>
                  <button className="btn-ghost !py-1.5 !px-3 text-xs"
                    disabled={!!busy}
                    onClick={() => runPluginAction(detail.id, "health")}>
                    {busy === `${detail.id}:health` ? "检查中…" : "健康检查"}
                  </button>
                  <button className="btn-ghost !py-1.5 !px-3 text-xs"
                    disabled={!!busy}
                    onClick={() => togglePlugin(detail, !(detail.installation?.enabled ?? true))}>
                    {detail.installation?.enabled === false ? "启用" : "禁用"}
                  </button>
                  <button className="btn-ghost !py-1.5 !px-3 text-xs ml-auto"
                    style={{ color: "#fca5a5" }}
                    disabled={!!busy}
                    onClick={() => unregister(detail)}>
                    {detail.installation?.source === "managed" ? "卸载" : "撤销登记"}
                  </button>
                </>
              )}
              {!detail.installable && (
                <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                  该插件尚未上线，上线后会在插件库中出现安装入口。
                </p>
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
