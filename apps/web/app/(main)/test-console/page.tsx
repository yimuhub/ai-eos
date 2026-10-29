"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";

/* ============================================================
   全功能测试台（开发/测试辅助工具）
   数据源: GET /test-console/meta · 生产环境默认隐藏
   ============================================================ */

type FnItem = { name: string; desc: string; route: string | null; api: string | null };
type Module = { module: string; items: FnItem[] };
type Meta = { enabled: boolean; modules: Module[]; stats: { modules: number; functions: number } };

export default function TestConsolePage() {
  const router = useRouter();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [search, setSearch] = useState("");
  const [moduleFilter, setModuleFilter] = useState("全部");

  const load = useCallback(async () => {
    try {
      // 这个页面依赖"接口是否存在"来决定是否显示（生产环境后端会返回 404）。
      // 走统一 api 层后 404 是 ApiError.isNotFound —— 比手工比 r.status 更明确，
      // 而且不会再因为"忘了带鉴权头"而把 401 误判成 404。
      setMeta(await api.get<Meta>("/test-console/meta"));
      setNotFound(false);
    } catch (e) {
      if (e instanceof ApiError && e.isAuth) return; // 401 由全局层跳登录，此处不误报"未启用"
      setNotFound(true);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const moduleNames = useMemo(
    () => (meta ? ["全部", ...meta.modules.map((m) => m.module)] : ["全部"]),
    [meta]);

  const filteredModules = useMemo(() => {
    if (!meta) return [];
    return meta.modules
      .filter((m) => moduleFilter === "全部" || m.module === moduleFilter)
      .map((m) => ({
        ...m,
        items: m.items.filter(
          (it) => !search ||
            it.name.includes(search) ||
            it.desc.includes(search) ||
            (it.api || "").includes(search)),
      }))
      .filter((m) => m.items.length > 0);
  }, [meta, search, moduleFilter]);

  /* ---------- 未授权 / 关闭 ---------- */
  if (notFound) {
    return (
      <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
        <div className="card card-static text-center py-24">
          <div className="text-5xl mb-4 opacity-30">🔒</div>
          <h1 className="text-lg font-bold mb-2">功能未开放</h1>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            全功能测试台仅在开发/测试环境对开发角色开放
          </p>
          <button onClick={() => router.push("/dashboard")} className="btn-primary mt-6">返回指挥中心</button>
        </div>
      </div>
    );
  }

  if (!meta) {
    return (
      <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
        <p className="text-sm text-center py-20" style={{ color: "var(--muted)" }}>加载中…</p>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头横幅 */}
      <div className="relative overflow-hidden rounded-2xl mb-6 p-6"
        style={{
          background: "linear-gradient(120deg, rgba(16,185,129,0.12), rgba(99,102,241,0.08) 60%, transparent)",
          border: "1px solid rgba(16,185,129,0.25)",
        }}>
        <div className="absolute -right-8 -top-10 w-48 h-48 rounded-full opacity-15 blur-3xl"
          style={{ background: "radial-gradient(circle, #10b981, transparent 65%)" }} />
        <div className="relative flex items-end justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">🧪 全功能测试台</h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              开发辅助工具 · 系统已实现功能总览与快速手动测试入口
            </p>
          </div>
          <span className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full bg-emerald-500/12 text-emerald-300 border border-emerald-500/25">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 pulse-dot" />
            【已实现/可用】{meta.stats.functions} 项功能
          </span>
        </div>
      </div>

      {/* 搜索 + 模块筛选 */}
      <div className="flex items-center gap-2 mb-5 flex-wrap">
        <input placeholder="搜索功能名 / 描述 / API…" value={search}
          onChange={(e) => setSearch(e.target.value)} className="input w-64 !py-2 text-sm" />
        <div className="flex gap-1.5 flex-wrap">
          {moduleNames.map((m) => (
            <button key={m}
              onClick={() => setModuleFilter(m)}
              className={`px-3 py-1.5 rounded-full text-xs transition-all border ${
                moduleFilter === m
                  ? "border-indigo-500 bg-indigo-500/15 text-indigo-200 font-medium"
                  : "hover:bg-white/[0.04]"}`}
              style={moduleFilter === m ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
              {m}
            </button>
          ))}
        </div>
      </div>

      {/* 功能模块分组 */}
      <div className="space-y-8">
        {filteredModules.map((m) => (
          <div key={m.module}>
            <div className="flex items-center gap-3 mb-3">
              <h2 className="text-base font-bold">{m.module}</h2>
              <span className="text-[10px] px-2 py-0.5 rounded-full"
                style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
                {m.items.length} 项
              </span>
              <div className="flex-1 h-px" style={{ background: "var(--border)" }} />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {m.items.map((it) => (
                <div key={it.name + it.api} className="card group relative overflow-hidden flex flex-col">
                  <div className="absolute top-0 left-0 w-full h-[2px]"
                    style={{ background: "linear-gradient(90deg,#10b981,transparent)" }} />

                  <div className="flex items-start justify-between gap-2">
                    <span className="font-medium text-sm truncate">{it.name}</span>
                    <span className="shrink-0 inline-flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/12 text-emerald-300 border border-emerald-500/25">
                      <span className="w-1 h-1 rounded-full bg-emerald-400" />
                      已实现/可用
                    </span>
                  </div>
                  <p className="text-xs mt-1.5 leading-relaxed line-clamp-2 flex-1" style={{ color: "var(--muted)" }}>
                    {it.desc}
                  </p>

                  {/* API 标签 */}
                  {it.api && (
                    <code className="mt-2 text-[10px] px-2 py-0.5 rounded-md self-start"
                      style={{ background: "var(--surface-2)", color: "#67e8f9" }}>
                      {it.api}
                    </code>
                  )}

                  {/* 跳转按钮 */}
                  <div className="mt-3 pt-2.5 border-t" style={{ borderColor: "var(--border)" }}>
                    {it.route ? (
                      <button onClick={() => router.push(it.route!)}
                        className="w-full text-[11px] py-1.5 rounded-lg transition-colors hover:bg-emerald-500/12 text-emerald-300 flex items-center justify-center gap-1"
                        style={{ border: "1px solid rgba(16,185,129,0.3)" }}>
                        打开页面测试 →
                      </button>
                    ) : (
                      <p className="text-[10px] text-center py-1" style={{ color: "var(--muted)" }}>
                        底层引擎能力（无独立页面，经 API 调用）
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}

        {!filteredModules.length && (
          <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
            没有匹配「{search}」的功能
          </div>
        )}
      </div>

      {/* 页脚说明 */}
      <div className="mt-8 card card-static text-center py-4">
        <p className="text-[11px]" style={{ color: "var(--muted)" }}>
          本页面为开发/测试辅助工具 · 生产环境自动隐藏 · 不修改任何业务逻辑
        </p>
      </div>
    </div>
  );
}
