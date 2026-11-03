"use client";
import { useState } from "react";
import Link from "next/link";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import AppAliasSection from "@/components/AppAliasSection";

/* ============================================================
   设备操作台：Agent 真实操作电脑（截屏/键入/点击/按键/启应用/Shell）
   + 浏览器抓取（静态 / JS 渲染）
   所有操作有授权检查与审计日志。
   ============================================================ */

type LogRow = {
  id: string; agent_id: string | null; action: string;
  allowed: boolean; deny_reason?: string | null;
  mock: boolean; result: unknown; created_at: string;
};

/** 单项能力是否已授权：{ screenshot: true, run_shell: false, ... } */
type Perms = Record<string, boolean>;

/** 电脑操作执行结果 */
type ExecResult = {
  /** 实际执行模式：real = 真的操作了本机；mock = 未授权下的模拟 */
  mode: string;
  result?: { ok?: boolean; [k: string]: unknown };
  [k: string]: unknown;
};

/** 浏览器抓取结果（render=js 时 rendered 为真） */
type BrowserFetchResult = {
  title?: string;
  text?: string;
  error?: string;
  rendered?: boolean;
};

type ActionDef = { type: string; label: string; icon: string };

/** 分组渲染：先「读界面」，再「按引用操作」，最后才是传统的截图/键鼠。
    顺序本身就是推荐用法——能读结构就别猜坐标。 */
const ACTION_GROUPS: { title: string; note: string; items: ActionDef[] }[] = [
  {
    title: "读界面结构",
    note: "不看像素：直接拿窗口里的元素清单（角色 / 文字 / 位置）",
    items: [
      { type: "list_windows", label: "列出窗口", icon: "🪟" },
      { type: "accessibility_snapshot", label: "界面快照", icon: "🧬" },
      { type: "get_window", label: "窗口概览", icon: "🔍" },
      { type: "find_element", label: "找元素", icon: "🎯" },
    ],
  },
  {
    title: "按引用操作",
    note: "用快照里的 ref 点按钮 / 填输入框，不移动光标、不抢焦点",
    items: [
      { type: "click_element", label: "点击元素", icon: "👆" },
      { type: "set_value", label: "填入文本", icon: "✍️" },
    ],
  },
  {
    title: "截图与键鼠",
    note: "兜底路径：没有无障碍树的界面才用坐标",
    items: [
      { type: "screenshot", label: "截屏", icon: "📸" },
      { type: "type_text", label: "键入文本", icon: "⌨️" },
      { type: "click", label: "点击坐标", icon: "🖱" },
      { type: "key", label: "按键", icon: "⏎" },
      { type: "run_app", label: "启动应用", icon: "🚀" },
      { type: "run_shell", label: "Shell(白名单)", icon: "💻" },
    ],
  },
];

/** 能力中文名：授权面板与审计日志都靠它，否则一堆英文键看不出开了什么。 */
const CAP_LABELS: Record<string, string> = {
  list_windows: "列出窗口",
  accessibility_snapshot: "读界面结构",
  get_window: "窗口概览",
  find_element: "找元素",
  click_element: "点击元素",
  set_value: "填入文本",
  screenshot: "截屏",
  type_text: "键入文本",
  click: "点击坐标",
  key: "按键",
  run_app: "启动应用",
  run_shell: "Shell 命令",
  run_write: "写文件",
};

/** 只读能力：默认审批模式下也不会弹审批单，面板上标出来省得误以为「开了还不执行」。 */
const READ_ONLY_CAPS = new Set([
  "list_windows", "accessibility_snapshot", "get_window", "find_element", "screenshot",
]);

