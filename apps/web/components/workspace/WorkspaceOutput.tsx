"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { notify } from "@/components/Toast";
import {
  artifactMeta,
  clock,
  taskMeta,
  toneClass,
  type TraceNode,
  type TraceResp,
  type WsAgent,
  type WsArtifact,
  type WsFile,
} from "@/lib/workspace";

/**
 * 右栏：产出侧。三个视角看同一件事——
 *   浏览器 = 员工读过的/要读的网页（正文阅读视图）
 *   产物   = 已经交出来的文件与成果
 *   轨迹   = 这件事是怎么一步步做成的
 *
 * 宽度由外层控制；这里只负责在任意宽度下都不溢出（min-w-0 + truncate）。
 */

export type OutTab = "browse" | "artifacts" | "trace";

const TABS: { key: OutTab; label: string; icon: string }[] = [
  { key: "browse", label: "浏览器", icon: "🌐" },
  { key: "artifacts", label: "产物", icon: "▣" },
  { key: "trace", label: "轨迹", icon: "🧭" },
];

function SectionTitle({ children, extra }: { children: React.ReactNode; extra?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 px-1 pt-4 pb-1.5 first:pt-1">
      <span className="text-[9px] font-semibold tracking-[0.16em] uppercase" style={{ color: "#55555f" }}>
        {children}
      </span>
      {extra && <span className="ml-auto text-[9px]" style={{ color: "#55555f" }}>{extra}</span>}
    </div>
  );
}

function Empty({ icon, title, hint }: { icon: string; title: string; hint?: string }) {
  return (
    <div className="py-10 text-center">
      <div className="text-lg opacity-60" aria-hidden>{icon}</div>
      <p className="text-xs mt-1.5" style={{ color: "var(--muted)" }}>{title}</p>
      {hint && <p className="text-[10px] mt-1 leading-relaxed" style={{ color: "#55555f" }}>{hint}</p>}
    </div>
  );
}

export default function WorkspaceOutput({
  tab,
  onTab,
  agent,
  cid,
}: {
  tab: OutTab;
  onTab: (t: OutTab) => void;
  agent: WsAgent | null;
  cid: string;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--surface)" }}>
      {/* 产出是右栏固定的第一个标签，这里只放它自己的三个视角 */}
      <div className="flex items-center gap-1 px-2 shrink-0"
        style={{ height: 40, borderBottom: "1px solid var(--border)" }}>
        {TABS.map((t) => {
          const active = t.key === tab;
          return (
            <button key={t.key} type="button" onClick={() => onTab(t.key)}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] transition-colors ${
                active ? "text-white" : "hover:bg-white/[0.05]"
              }`}
              style={active
                ? { background: "rgba(99,102,241,0.16)", border: "1px solid rgba(99,102,241,0.32)" }
                : { color: "var(--muted)", border: "1px solid transparent" }}>
              <span aria-hidden>{t.icon}</span>
              {t.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto ws-scroll px-3 pb-4">
        {tab === "browse" && <BrowsePane cid={cid} agent={agent} />}
        {tab === "artifacts" && <ArtifactsPane cid={cid} agent={agent} />}
        {tab === "trace" && <TracePane cid={cid} agent={agent} />}
      </div>
    </div>
  );
}

/* ---------------- 浏览器 ---------------- */

function BrowsePane({ cid, agent }: { cid: string; agent: WsAgent | null }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState<{ url: string; title?: string | null; text: string } | null>(null);
  const [err, setErr] = useState("");

  const go = useCallback(async (raw: string) => {
    let target = raw.trim();
    if (!target || !cid) return;
    if (!/^https?:\/\//i.test(target)) target = `https://${target}`;
    setBusy(true);
    setErr("");
    try {
      const d = await api.post<{ text: string; title?: string | null }>(
        `/browser-agent/fetch?company_id=${encodeURIComponent(cid)}${agent ? `&agent_id=${encodeURIComponent(agent.id)}` : ""}`,
        { url: target, max_chars: 12000 },
        { timeoutMs: 60_000 },
      );
      setPage({ url: target, title: d.title, text: d.text });
      setUrl(target);
    } catch (e) {
      setErr(describeError(e, "抓取失败"));
      setPage(null);
    } finally {
      setBusy(false);
    }
  }, [cid, agent]);

  return (
    <div className="pt-2">
      <form
        onSubmit={(e) => { e.preventDefault(); go(url); }}
        className="flex items-center gap-1.5">
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="输入网址，看员工能读到什么"
          aria-label="网址"
          className="flex-1 min-w-0 text-[11px] px-2.5 py-2 rounded-lg outline-none focus:border-indigo-500/50"
          style={{ border: "1px solid var(--border)", background: "var(--surface-2)", color: "var(--foreground)" }}
        />
        <button type="submit" disabled={busy || !url.trim() || !cid}
          className="text-[11px] px-2.5 py-2 rounded-lg font-medium disabled:opacity-40 shrink-0"
          style={{ background: "linear-gradient(135deg,#6366f1,#4f46e5)", color: "#fff" }}>
          {busy ? "读取中" : "读取"}
        </button>
      </form>
      <p className="text-[10px] mt-1.5 px-1 leading-relaxed" style={{ color: "#55555f" }}>
        这是员工视角的正文阅读视图（走安全抓取与审计）。需要点击、登录、填表的交互式浏览请到
        <Link href="/device-console" className="text-indigo-300 ml-0.5">设备操作台</Link>。
      </p>

      {err && (
        <div className="mt-3 rounded-lg px-2.5 py-2 text-[11px]"
          style={{ border: "1px solid rgba(239,68,68,0.3)", background: "rgba(239,68,68,0.08)", color: "#fca5a5" }}>
          {err}
        </div>
      )}

      {busy && (
        <div className="mt-6 flex items-center justify-center gap-2 text-[11px]" style={{ color: "var(--muted)" }}>
          <span className="w-3 h-3 rounded-full border-2 animate-spin"
            style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} aria-hidden />
          正在抓取页面…
        </div>
      )}

      {!busy && page && (
        <div className="mt-3">
          <div className="flex items-center gap-2">
            <p className="text-[11px] font-medium truncate flex-1">{page.title || page.url}</p>
            <a href={page.url} target="_blank" rel="noreferrer noopener"
              className="text-[10px] shrink-0 text-indigo-300 hover:text-indigo-200">
              在浏览器打开 ↗
            </a>
          </div>
          <p className="text-[9px] truncate mt-0.5" style={{ color: "#55555f" }}>{page.url}</p>
          <div className="mt-2 rounded-lg p-2.5 text-[11px] leading-relaxed whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto ws-scroll"
            style={{ border: "1px solid var(--border)", background: "var(--surface-2)", color: "#c9cad5" }}>
            {page.text || "（页面没有可读正文，可能需要登录或依赖脚本渲染）"}
          </div>
        </div>
      )}

      {!busy && !page && !err && (
        <Empty icon="🌐" title="把网址丢进来，看看员工读到什么"
          hint="抓取结果就是员工做任务时看到的内容——排版乱了、要点缺了，产出自然也会偏。" />
      )}
    </div>
  );
}

