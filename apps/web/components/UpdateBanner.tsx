/**
 * 自动升级横幅（V5.2）
 *
 * 只在桌面壳（Electron）里工作：启动时问后端 /clients/update/check
 * 是否有比当前壳版本更新的安装包 → 有则底部浮出横幅，点击「立即升级」
 * 交给主进程下载 + 应用（Win 静默安装 / Mac 打开安装器 / Linux 原地替换），
 * 进度与结果通过主进程事件实时显示。
 * 浏览器访问（无 window.aieosDesktop）→ 完全不渲染，零影响。
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE, api } from "@/lib/api";

interface UpdateInfo {
  has_update: boolean;
  latest: string | null;
  notes?: string | null;
  file?: string | null;
  url?: string | null;
  size_mb?: number | null;
  sha256?: string | null;
  message?: string | null;
}

export default function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [phase, setPhase] = useState<string | null>(null); // downloading|installing|restarting|manual|error|done
  const [pct, setPct] = useState(0);
  const [tip, setTip] = useState("");
  const doneRef = useRef(false);

  useEffect(() => {
    const d = window.aieosDesktop;
    if (!d) return;
    let alive = true;

    // 当天点过「稍后」就不再打扰
    const last = localStorage.getItem("aieos-update-dismissed");
    if (last === new Date().toDateString()) setDismissed(true);

    d.getEnv().then(async (env) => {
      try {
        // 收口到 api 层：URL/方法/查询参数与原裸 fetch 完全一致。
        // 保留原语义：cache no-store（更新检查不能被缓存）+ 静默
        // （更新通道不可达不影响主流程，原实现失败时直接 return，
        // silent:true 避免 api 层把这个"可忽略的探测失败"广播成全局红色 Toast）。
        const data = await api.get<UpdateInfo>(
          `/clients/update/check?platform=${env.platform}&arch=${env.arch}&current=${env.version}`,
          { cache: "no-store", silent: true },
        );
        if (!alive) return;
        if (data.has_update && data.url && data.file) setInfo(data);
      } catch { /* 更新通道不可达 → 静默 */ }
    });

    const off = d.onUpdateEvent((e: any) => {
      if (!alive) return;
      if (e.phase === "downloading") { setPhase("downloading"); setPct(e.pct ?? 0); }
      else if (e.phase === "installing") setPhase("installing");
      else if (e.phase === "restarting") setPhase("restarting");
      else if (e.phase === "manual") { setPhase("manual"); setTip(e.tip || ""); }
      else if (e.phase === "error") { setPhase("error"); setTip(e.error || "下载失败"); }
      else if (e.phase === "done") doneRef.current = true;
    });

    return () => { alive = false; off(); };
  }, []);

  // SSR/静态生成加固：服务端 render 体没有 window，必须放在所有渲染逻辑之前。
  // 当前 (main) layout 的 checking 分支挡住 banner 才没出事，不能把安全建立在 UI 排列上。
  if (typeof window === "undefined" || !window.aieosDesktop || dismissed || !info) return null;
  if (phase === "restarting") {
    return <Bar tone="ok">🔄 正在重启，即将自动打开新版本…</Bar>;
  }

  const btn = () => {
    const d = window.aieosDesktop!;
    setPhase("downloading"); setPct(0);
    d.startUpdate({ url: API_BASE + info.url!, file: info.file!, platform: d.platform, sha256: info.sha256 ?? undefined });
  };

  let body: string;
  if (phase === "downloading") body = `正在下载 ${info.file}（${info.size_mb ?? "?"} MB）… ${pct}%`;
  else if (phase === "installing") body = "正在静默安装，即将自动重启…";
  else if (phase === "manual") body = tip || "下载完成，请按提示完成替换";
  else if (phase === "error") body = `升级失败：${tip}`;
  else body = `🆕 新版本 ${info.latest} 可用${info.notes ? "：" + String(info.notes).split("\n")[0].slice(0, 60) : ""}`;

  const busy = phase === "downloading" || phase === "installing";

  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] w-[min(640px,92vw)]">
      <div className={`card px-4 py-3 flex items-center gap-3 shadow-2xl ${phase === "error" ? "ring-1 ring-red-500/50" : ""}`}>
        <div className="flex-1 min-w-0 text-sm">
          <div className="text-zinc-100 font-medium truncate">{body}</div>
          {phase === "downloading" && (
            <div className="mt-2 h-1.5 rounded-full bg-white/10 overflow-hidden">
              <div className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-cyan-400 transition-all"
                   style={{ width: `${pct}%` }} />
            </div>
          )}
        </div>
        {!busy && phase !== "manual" && phase !== "error" && (
          <button onClick={() => { localStorage.setItem("aieos-update-dismissed", new Date().toDateString()); setDismissed(true); }}
                  className="text-xs text-zinc-400 hover:text-zinc-200 px-2 py-1">稍后</button>
        )}
        {phase === "manual" && (
          <button onClick={() => setDismissed(true)} className="text-xs text-zinc-400 hover:text-zinc-200 px-2 py-1">知道了</button>
        )}
        {phase === "error" && (
          <button onClick={btn} className="btn-primary text-xs py-1.5 px-3">重试</button>
        )}
        {!busy && phase !== "manual" && phase !== "error" && (
          <button onClick={btn} className="btn-primary text-xs py-1.5 px-3">立即升级</button>
        )}
      </div>
    </div>
  );
}

function Bar({ children, tone = "ok" }: { children: React.ReactNode; tone?: "ok" | "warn" }) {
  const cls = tone === "warn" ? "ring-1 ring-amber-400/30 text-amber-200" : "ring-1 ring-emerald-400/30 text-emerald-200";
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100]">
      <div className={`card px-5 py-3 shadow-2xl ${cls} text-sm`}>
        {children}
      </div>
    </div>
  );
}