const PARAM_HINTS: Record<string, string> = {
  screenshot: "{}",
  type_text: '{"text": "要输入的文字"}',
  click: '{"x": 500, "y": 300}',
  key: '{"key": "enter"}',
  run_app: '{"app": "Calculator"}',
  run_shell: '{"command": "ls ~/Documents"}',
  list_windows: '{}',
  accessibility_snapshot: '{"app": "Finder", "window": 1, "max_depth": 2}',
  get_window: '{"app": "Finder", "window": 1}',
  find_element: '{"app": "Safari", "query": "搜索"}',
  click_element: '{"ref": "Safari|1|3.1", "action": "AXPress"}',
  set_value: '{"ref": "Safari|1|3.1", "value": "要填的内容"}',
};

export default function DeviceConsolePage() {
  // 激活公司：可订阅 —— 顶栏切公司后权限/审计自动换数据
  const cid = useActiveCompanyId();
  // 契约扫描器无法解析模板里的三元表达式；先落成普通变量
  const cidParam = cid ?? "";
  const [action, setAction] = useState("screenshot");
  const [paramsText, setParamsText] = useState("{}");
  const [result, setResult] = useState<ExecResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);

  // 浏览器抓取
  const [url, setUrl] = useState("https://example.com");
  const [renderJs, setRenderJs] = useState(false);
  const [fetchResult, setFetchResult] = useState<BrowserFetchResult | null>(null);
  const [fetchBusy, setFetchBusy] = useState(false);

  // 权限与日志是两个独立资源：各自缓存/失败互不影响（与原 Promise.allSettled 语义一致）
  const { data: permData, refresh: refreshPerms } = useResource<Perms>(
    cid ? `device-console:perms:${cid}` : null,
    () => api.get<Perms>(`/computer-agent/permissions?company_id=${cid}`),
  );
  const perms = permData ?? {};
  const { data: logData, refresh: refreshLogs } = useResource<LogRow[]>(
    cid ? `device-console:logs:${cid}` : null,
    () => api.get<LogRow[]>(`/computer-agent/logs?company_id=${cid}&limit=30`),
  );
  const logs = logData ?? [];
  const reload = () => { refreshPerms(); refreshLogs(); };

  async function grant(cap: string) {
    try {
      // 原来不看返回就重新加载：授权失败时按钮看起来"点了没反应"
      await api.post(`/computer-agent/grant?capability=${cap}&company_id=${cidParam}`);
      reload();
    } catch (e) { setMsg(`❌ ${describeError(e, "授权失败")}`); }
  }

  async function revoke(cap: string) {
    try {
      await api.post(`/computer-agent/revoke?capability=${cap}&company_id=${cidParam}`);
      reload();
    } catch (e) { setMsg(`❌ ${describeError(e, "撤销失败")}`); }
  }

  async function execute() {
    if (busy) return;
    setBusy(true); setMsg(""); setResult(null);
    let params = {};
    try { params = JSON.parse(paramsText || "{}"); }
    catch { setMsg("❌ 参数不是合法 JSON"); setBusy(false); return; }
    try {
      const data = await api.post<ExecResult>(`/computer-agent/execute?company_id=${cidParam}`,
        { action_type: action, params });
      setResult(data);
      if (action === "screenshot" && data.result?.ok) {
        // 拉取最新截屏 base64
        const imgData = await api.get<{ image_base64?: string }>(
          `/computer-agent/screenshots/latest?company_id=${cidParam}`).catch(() => null);
        if (imgData?.image_base64) setScreenshotUrl(imgData.image_base64);
      }
      setMsg(`✅ 执行完成（${data.mode}）`);
      reload();
    } catch (e) { setMsg(`❌ ${describeError(e, "执行失败")}`); }
    setBusy(false);
  }

  async function runFetch() {
    if (fetchBusy) return;
    setFetchBusy(true); setFetchResult(null);
    try {
      setFetchResult(await api.post(`/browser-agent/fetch?company_id=${cidParam}`,
        { url, render: renderJs ? "js" : null }));
    } catch (e) { setFetchResult({ error: describeError(e, "抓取失败") }); }
    setFetchBusy(false);
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)",
          border: "1px solid rgba(99,102,241,0.22)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, #6366f1, transparent 65%)" }} />
        <div className="relative">
          <h1 className="text-2xl font-bold tracking-tight">🖥 设备操作台</h1>
          <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
            Agent 真实操作电脑 · 浏览器抓取 · 全程审计
          </p>
          <Link href="/plugins"
            className="btn-ghost !py-1.5 !px-3 text-xs inline-flex mt-3">
            ⊞ 打开插件库与电脑权限
          </Link>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-5 items-start">
        {/* 左栏：执行区 */}
        <div className="space-y-5 min-w-0">
          {/* 操作面板 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">本机操作</h2>
            <div className="space-y-3 mb-3">
              {ACTION_GROUPS.map((g) => (
                <div key={g.title}>
                  <p className="text-[10px] mb-1.5" style={{ color: "var(--muted)" }}>
                    {g.title} · {g.note}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {g.items.map((a) => (
                      <button key={a.type}
                        onClick={() => { setAction(a.type); setResult(null); }}
                        className={`px-3 py-1.5 rounded-full text-xs transition-all border ${
                          action === a.type ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                                            : "hover:bg-white/[0.04]"}`}
                        style={action === a.type ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                        {a.icon} {a.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            <label className="text-xs block mb-1.5" style={{ color: "var(--muted)" }}>参数（JSON）</label>
            <textarea rows={3} value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
              placeholder={PARAM_HINTS[action]}
              className="input font-mono text-xs" />
            <div className="mt-2 flex items-center gap-3">
              <button onClick={execute} disabled={busy}
                className="btn-primary disabled:opacity-40">
                {busy ? "执行中…" : "▶ 执行"}
              </button>
              {!perms[action] && perms && Object.keys(perms).length > 0 && (
                <span className="text-[11px] text-amber-400">
                  ⚠️ 此能力未授权，将返回403 —— 请在右侧授权
                </span>
              )}
              {perms[action] && !READ_ONLY_CAPS.has(action) && (
                <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                  已授权 · 写操作仍按「插件库 → 电脑权限」的审批模式处理
                </span>
              )}
            </div>
            {msg && <p className="mt-2 text-sm text-indigo-300">{msg}</p>}

            {/* 截图预览 */}
            {screenshotUrl && action === "screenshot" && (
              <div className="mt-4">
                <p className="text-xs mb-2" style={{ color: "var(--muted)" }}>
                  📸 已捕获真实屏幕画面
                </p>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={screenshotUrl}
                  alt="最新截屏"
                  className="rounded-xl border w-full"
                  style={{ borderColor: "var(--border)" }} />
              </div>
            )}

            {/* 结果展示 */}
            {result && (
              <pre className="mt-4 p-3 rounded-xl text-[11px] whitespace-pre-wrap overflow-auto max-h-56"
                style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                {JSON.stringify(result.result ?? result, null, 2)}
              </pre>
            )}
          </div>

          {/* 浏览器抓取 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">🌐 浏览器抓取</h2>
            <div className="flex gap-2 mb-2">
              <input value={url} onChange={(e) => setUrl(e.target.value)}
                placeholder="https://…"
                className="input flex-1 !py-2 text-xs" />
              <button onClick={runFetch} disabled={fetchBusy || !cid}
                className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40">
                {fetchBusy ? "抓取中…" : "抓取"}
              </button>
            </div>
            <label className="inline-flex items-center gap-1.5 text-xs cursor-pointer select-none"
              style={{ color: "var(--muted)" }}>
              <input type="checkbox" checked={renderJs}
                onChange={(e) => setRenderJs(e.target.checked)}
                className="accent-indigo-500 w-3.5 h-3.5" />
              JS 渲染模式（Playwright headless Chromium）
            </label>
            {renderJs && (
              <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                首次使用需下载 Chromium；未就绪时自动回退静态抓取。
              </p>
            )}
            {fetchResult && (
              <div className="mt-3 rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
                <div className="px-3 py-2 border-b text-[11px] flex gap-3"
                  style={{ background: "var(--surface-2)", borderColor: "var(--border)", color: "var(--muted)" }}>
                  {fetchResult.title && <span>📌 {fetchResult.title}</span>}
                  {fetchResult.rendered !== undefined && (
                    <span className={fetchResult.rendered ? "text-emerald-400" : ""}>
                      {fetchResult.rendered ? "✓ JS渲染" : "静态抓取"}
                    </span>
                  )}
                </div>
                <pre className="p-3 text-[11px] whitespace-pre-wrap max-h-64 overflow-auto leading-relaxed"
                  style={{ color: "var(--foreground)" }}>{fetchResult.text || fetchResult.error}</pre>
              </div>
            )}
          </div>
        </div>

        {/* 右栏：权限 + 审计日志 */}
        <div className="space-y-5 min-w-0">
          <AppAliasSection cid={cid ?? ""} />

          {/* 权限管理 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">🔐 能力授权</h2>
            <div className="space-y-2">
              {Object.entries(perms).map(([cap, granted]) => (
                <div key={cap} className="flex items-center justify-between px-3 py-2 rounded-xl gap-2"
                  style={{ background: "var(--surface-2)" }}>
                  <span className="text-xs truncate">
                    {CAP_LABELS[cap] ?? cap}
                    <span className="ml-1.5 text-[10px] opacity-60">
                      {READ_ONLY_CAPS.has(cap) ? "只读" : "写"}
                    </span>
                  </span>
                  {granted ? (
                    <button onClick={() => revoke(cap)}
                      className="text-[11px] px-2 py-1 rounded-lg hover:bg-red-500/12 text-emerald-300 transition-colors"
                      style={{ border: "1px solid rgba(16,185,129,0.3)" }}>
                      ✓ 已授权 · 点击撤销
                    </button>
                  ) : (
                    <button onClick={() => grant(cap)}
                      className="text-[11px] px-2 py-1 rounded-lg hover:bg-white/[0.06] transition-colors"
                      style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                      授权
                    </button>
                  )}
                </div>
              ))}
              {!Object.keys(perms).length && (
                <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>加载中…</p>
              )}
            </div>
            <p className="text-[10px] mt-3 leading-relaxed" style={{ color: "var(--muted)" }}>
              ⚠️ 首次使用需在 macOS「系统设置 → 隐私与安全性」中为终端 App 开启
              屏幕录制（截屏）和辅助功能（读界面结构、点击、赋值）。
              点元素 / 填文本要先用「界面快照」或「找元素」拿到 ref，
              界面一变 ref 就可能失效，重新取一次即可。
            </p>
          </div>

          {/* 审计日志 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">📜 操作审计（{logs.length}）</h2>
            <div className="space-y-1 max-h-96 overflow-y-auto">
              {logs.map((l) => (
                <details key={l.id} className="rounded-xl px-3 py-2 transition-colors"
                  style={{ background: "var(--surface-2)" }}>
                  <summary className="text-xs cursor-pointer flex items-center gap-2 list-none">
                    <span className={l.allowed ? "text-emerald-400" : "text-red-400"}>
                      {l.allowed ? "✓" : "✕"}
                    </span>
                    <span className="font-medium">{CAP_LABELS[l.action] ?? l.action}</span>
                    <span className="ml-auto text-[10px] tabular-nums shrink-0"
                      style={{ color: "var(--muted)" }}>
                      {l.created_at.slice(5, 16).replace("T", " ")}
                    </span>
                  </summary>
                  <pre className="text-[10px] mt-1 whitespace-pre-wrap overflow-auto max-h-32"
                    style={{ color: "var(--muted)" }}>
                    {l.deny_reason || JSON.stringify(l.result, null, 2)}
                  </pre>
                </details>
              ))}
              {!logs.length && (
                <p className="text-xs text-center py-4" style={{ color: "var(--muted)" }}>暂无记录</p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
