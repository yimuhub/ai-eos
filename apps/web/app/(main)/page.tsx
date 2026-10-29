"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useUIMode } from "@/lib/ui-mode";

/** 首页按模式落点：经典模式回指挥中心，极客模式进工作台。 */
export default function Home() {
  const router = useRouter();
  const mode = useUIMode();

  useEffect(() => {
    if (mode === "classic") router.replace("/dashboard");
    else if (mode === "geek") router.replace("/workspace");
  }, [mode, router]);

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