/* ---------------- 产物 ---------------- */

function ArtifactsPane({ cid, agent }: { cid: string; agent: WsAgent | null }) {
  const [files, setFiles] = useState<WsFile[] | null>(null);
  const [arts, setArts] = useState<WsArtifact[] | null>(null);
  const [err, setErr] = useState("");
  const [downloading, setDownloading] = useState<string>("");
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++reqRef.current;
    setErr("");
    const jobs: Promise<void>[] = [];
    if (cid) {
      jobs.push(
        api.get<WsArtifact[]>(`/artifacts?company_id=${encodeURIComponent(cid)}`)
          .then((d) => { if (seq === reqRef.current) setArts(Array.isArray(d) ? d : []); })
          .catch((e) => { if (seq === reqRef.current) { setArts([]); setErr(describeError(e, "成果列表加载失败")); } }),
      );
    }
    if (agent) {
      jobs.push(
        api.get<{ files: WsFile[] }>(`/delivery/agents/${agent.id}/folder`)
          .then((d) => { if (seq === reqRef.current) setFiles(Array.isArray(d.files) ? d.files : []); })
          .catch(() => { if (seq === reqRef.current) setFiles([]); }),
      );
    } else {
      setFiles(null);
    }
    await Promise.all(jobs);
  }, [cid, agent]);

  useEffect(() => { load(); }, [load]);

  async function download(f: WsFile) {
    if (!f.id) return;
    setDownloading(f.id);
    try {
      downloadURL(await fetchBlobURL(`/delivery/download/${f.id}`), f.name);
    } catch (e) {
      notify("error", `下载失败：${describeError(e)}`);
    } finally {
      setDownloading("");
    }
  }

  return (
    <div className="pt-1">
      <SectionTitle extra={
        <button type="button" onClick={load} className="hover:text-gray-300">刷新</button>
      }>
        {agent ? `${agent.name} 的交付` : "交付文件"}
      </SectionTitle>

      {err && <p className="px-1 text-[10px]" style={{ color: "#fca5a5" }}>{err}</p>}

      {!agent && (
        <p className="px-1 text-[11px]" style={{ color: "var(--muted)" }}>
          选中一个作战或员工后，这里显示他交出来的文件。
        </p>
      )}

      {agent && files === null && <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>读取中…</p>}
      {agent && files?.length === 0 && (
        <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>还没有交付文件。</p>
      )}
      {!!files?.length && (
        <div className="space-y-0.5">
          {files.map((f) => (
            <div key={f.id || f.name}
              className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-white/[0.04]">
              <span className="text-[11px] shrink-0 opacity-70" aria-hidden>📄</span>
              <span className="flex-1 min-w-0 text-[11px] truncate" title={f.name}>{f.name}</span>
              {typeof f.size === "number" && (
                <span className="text-[9px] shrink-0" style={{ color: "#55555f" }}>{fmtSize(f.size)}</span>
              )}
              {f.id && (
                <button type="button" onClick={() => download(f)} disabled={downloading === f.id}
                  className="text-[10px] shrink-0 text-indigo-300 hover:text-indigo-200 disabled:opacity-40">
                  {downloading === f.id ? "…" : "下载"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <SectionTitle extra={
        <Link href="/artifacts" className="hover:text-gray-300">全部 →</Link>
      }>
        成果库 {(arts?.length ?? 0) > 0 ? `· ${arts?.length}` : ""}
      </SectionTitle>
      {arts === null && <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>读取中…</p>}
      {arts?.length === 0 && <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>还没有成果。</p>}
      {!!arts?.length && (
        <div className="space-y-0.5">
          {arts.slice(0, 20).map((a) => {
            const m = artifactMeta(a.status);
            return (
              <Link key={a.id} href="/artifacts"
                className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-white/[0.04]">
                <span className="flex-1 min-w-0 text-[11px] truncate" title={a.name}>{a.name}</span>
                <span className="text-[9px] shrink-0" style={{ color: "#55555f" }}>v{a.version}</span>
                <span className={`text-[9px] px-1.5 py-0.5 rounded-full shrink-0 ${m.cls}`}>{m.label}</span>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ---------------- 轨迹 ---------------- */

type TaskRow = { id: string; title: string; status: string; created_at?: string };

function TracePane({ cid, agent }: { cid: string; agent: WsAgent | null }) {
  const [tasks, setTasks] = useState<TaskRow[] | null>(null);
  const [taskId, setTaskId] = useState("");
  const [data, setData] = useState<TraceResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  // 选中员工就拉他的任务，自动停在最值得看的一个（正在跑 > 待确认 > 最近）
  useEffect(() => {
    if (!agent || !cid) { setTasks(null); setTaskId(""); return; }
    let alive = true;
    setTasks(null);
    api.get<TaskRow[]>(`/tasks?company_id=${encodeURIComponent(cid)}&agent_id=${encodeURIComponent(agent.id)}`)
      .then((d) => {
        if (!alive) return;
        const rows = Array.isArray(d) ? d : [];
        setTasks(rows);
        const hot = rows.find((t) => t.status === "running") || rows.find((t) => t.status === "submitted");
        setTaskId(hot?.id || rows[0]?.id || "");
      })
      .catch((e) => { if (alive) { setTasks([]); setErr(describeError(e, "任务列表加载失败")); } });
    return () => { alive = false; };
  }, [cid, agent]);

  useEffect(() => {
    if (!taskId) { setData(null); return; }
    let alive = true;
    setLoading(true);
    setErr("");
    api.get<TraceResp>(`/tasks/${taskId}/trace`)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) { setData(null); setErr(describeError(e, "轨迹加载失败")); } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [taskId]);

  if (!agent) {
    return <Empty icon="🧭" title="先选一个员工，看这活是怎么干成的"
      hint="轨迹把每一步的执行、成本、失败原因放在同一条时间线上。" />;
  }

  const task = tasks?.find((t) => t.id === taskId) || null;

  return (
    <div className="pt-1">
      <SectionTitle extra={tasks ? `${tasks.length} 个任务` : "读取中"}>
        {agent.name} 的近期任务
      </SectionTitle>

      {tasks?.length === 0 && (
        <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>
          还没有任务。在中间下一句话，或者去<Link href="/tasks" className="text-indigo-300 mx-0.5">任务</Link>页派活。
        </p>
      )}

      {!!tasks?.length && (
        <div className="flex flex-wrap gap-1">
          {tasks.slice(0, 10).map((t) => {
            const m = taskMeta(t.status);
            const active = t.id === taskId;
            return (
              <button key={t.id} type="button" onClick={() => setTaskId(t.id)}
                className={`text-[10px] px-2 py-1 rounded-md border max-w-full truncate ${active ? "text-white" : ""}`}
                style={active
                  ? { borderColor: "rgba(99,102,241,0.5)", background: "rgba(99,102,241,0.16)" }
                  : { borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}
                title={`${t.title} · ${m.label}`}>
                {t.title}
                <span className={`ml-1 ${active ? "" : "opacity-70"}`}>· {m.label}</span>
              </button>
            );
          })}
        </div>
      )}

      {task && (
        <div className="mt-3 rounded-lg p-2.5" style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
          <div className="flex items-center gap-2">
            <p className="text-[11px] font-medium truncate flex-1" title={task.title}>{task.title}</p>
            <span className={`text-[9px] px-1.5 py-0.5 rounded-full border shrink-0 ${toneClass(taskMeta(task.status).tone)}`}>
              {taskMeta(task.status).label}
            </span>
          </div>
          <p className="text-[10px] mt-1" style={{ color: "#55555f" }}>
            {data?.total
              ? `${data.total.executions} 次执行 · ${data.total.calls} 次调用 · ¥${data.total.cost}`
              : "读取轨迹…"}
            {data?.total?.failed ? ` · ${data.total.failed} 次失败` : ""}
          </p>
          <Link href={`/tasks/${task.id}`} className="inline-block text-[10px] mt-1.5 text-indigo-300">
            打开任务详情 →
          </Link>
        </div>
      )}

      {loading && <p className="px-1 py-3 text-[11px]" style={{ color: "var(--muted)" }}>读取轨迹…</p>}
      {err && <p className="px-1 py-3 text-[11px]" style={{ color: "#fca5a5" }}>{err}</p>}

      {!loading && data && (
        <div className="mt-3">
          {!data.nodes.length && (
            <p className="px-1 py-2 text-[11px]" style={{ color: "var(--muted)" }}>这个任务还没有执行记录。</p>
          )}
          <ol className="relative pl-4">
            <span className="absolute left-[5px] top-1 bottom-1 w-px" style={{ background: "var(--border)" }} aria-hidden />
            {data.nodes.map((n) => <Node key={`${n.kind}-${n.id}`} n={n} />)}
          </ol>
        </div>
      )}
    </div>
  );
}

function Node({ n }: { n: TraceNode }) {
  if (n.kind === "boss") {
    return (
      <li className="relative pb-3">
        <span className="absolute -left-4 top-1.5 w-2.5 h-2.5 rounded-full"
          style={{ background: "#f59e0b", boxShadow: "0 0 0 3px rgba(245,158,11,0.15)" }} aria-hidden />
        <p className="text-[10px] text-amber-300">老板介入 · {n.type} {n.ts ? `· ${clock(n.ts)}` : ""}</p>
        <p className="text-[11px] mt-0.5 break-words" style={{ color: "#c9cad5" }}>{n.content}</p>
      </li>
    );
  }
  if (n.kind === "event") {
    return (
      <li className="relative pb-2.5">
        <span className="absolute -left-4 top-1.5 w-2.5 h-2.5 rounded-full"
          style={{ background: "#4a4b58" }} aria-hidden />
        <p className="text-[10px]" style={{ color: "#55555f" }}>
          {n.event_type} {n.ts ? `· ${clock(n.ts)}` : ""}
        </p>
        {n.detail && <p className="text-[10px] mt-0.5 break-words" style={{ color: "#55555f" }}>{n.detail}</p>}
      </li>
    );
  }
  const failed = n.status === "failed";
  return (
    <li className="relative pb-3">
      <span className="absolute -left-4 top-1.5 w-2.5 h-2.5 rounded-full"
        style={{ background: failed ? "var(--danger)" : "#6366f1",
                 boxShadow: `0 0 0 3px ${failed ? "rgba(239,68,68,0.15)" : "rgba(99,102,241,0.15)"}` }} aria-hidden />
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-medium">{n.agent_name}</span>
        <span className="text-[9px] px-1.5 py-0.5 rounded-full border"
          style={{ borderColor: failed ? "rgba(239,68,68,0.35)" : "var(--border)",
                   color: failed ? "#fca5a5" : "var(--muted)" }}>
          {n.status}
        </span>
        {n.ts && <span className="text-[9px]" style={{ color: "#55555f" }}>{clock(n.ts)}</span>}
        {n.duration_s != null && <span className="text-[9px]" style={{ color: "#55555f" }}>{n.duration_s}s</span>}
        {n.usage && <span className="text-[9px]" style={{ color: "#55555f" }}>¥{n.usage.cost}</span>}
      </div>
      {n.model_id && <p className="text-[9px] mt-0.5" style={{ color: "#55555f" }}>{n.model_id}</p>}
      {(n.input || n.output || n.error) && (
        <div className="mt-1 space-y-0.5">
          {n.input && <p className="text-[10px] break-words" style={{ color: "#6b6c7a" }}>▸ {n.input}</p>}
          {n.output && <p className="text-[10px] break-words" style={{ color: "#a9aab8" }}>◂ {n.output}</p>}
          {n.error && <p className="text-[10px] break-words" style={{ color: "#fca5a5" }}>✕ {n.error}</p>}
        </div>
      )}
      {!!n.checkpoints?.length && (
        <p className="text-[9px] mt-0.5" style={{ color: "#55555f" }}>
          {n.checkpoints.length} 个检查点
        </p>
      )}
    </li>
  );
}
