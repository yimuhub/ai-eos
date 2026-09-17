"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "@/components/Layout";
import { api, getToken } from "@/lib/api";

export default function MainLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [ok, setOk] = useState(false);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    api.get("/auth/me").then(() => setOk(true)).catch(() => {});
  }, []);

  if (!ok) return <div className="min-h-screen bg-gray-950" />;
  return <Layout>{children}</Layout>;
}
