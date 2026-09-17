"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import AppAliasSection from "@/components/AppAliasSection";

/* ============================================================
   设备操作台：Agent 真实操作电脑（截屏/键入/点击/按键/启应用/Shell）
   + 浏览器抓取（静态 / JS 渲染）
   所有操作有授权检查与审计日志。
   ============================================================ */

type LogRow = {
  id: string; agent_id: string | null; action: string;
  allowed: boolean; deny_reason?: string | null;
  mock: boolean; result: any; created_at: string;
};

const ACTIONS: { type: string; label: string; icon: string }[] = [
  { type: "screenshot", label: "截屏", icon: "📸" },
  { type: "type_text", label: "键入文本", icon: "⌨️" },
  { type: "click", label: "点击坐标", icon: "🖱" },
  { type: "key", label: "按键", icon: "⏎" },
  { type: "run_app", label: "启动应用", icon: "🚀" },
  { type: "run_shell", label: "Shell(白名单)", icon: "💻" },
];

export default function DeviceConsolePage() {
  const [cid, setCid] = useState("");
  const [perms, setPerms] = useState<Record<string, boolean>>({});
  const [action, setAction] = useState("screenshot");
  const [paramsText, setParamsText] = useState("{}");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);

  // 浏览器抓取
  const [url, setUrl] = useState("https://example.com");
  const [renderJs, setRenderJs] = useState(false);
  const [fetchResult, setFetchResult] = useState<any>(null);
  const [fetchBusy, setFetchBusy] = useState(false);

  const tokenRef = useRef<string>("");
  const getToken = () => tokenRef.current || (tokenRef.current = localStorage.getItem("aieos_token") || "");

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setCid(cs[0].id);
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/permissions?company_id=${cs[0].id}`,
        { headers: { Authorization: `Bearer ${getToken()}` } });
      setPerms(await r.json());
    } catch {}
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/logs?company_id=${cs[0].id}&limit=30`,
        { headers: { Authorization: `Bearer ${getToken()}` } });
      setLogs(await r.json());
    } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

  async function grant(cap: string) {
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/grant?capability=${cap}&company_id=${cid}`,
      { method: "POST", headers: { Authorization: `Bearer ${getToken()}` } });
    load();
  }

  async function revoke(cap: string) {
    await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/revoke?capability=${cap}&company_id=${cid}`,
      { method: "POST", headers: { Authorization: `Bearer ${getToken()}` } });
    load();
  }

  async function execute() {
    if (busy) return;
    setBusy(true); setMsg(""); setResult(null);
    let params = {};
    try { params = JSON.parse(paramsText || "{}"); }
    catch { setMsg("❌ 参数不是合法 JSON"); setBusy(false); return; }
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/execute?company_id=${cid}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
          body: JSON.stringify({ action_type: action, params }),
        });
      const data = await r.json();
      if (r.ok) {
        setResult(data);
        if (action === "screenshot" && data.result?.ok) {
          // 拉取最新截屏 base64
          const imgR = await fetch(
            `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/computer-agent/screenshots/latest?company_id=${cid}`,
            { headers: { Authorization: `Bearer ${getToken()}` } });
          if (imgR.ok) {
            const imgData = await imgR.json();
            setScreenshotUrl(imgData.image_base64);
          }
        }
        setMsg(`✅ 执行完成（${data.mode}）`);
        load();
      } else {
        setMsg(`❌ ${typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)}`);
      }
    } catch (e: any) { setMsg(`❌ ${e.message}`); }
    setBusy(false);
  }

  async function runFetch() {
    if (fetchBusy) return;
    setFetchBusy(true); setFetchResult(null);
    try {
      const r = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/browser-agent/fetch?company_id=${cid}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
          body: JSON.stringify({ url, render: renderJs ? "js" : null }),
        });
      const data = await r.json();
      setFetchResult(data);
    } catch (e: any) { setFetchResult({ error: e.message }); }
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
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-5 items-start">
        {/* 左栏：执行区 */}
        <div className="space-y-5 min-w-0">
          {/* 操作面板 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">本机操作</h2>
            <div className="flex flex-wrap gap-2 mb-3">
              {ACTIONS.map((a) => (
                <button key={a.type} onClick={() => { setAction(a.type); setResult(null); }}
                  className={`px-3 py-1.5 rounded-full text-xs transition-all border ${
                    action === a.type ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                                      : "hover:bg-white/[0.04]"}`}
                  style={action === a.type ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                  {a.icon} {a.label}
                </button>
              ))}
            </div>

            <label className="text-xs block mb-1.5" style={{ color: "var(--muted)" }}>参数（JSON）</label>
            <textarea rows={3} value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
              placeholder={{
                screenshot: "{}",
                type_text: '{"text": "要输入的文字"}',
                click: '{"x": 500, "y": 300}',
                key: '{"key": "enter"}',
                run_app: '{"app": "Calculator"}',
                run_shell: '{"command": "ls ~/Documents"}',
              }[action]}
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
          <AppAliasSection cid={cid} />

          {/* 权限管理 */}
          <div className="card card-static">
            <h2 className="font-semibold text-sm mb-3">🔐 能力授权</h2>
            <div className="space-y-2">
              {Object.entries(perms).map(([cap, granted]) => (
                <div key={cap} className="flex items-center justify-between px-3 py-2 rounded-xl"
                  style={{ background: "var(--surface-2)" }}>
                  <span className="text-xs">{cap}</span>
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
              ⚠️ 首次使用截屏/键鼠需在 macOS「系统设置 → 隐私与安全性」中为终端 App 开启屏幕录制和辅助功能权限。
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
                    <span className="font-medium">{l.action}</span>
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
