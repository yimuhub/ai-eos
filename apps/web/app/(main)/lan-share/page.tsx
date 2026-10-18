"use client";
/**
 * 📡 局域网共享（V5.3）
 *
 * 桌面版（Mac/Win/Linux）启动后，同局域网的手机扫这里的大二维码
 * → 浏览器直接进 Web 版（响应式布局）；Android App 在「服务器」页
 * 填 API 地址即可接管同一份数据。
 *
 * 数据从哪来：
 *  - 局域网 IP：GET /clients/lan（后端探测本机对外网卡）
 *  - Web 入口：当前页面的 origin（桌面壳 8000 / dev 3000）
 *  - API 地址：从本页对 API_BASE 的相对位置推（壳模式 8001）
 *  - 开机自启：window.aieosDesktop IPC（浏览器访问时置灰）
 */
import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "@/lib/api";
import { notify } from "@/components/Toast";

export default function LanSharePage() {
  const [lanIp, setLanIp] = useState("");
  const [reachable, setReachable] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [apiPort, setApiPort] = useState("");
  const [isDesktop, setIsDesktop] = useState(false);
  const [autoLaunch, setAutoLaunch] = useState(false);
  const [alBusy, setAlBusy] = useState(false);

  useEffect(() => {
    setIsDesktop(!!window.aieosDesktop);
    if (window.aieosDesktop?.getAutoLaunch) {
      window.aieosDesktop.getAutoLaunch().then(setAutoLaunch).catch(() => {});
    }
    // 推断 API 端口——与 lib/api.ts 的 resolveApiBase 同一规则：
    // 前端 8000（便携/桌面壳）→ API 8001；其余（dev 3000）→ API 8000
    const o = window.location.origin;
    const m = o.match(/:(\d+)$/);
    const frontPort = m ? m[1] : "";
    setApiPort(frontPort === "8000" ? "8001" : "8000");
    api.get("/clients/lan").then((d: any) => {
      setLanIp(d.lan_ip || "");
      setReachable(!!d.reachable);
    }).catch(() => {
      // 后端不可达：用当前 hostname（多半用户正从局域网 IP 访问）
      setLanIp(window.location.hostname !== "localhost" ? window.location.hostname : "");
      setReachable(window.location.hostname !== "localhost");
    }).finally(() => setLoaded(true));
  }, []);

  const webUrl = lanIp ? `${window.location.protocol}//${lanIp}${window.location.port ? ":" + window.location.port : ""}` : "";
  const apiUrl = lanIp ? `http://${lanIp}:${apiPort}` : "";

  const toggleAutoLaunch = async () => {
    if (!window.aieosDesktop?.setAutoLaunch) return;
    setAlBusy(true);
    try {
      const next = await window.aieosDesktop.setAutoLaunch(!autoLaunch);
      setAutoLaunch(!!next);
      notify("success", next ? "✅ 已设为开机自启" : "已取消开机自启");
    } catch {
      notify("error", "设置开机自启失败");
    } finally {
      setAlBusy(false);
    }
  };

  const copy = async (txt: string, label: string) => {
    try {
      await navigator.clipboard.writeText(txt);
      notify("success", `${label}已复制`);
    } catch {
      notify("error", "复制失败，请手动选择文本复制");
    }
  };

  return (
    <div className="p-4 flex flex-col h-full gap-3 overflow-auto">
      <div className="flex items-center justify-between fade-up">
        <div>
          <h1 className="text-lg font-semibold">📡 局域网共享</h1>
          <p className="text-xs mt-0.5" style={{ color: "var(--muted-foreground)" }}>
            手机进同一局域网 → 扫码进 Web 版 / App 接管 → 数据实时同步
          </p>
        </div>
        {isDesktop && (
          <button
            onClick={toggleAutoLaunch}
            disabled={alBusy}
            className={`card px-3 py-2 text-xs flex items-center gap-2 transition ${autoLaunch ? "ring-1 ring-[var(--accent)]" : ""}`}
            style={{ borderColor: autoLaunch ? "var(--accent)" : "var(--border)" }}
            title="随系统启动 AI-EOS 桌面版（Mac/Win 登录项 · Linux 桌面自启）"
          >
            <span>{autoLaunch ? "✅" : "⭕"}</span>
            <span>{autoLaunch ? "开机自启：开" : "开机自启：关"}</span>
          </button>
        )}
      </div>

      {!reachable && loaded && (
        <div className="card p-3 text-xs fade-up" style={{ borderColor: "rgba(255,80,80,.35)" }}>
          ⚠️ 没能确定本机局域网地址（可能处于离线/纯本地网络）。若你在用局域网 IP 访问本页，
          二维码地址取当前访问地址，仍可扫码；否则请手动复制下方地址核对。
        </div>
      )}

      <div className="grid gap-3 fade-up" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        {/* 二维码 */}
        <div className="card p-4 flex flex-col items-center gap-3">
          <div className="text-xs" style={{ color: "var(--muted-foreground)" }}>手机扫码 · 同 WiFi 下</div>
          <div className="p-3 rounded-xl" style={{ background: "#fff" }}>
            {webUrl ? (
              <QRCodeSVG value={webUrl} size={190} level="M" />
            ) : (
              <div className="w-[190px] h-[190px] flex items-center justify-center text-xs" style={{ color: "var(--muted-foreground)" }}>
                {loaded ? "无法生成地址" : "加载中…"}
              </div>
            )}
          </div>
          <code className="text-xs px-2 py-1 rounded" style={{ background: "var(--card)" }}>
            {webUrl || "—"}
          </code>
          <button onClick={() => copy(webUrl, "Web 地址")} className="text-xs px-3 py-1.5 rounded-md" style={{ background: "var(--card)" }}>
            📋 复制 Web 地址
          </button>
        </div>

        {/* 步骤 */}
        <div className="card p-4 flex flex-col gap-3">
          <div className="text-sm font-medium">接入方式</div>
          <ol className="text-xs flex flex-col gap-2.5 list-decimal pl-4" style={{ color: "var(--muted-foreground)" }}>
            <li>
              <b style={{ color: "var(--foreground)" }}>手机连同一 WiFi</b>（与本机同一局域网；
              手机 4G 时连不上属正常）
            </li>
            <li>
              <b style={{ color: "var(--foreground)" }}>手机浏览器扫二维码</b> → 直接进 Web 版
              （老板视角 / 任务 / 成果都可看，与桌面版实时同步）
            </li>
            <li>
              <b style={{ color: "var(--foreground)" }}>Android App 接管</b>：App「服务器」页 →
              服务地址填下方 API 地址 → 登录同一账号
            </li>
          </ol>
          <div className="flex flex-col gap-2 mt-auto">
            <div className="flex items-center gap-2 text-xs">
              <span className="shrink-0" style={{ color: "var(--muted-foreground)" }}>API 地址（App 用）</span>
              <code className="flex-1 truncate px-2 py-1 rounded" style={{ background: "var(--card)" }}>{apiUrl || "—"}</code>
              <button onClick={() => copy(apiUrl, "API 地址")} className="px-2 py-1 rounded-md text-xs" style={{ background: "var(--card)" }}>📋</button>
            </div>
          </div>
        </div>
      </div>

      {/* 说明与边界 */}
      <div className="card p-4 text-xs flex flex-col gap-2 fade-up" style={{ color: "var(--muted-foreground)" }}>
        <div className="text-sm font-medium" style={{ color: "var(--foreground)" }}>边界与安全</div>
        <div>· 桌面版后端已监听 <code>0.0.0.0</code>：同局域网设备可访问，<b style={{ color: "var(--foreground)" }}>所有写接口仍需登录</b>，没有账号就进不了数据。</div>
        <div>· 手机浏览器看到的是与桌面版<b style={{ color: "var(--foreground)" }}>同一份数据</b>（本机 SQLite / 服务器 Postgres），操作实时互见。</div>
        <div>· 公司网/多 VLAN 环境下手机与电脑可能不在同一网段 → 扫码后打不开属网络隔离，换热点直连（手机开热点给电脑连）即可。</div>
        <div>· 「开机自启」仅桌面版可用（{!isDesktop ? "你现在在浏览器里，请用桌面版" : "Mac 登录项 / Windows 启动项 / Linux 桌面自启"}）。</div>
        <div>· 一键部署：三端安装包首启即自动备好环境（SQLite + 内置服务），无需配置；服务器形态用「服务器一键部署.sh」。</div>
      </div>
    </div>
  );
}
