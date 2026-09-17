"use client";
/* ============================================================
   App Alias 模块：跨平台应用别名配置
   任何人在本地部署后自动感知本机系统（macOS/Windows/Linux）
   ============================================================ */
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { notify } from "@/components/Toast";

type AliasRule = {
  id: string; alias: string; app_name: string;
  app_path: string | null; created_at: string;
};
type InstalledApp = {
  name: string; path: string | null;
  source: string; terminal?: boolean;
};
type Platform = { os?: string; hint?: string };
/** 自然语言 → 应用的匹配结果（界面上用到 matched / name / via 三个字段） */
type MatchResult = { matched?: boolean; name?: string; via?: string };
type RunResult = { ok?: boolean; message?: string };

export default function AppAliasSection({ cid }: { cid: string }) {
  const [rules, setRules] = useState<AliasRule[]>([]);
  const [installed, setInstalled] = useState<InstalledApp[]>([]);
  const [platform, setPlatform] = useState<Platform | null>(null);
  const [opened, setOpened] = useState(false);
  const [formAlias, setFormAlias] = useState("");
  const [formApp, setFormApp] = useState("");
  const [formPath, setFormPath] = useState("");
  const [matchText, setMatchText] = useState("");
  const [matchRes, setMatchRes] = useState<MatchResult | null>(null);
  const [running, setRunning] = useState<string | null>(null);

  // 以下三处原来都各自重新读一遍 token 与 base url（同一个文件里重复四遍），
  // 现在统一走 api 层：鉴权头、超时、错误规范化只有一处实现。
  const loadRules = async () => {
    const list = await api.get<AliasRule[]>(`/app-alias/rules?company_id=${cid}`);
    setRules(Array.isArray(list) ? list : []);
  };

  useEffect(() => {
    if (!cid) return;
    (async () => {
      try {
        const [p, inst, r] = await Promise.all([
          api.get<Platform>(`/app-alias/platform?company_id=${cid}`),
          api.get<{ installed?: InstalledApp[] }>(`/app-alias/installed?company_id=${cid}`),
          api.get<AliasRule[]>(`/app-alias/rules?company_id=${cid}`),
        ]);
        setPlatform(p);
        setInstalled(inst.installed || []);
        setRules(Array.isArray(r) ? r : []);
      } catch {
        // 本机能力探测失败（如未授权）不该让整块区域崩掉，保持空态即可
        setInstalled([]); setRules([]);
      }
    })();
  }, [cid]);

  const doRun = async (text: string) => {
    setRunning(text);
    try {
      const d = await api.post<RunResult>(`/app-alias/run?company_id=${cid}`, { text });
      notify(d.ok === false ? "error" : "success", d.message || JSON.stringify(d));
    } catch (e) {
      notify("error", `执行失败：${describeError(e, "执行失败")}`);
    } finally { setRunning(null); }
  };

  const doMatch = async () => {
    try {
      setMatchRes(await api.post<MatchResult>(`/app-alias/match?company_id=${cid}`, { text: matchText }));
    } catch (e) {
      notify("error", `匹配失败：${describeError(e, "匹配失败")}`);
    }
  };

  const doCreateRule = async () => {
    if (!formAlias.trim() || !formApp.trim()) return;
    try {
      await api.post(`/app-alias/rules?company_id=${cid}`,
        { alias: formAlias, app_name: formApp, app_path: formPath || null });
      await loadRules();
      setFormAlias(""); setFormApp(""); setFormPath("");
    } catch (e) {
      notify("error", `新增规则失败：${describeError(e, "新增失败")}`);
    }
  };

  const doDeleteRule = async (id: string) => {
    try {
      await api.del(`/app-alias/rules/${id}?company_id=${cid}`);
      setRules(rules.filter(x => x.id !== id));
    } catch (e) {
      // 原来不看返回就直接从列表里抹掉：删除失败时界面和数据库不一致
      notify("error", `删除规则失败：${describeError(e, "删除失败")}`);
    }
  };

  const osLabel: Record<string, string> = { mac: "macOS", windows: "Windows", linux: "Linux" };
  const pathHint: Record<string, string> = {
    mac: "/Applications/App.app",
    windows: "C:\\\\Program Files\\\\App\\\\app.exe",
    linux: "/usr/bin/app",
  };

  return (
    <div className="mt-8 border-t border-white/10 pt-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <div className="text-lg font-semibold text-white">📱 本机应用别名（自然语言）</div>
          <div className="text-xs text-white/40 mt-1">
            {osLabel[platform?.os || ""] || platform?.os || "未知系统"} · 已扫描 {installed.length} 个应用
            {platform?.os === "windows" && "（开始菜单 .lnk）"}
            {platform?.os === "linux" && "（.desktop 文件）"}
            {platform?.os === "mac" && "（.app 包）"}
          </div>
        </div>
        <button onClick={() => setOpened(v => !v)}
          className="px-3 py-1.5 text-xs rounded-lg bg-white/10 hover:bg-white/20 text-white transition">
          {opened ? "收起" : "配置别名"}
        </button>
      </div>

      {opened && (
        <div className="space-y-4">
          {/* 别名表单 */}
          <div className="p-4 rounded-xl" style={{ background: "var(--surface-2)" }}>
            <div className="text-sm text-white/70 mb-3">添加别名规则</div>
            <div className="flex flex-wrap gap-3 items-end">
              <div className="flex-1 min-w-[140px]">
                <div className="text-xs text-white/40 mb-1">口语词（如：vx、小企鹅）</div>
                <input value={formAlias} onChange={e => setFormAlias(e.target.value)} placeholder="输入口语词"
                  className="w-full rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm text-white placeholder-white/30 focus:outline-none focus:ring-1 focus:ring-indigo-500" />
              </div>
              <div className="flex-1 min-w-[180px]">
                <div className="text-xs text-white/40 mb-1">应用名称</div>
                <select value={formApp} onChange={e => setFormApp(e.target.value)}
                  className="w-full rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-indigo-500">
                  <option value="">选择本机应用...</option>
                  {installed.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
                </select>
              </div>
              <div className="flex-1 min-w-[200px]">
                <div className="text-xs text-white/40 mb-1">路径（可选）</div>
                <input value={formPath} onChange={e => setFormPath(e.target.value)} placeholder={pathHint[platform?.os || ""] || "/path/to/app"}
                  className="w-full rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm text-white placeholder-white/30 focus:outline-none focus:ring-1 focus:ring-indigo-500" />
              </div>
              <button onClick={doCreateRule}
                className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm transition whitespace-nowrap">
                添加
              </button>
            </div>
          </div>

          {/* 别名规则列表 */}
          {rules.length > 0 && (
            <div className="p-4 rounded-xl" style={{ background: "var(--surface-2)" }}>
              <div className="text-sm text-white/70 mb-3">已配置的别名规则</div>
              <div className="space-y-2">
                {rules.map(r => (
                  <div key={r.id} className="flex items-center gap-3 px-3 py-2 rounded-lg bg-white/5">
                    <span className="text-indigo-300 font-mono text-sm">「{r.alias}」</span>
                    <span className="text-white/30">→</span>
                    <span className="text-white text-sm">{r.app_name}</span>
                    {r.app_path && <span className="text-white/30 text-xs font-mono flex-1 truncate">{r.app_path}</span>}
                    <button onClick={() => doDeleteRule(r.id)}
                      className="text-white/30 hover:text-red-400 text-xs transition shrink-0">✕</button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 自然语言匹配测试 */}
          <div className="p-4 rounded-xl" style={{ background: "var(--surface-2)" }}>
            <div className="text-sm text-white/70 mb-3">🔍 自然语言匹配测试</div>
            <div className="flex gap-3">
              <input value={matchText} onChange={e => setMatchText(e.target.value)} placeholder="输入口语，如：帮我打开微信"
                className="flex-1 rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm text-white placeholder-white/30 focus:outline-none focus:ring-1 focus:ring-indigo-500" />
              <button onClick={doMatch}
                className="px-4 py-2 rounded-lg bg-white/10 hover:bg-white/20 text-white text-sm transition">匹配</button>
            </div>
            {matchRes && (
              <div className="mt-3 p-3 rounded-lg bg-white/5 text-sm">
                <div className="text-white/60">
                  匹配结果：
                  {matchRes.matched
                    ? <span className="text-green-400">✅ {matchRes.name}</span>
                    : <span className="text-red-400">❌ 未匹配</span>}
                </div>
                {matchRes.via && <div className="text-white/40 text-xs mt-1">来源：{matchRes.via}</div>}
                <button onClick={() => doRun(matchText)} disabled={running === matchText || !matchRes.matched}
                  className="mt-2 px-3 py-1 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:bg-white/10 text-white text-xs transition">
                  {running === matchText ? "执行中..." : "立即启动"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 已装应用预览 */}
      <div className="mt-4 grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2">
        {installed.slice(0, 24).map(a => (
          <div key={a.name} title={a.path || a.source}
            className="px-2 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs text-white/60 truncate transition cursor-pointer"
            onClick={() => doRun(a.name)}>
            {a.name}
          </div>
        ))}
        {installed.length > 24 && (
          <div className="px-2 py-1.5 text-xs text-white/30">...共{installed.length}个</div>
        )}
      </div>
    </div>
  );
}
