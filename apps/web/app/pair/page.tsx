"use client";
/**
 * 配对入口（V5.4）：手机扫桌面版二维码 → 打开 http://IP:port/pair?token=***
 * 前端拿 token 调公开 API /pair 换登录 → 存 token → 进首页。
 * 全程免输密码；token 一次性、10 分钟过期，过期/已用都有明确提示。
 */
import { useCallback, useEffect, useState } from "react";
import { API_BASE, setToken, setRefreshToken } from "@/lib/api";
import { useRouter } from "next/navigation";

export default function PairPage() {
  const router = useRouter();
  const [state, setState] = useState<"loading" | "ok" | "bad">("loading");
  const [detail, setDetail] = useState("");

  const run = useCallback(async () => {
    const token = new URLSearchParams(window.location.search).get("token") || "";
    if (!token) { setState("bad"); setDetail("链接缺少配对码。请在电脑端「局域网共享」重新生成。"); return; }
    try {
      const r = await fetch(`${API_BASE}/pair?token=***`, { method: "GET" });
      const d = await r.json();
      if (!r.ok || !d.access_token) {
        setState("bad");
        setDetail(d.detail || "配对失败，请重新生成配对码。");
        return;
      }
      setToken(d.access_token);
      if (d.refresh_token) setRefreshToken(d.refresh_token);
      setState("ok");
      setDetail(`已接入：${d.device?.name || "设备"}`);
      setTimeout(() => router.replace("/"), 600);
    } catch (e: any) {
      setState("bad");
      setDetail("连不上服务器：" + (e?.message || e));
    }
  }, [router]);

  useEffect(() => { run(); }, [run]);

  return (
    <div className="min-h-screen flex items-center justify-center p-6"
         style={{ background: "var(--background)" }}>
      <div className="card p-8 max-w-sm w-full text-center flex flex-col items-center gap-4">
        <div className="text-4xl">{state === "ok" ? "✅" : state === "bad" ? "⚠️" : "⏳"}</div>
        <div>
          <h1 className="text-lg font-semibold">AI-EOS 手机接入</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted-foreground)" }}>
            {state === "ok" ? `${detail}，正在进入…` :
             state === "bad" ? detail : "正在验证配对码…"}
          </p>
        </div>
        {state === "bad" && (
          <div className="flex gap-2 w-full justify-center">
            <button onClick={run} className="text-xs px-3 py-2 rounded-md"
                    style={{ background: "var(--card)" }}>重试</button>
            <button onClick={() => router.replace("/login")}
                    className="text-xs px-3 py-2 rounded-md"
                    style={{ background: "var(--card)" }}>用账号登录</button>
          </div>
        )}
      </div>
    </div>
  );
}
