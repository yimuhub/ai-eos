"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { SetupStatus } from "@/lib/setup";
import { useUIMode } from "@/lib/ui-mode";

/** 首页按模式落点：经典模式回指挥中心，极客模式进工作台。 */
export default function Home() {
  const router = useRouter();
  const mode = useUIMode();
  // null = 还在探测；false = 已初始化（照常分流）；true = 首次使用（去安装引导）
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);

  // 首次使用：先去安装引导，别把用户丢进一个空系统的指挥中心。
  // 状态接口失败时按"已初始化"处理——不能因为后端临时不可用就把人锁在安装页。
  useEffect(() => {
    let cancelled = false;
    api
      .get<SetupStatus>("/setup/status", { silent: true, timeoutMs: 8_000 })
      .then((s) => {
        if (!cancelled) setNeedsSetup(s?.needs_setup === true);
      })
      .catch(() => {
        if (!cancelled) setNeedsSetup(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (needsSetup === null) return;
    if (needsSetup) {
      router.replace("/setup");
      return;
    }
    if (mode === "classic") router.replace("/dashboard");
    else if (mode === "geek") router.replace("/workspace");
  }, [needsSetup, mode, router]);

  return (
    <div className="min-h-[60vh] flex items-center justify-center">
      <span
        className="w-5 h-5 rounded-full border-2 animate-spin"
        style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
        aria-hidden
      />
    </div>
  );
}
