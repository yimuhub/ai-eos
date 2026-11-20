"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { useResource } from "@/lib/useResource";
import { formatLocalStamp } from "@/lib/remoteDevices";
import { notify } from "@/components/Toast";
import {
  autonomyBreakdown,
  executionStatusLabel,
  executionStatusTone,
  failedChecks,
  formatAutonomy,
  formatCheckValue,
  isTerminalStatus,
  MANUAL_REASONS,
  MANUAL_REASON_LABELS,
  summarizeGates,
  type AutonomyLedger,
  type GateRecord,
  type ManualReason,
} from "@/lib/evidence";
import type { Company } from "@/lib/types";

type Node = {
  node_key: string; node_type: string; name: string;
  config: Record<string, any>; position_x: number; position_y: number;
};
type Edge = { from_node: string; to_node: string; label?: string | null };
type Agent = { id: string; name: string };

type FeedbackItem = {
  id: string; workflow_id: string; success: boolean; score: number;
  lessons: string[]; note: string; source: string; created_at: string;
};

type WorkflowMeta = {
  id: string; name: string; version: number; source: string; status: string;
  score: number | null; run_count: number; success_count: number;
  failure_count: number; reuse_key: string; previous_workflow_id: string;
  feedback: FeedbackItem[];
};

/** GET /executions/{id} 的响应里与证据有关的部分（steps 只取需要的字段） */
type ExecutionDetail = {
  id: string; status: string; error: string | null;
  steps: { node_key: string; node_name: string; node_type: string;
           status: string; stale_at?: string | null }[];
  gates: GateRecord[];
};


const NODE_TYPES = [
  { type: "START", label: "开始", color: "bg-green-700" },
  { type: "END", label: "结束", color: "bg-red-800" },
  { type: "AGENT", label: "Agent执行", color: "bg-blue-700" },
  { type: "CONDITION", label: "条件判断", color: "bg-yellow-700" },
  { type: "PARALLEL", label: "并行", color: "bg-purple-700" },
  { type: "MERGE", label: "汇聚", color: "bg-purple-900" },
  { type: "APPROVAL", label: "人工审批", color: "bg-orange-700" },
  { type: "WAIT", label: "等待", color: "bg-gray-600" },
];

const W = 150, H = 44;

export default function WorkflowBuilderPage() {
  const canvasRef = useRef<HTMLDivElement>(null);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Agent 下拉候选：账号级列表，交给统一数据层（缓存 + 回窗重取）
  const { data: agentData } = useResource<Agent[]>(
    "agents:all",
    () => api.get<Agent[]>("/agents"),
  );
  const agents = useMemo<Agent[]>(() => agentData ?? [], [agentData]);
  const [wfName, setName] = useState("新工作流");
  const [savedId, setSavedId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<string>("");
  const dragRef = useRef<{ key: string; dx: number; dy: number } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);  // 工作流运行轮询句柄
  const linkRef = useRef<{ from: string; x: number; y: number } | null>(null);
  const [linkPreview, setLinkPreview] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);

  /* 从 CEO 指挥台跳转过来时看的是服务端已落库的流程（含版本/评分） */
  const [meta, setMeta] = useState<WorkflowMeta | null>(null);
  const [fbScore, setFbScore] = useState(80);
  const [fbLessons, setFbLessons] = useState("");
  const [fbBusy, setFbBusy] = useState(false);
  const [fbError, setFbError] = useState("");
  const [optimized, setOptimized] = useState<{ id: string; version: number } | null>(null);

  /* 运行证据（M4 前端）：跑完后保留执行详情，而不是只留一行状态文字 */
  const [execution, setExecution] = useState<ExecutionDetail | null>(null);
  const [autonomy, setAutonomy] = useState<AutonomyLedger | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [manual, setManual] = useState<{ reason: ManualReason; minutes: number; note: string }>(
    { reason: "judgment", minutes: 10, note: "" });
  const [manualBusy, setManualBusy] = useState(false);
  const [reworkNode, setReworkNode] = useState("");
  const [reworkText, setReworkText] = useState("");
  const [reworkBusy, setReworkBusy] = useState(false);

  const loadWorkflow = useCallback(async (id: string) => {
    try {
      const wf = await api.get<WorkflowMeta & { nodes: Node[]; edges: Edge[] }>(`/workflows/${id}`);
      setName(wf.name);
      setNodes((wf.nodes || []).map((n) => ({
        node_key: n.node_key, node_type: n.node_type, name: n.name,
        config: n.config || {}, position_x: n.position_x || 0, position_y: n.position_y || 0,
      })));
      setEdges(wf.edges || []);
      setSavedId(wf.id);
      setMeta(wf);
      setSelected(null);
      setOptimized(null);
      setFbError("");
    } catch (e) {
      notify("error", describeError(e, "载入工作流失败"));
    }
  }, []);

  const refreshMeta = useCallback(async (id: string) => {
    try {
      setMeta(await api.get<WorkflowMeta>(`/workflows/${id}`));
    } catch {
      // 元数据刷新失败不影响已经展示的图；下次载入会纠正
    }
  }, []);

  useEffect(() => {
    const open = new URLSearchParams(window.location.search).get("open");
    if (open) void loadWorkflow(open);
  }, [loadWorkflow]);

  async function submitFeedback() {
    if (!savedId || fbBusy) return;
    setFbBusy(true);
    setFbError("");
    try {
      const lessons = fbLessons.split("\n").map((s) => s.trim()).filter(Boolean);
      const res = await api.post<{ workflow: { id: string; version: number }; optimized_from: string }>(
        `/workflows/${savedId}/feedback`,
        { score: fbScore, lessons, note: "工作流编排页人工复盘", optimize: true },
      );
      setFbLessons("");
      await refreshMeta(savedId);
      if (res.optimized_from) {
        setOptimized({ id: res.workflow.id, version: res.workflow.version });
        notify("success", `反馈已记录；低于 80 分，已生成 v${res.workflow.version}`);
      } else {
        setOptimized(null);
        notify("success", "反馈已记录");
      }
    } catch (e) {
      setFbError(describeError(e, "提交反馈失败"));
    } finally {
      setFbBusy(false);
    }
  }


  function addNode(type: string) {
    const key = `n${Date.now().toString(36)}${Math.floor(Math.random() * 100)}`;
    setNodes((ns) => [...ns, {
      node_key: key, node_type: type,
      name: NODE_TYPES.find((t) => t.type === type)?.label || type,
      config: {}, position_x: 40 + Math.random() * 320, position_y: 40 + Math.random() * 220,
    }]);
  }

  /** 从节点右侧圆点按下 → 开始连线 */
  function onPortMouseDown(e: React.MouseEvent, key: string) {
    e.stopPropagation();
    e.preventDefault();
    const rect = canvasRef.current!.getBoundingClientRect();
    const n = nodes.find((x) => x.node_key === key)!;
    linkRef.current = { from: key, x: n.position_x + W, y: n.position_y + H / 2 };
    setLinkPreview({ x1: linkRef.current.x, y1: linkRef.current.y, x2: e.clientX - rect.left, y2: e.clientY - rect.top });
  }

  function onMouseMove(e: React.MouseEvent) {
    const rect = canvasRef.current!.getBoundingClientRect();
    // 连线中：更新预览线
    if (linkRef.current) {
      setLinkPreview({ x1: linkRef.current.x, y1: linkRef.current.y, x2: e.clientX - rect.left, y2: e.clientY - rect.top });
      return;
    }
    // 拖拽节点
    if (dragRef.current) {
      const x = Math.max(0, e.clientX - rect.left - dragRef.current.dx);
      const y = Math.max(0, e.clientY - rect.top - dragRef.current.dy);
      setNodes((ns) => ns.map((n) =>
        n.node_key === dragRef.current!.key ? { ...n, position_x: x, position_y: y } : n));
    }
  }

  /** 松开：若在某个节点上释放则完成连线 */
  function onMouseUp(e: React.MouseEvent) {
    if (linkRef.current) {
      const el = document.elementFromPoint(e.clientX, e.clientY);
      const nodeEl = el?.closest("[data-node-key]") as HTMLElement | null;
      const toKey = nodeEl?.dataset.nodeKey;
      const from = linkRef.current.from;
      if (toKey && toKey !== from && !edges.some((x) => x.from_node === from && x.to_node === toKey)) {
        setEdges((es) => [...es, { from_node: from, to_node: toKey }]);
      }
      linkRef.current = null;
      setLinkPreview(null);
      return;
    }
    dragRef.current = null;
  }

  function deleteNode(key: string) {
    setNodes((ns) => ns.filter((n) => n.node_key !== key));
    setEdges((es) => es.filter((e) => e.from_node !== key && e.to_node !== key));
    setSelected(null);
  }

  const sel = nodes.find((n) => n.node_key === selected) || null;
  const gateInfo = useMemo(() => summarizeGates(execution?.gates), [execution]);
  const reworkable = (execution?.steps ?? []).filter(
    (s) => !["START", "END"].includes(s.node_type));

  // selected 指向的节点已被删除时自动清除
  useEffect(() => {
    if (selected && !nodes.some((n) => n.node_key === selected)) {
      setSelected(null);
    }
  }, [nodes, selected]);

  function updateSelConfig(patch: Record<string, any>) {
    setNodes((ns) => ns.map((n) =>
      n.node_key === selected ? { ...n, config: { ...n.config, ...patch } } : n));
  }

  async function save() {
    const cs = await api.get<Company[]>("/companies");
    const body = {
      name: wfName, nodes: nodes.map(({ node_key, node_type, name, config, position_x, position_y }) =>
        ({ node_key, node_type, name, config, position_x, position_y })),
      edges,
    };
    // 修复：此前每次保存都 POST 新建——同名工作流会越攒越多。
    // 编辑已保存的工作流（savedId 存在）改为 PUT 原地更新、版本 +1。
    const res = savedId
      ? await api.put<{ id: string; name: string; version: number }>(`/workflows/${savedId}`, body)
      : await api.post<{ id: string; name: string; version?: number }>(`/workflows?company_id=${cs[0].id}`, body);
    setSavedId(res.id);
    notify("success", res.version ? `已保存：${res.name}（v${res.version}）` : `已保存：${res.name}`);
    void refreshMeta(res.id);
  }

  /** 拉一次执行详情 + 自主率账本。轮询与返工后都走这里，避免两套解析。 */
  const loadEvidence = useCallback(async (eid: string) => {
    const ex = await api.get<ExecutionDetail>(`/executions/${eid}`);
    setExecution(ex);
    setRunStatus(`${executionStatusLabel(ex.status)}（${ex.steps.length} 步）`);
    // 自主率只在跑完后取：跑一半的分母还在变，提前展示会误导
    if (isTerminalStatus(ex.status)) {
      try {
        setAutonomy(await api.get<AutonomyLedger>(`/executions/${eid}/autonomy`));
      } catch {
        setAutonomy(null);   // 账本失败不影响主流程，页面显示"—"
      }
    }
    return ex;
  }, []);

  /** 开始/重启轮询。抽出来是因为"整轮重跑""改稿"之后同样要接着盯。 */
  const pollExecution = useCallback((eid: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    let fails = 0;
    setRunStatus("运行中…");
    pollRef.current = setInterval(async () => {
      try {
        const ex = await loadEvidence(eid);
        fails = 0;
        if (isTerminalStatus(ex.status)) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        // 轮询失败不崩：连续 3 次失败自动停止并提示（否则每 5s 一条未处理 rejection）
        if (++fails >= 3) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setRunStatus("轮询失败，已停止（刷新页面可查看结果）");
        }
      }
    }, 5000);
  }, [loadEvidence]);

  async function run() {
    if (!savedId) return;
    try {
      const res = await api.post<{ execution_id: string }>(`/workflows/${savedId}/run`);
      setAutonomy(null);
      pollExecution(res.execution_id);
    } catch (e) {
      notify("error", describeError(e, "启动失败"));
    }
  }

  /** 下载单文件报告：必须走 blob（window.open 带不上 Bearer token）。 */
  async function downloadReport() {
    if (!execution || reportBusy) return;
    setReportBusy(true);
    try {
      const url = await fetchBlobURL(`/executions/${execution.id}/report.html`);
      downloadURL(url, `report-${execution.id.slice(0, 8)}.html`);
    } catch (e) {
      notify("error", describeError(e, "下载报告失败"));
    } finally {
      setReportBusy(false);
    }
  }

  async function addManualLog() {
    if (!execution || manualBusy) return;
    setManualBusy(true);
    try {
      await api.post(`/executions/${execution.id}/manual-log`, {
        reason: manual.reason, minutes: manual.minutes, note: manual.note || null });
      setManual((m) => ({ ...m, note: "" }));
      setAutonomy(await api.get<AutonomyLedger>(`/executions/${execution.id}/autonomy`));
      notify("success", "已记一条人工介入，自主率随之更新");
    } catch (e) {
      notify("error", describeError(e, "记录失败"));
    } finally {
      setManualBusy(false);
    }
  }

  /** 返工：node_key 为空 = 整轮重跑；给了 feedback = 原稿改稿。 */
  async function rework(feedback?: string) {
    if (!execution || reworkBusy) return;
    const nodeKey = reworkNode || undefined;
    if (feedback !== undefined && !nodeKey) {
      notify("error", "带意见改稿需要先选中一个节点");
      return;
    }
    setReworkBusy(true);
    try {
      await api.post(`/executions/${execution.id}/rework`, {
        node_key: nodeKey,
        mode: feedback !== undefined ? "revise" : "rerun",
        feedback: feedback ?? null,
      });
      setReworkText("");
      notify("success", feedback !== undefined ? "已交回该员工改稿" : "已重新开跑");
      pollExecution(execution.id);
    } catch (e) {
      notify("error", describeError(e, "返工失败"));
    } finally {
      setReworkBusy(false);
    }
  }
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  return (
    <div className="p-4 flex flex-col h-full">
      <div className="flex items-center gap-3 mb-3">
        <input value={wfName} onChange={(e) => setName(e.target.value)}
          className="bg-white/[0.04] border border-[var(--border)] rounded-md px-3 py-1.5 text-sm w-48" />
        <button onClick={save} className="bg-green-600 hover:bg-green-500 px-4 py-1.5 rounded-md text-sm">保存</button>
        <button onClick={run} disabled={!savedId}
          className="bg-blue-600 hover:bg-blue-500 disabled:opacity-40 px-4 py-1.5 rounded-md text-sm">▶ 运行</button>
        {runStatus && <span className="text-xs text-[var(--muted)]">{runStatus}</span>}
      </div>

      {meta && (
        <div className="flex items-center gap-2 flex-wrap text-[11px] text-[var(--muted)] mb-2">
          <span className="px-1.5 py-0.5 rounded"
            style={{ background: "rgba(34,211,238,0.14)", color: "#a5f3fc" }}>
            v{meta.version}
          </span>
          <span>{meta.source === "ceo" ? "CEO 自动生成" : "手工编排"}</span>
          <span>状态 {meta.status}</span>
          <span>评分 {meta.score == null ? "—" : meta.score.toFixed(1)}</span>
          <span>跑过 {meta.run_count} 次 · 成功 {meta.success_count} · 失败 {meta.failure_count}</span>
          {meta.reuse_key && <span>复用键「{meta.reuse_key}」</span>}
          {meta.previous_workflow_id && <span>由上一版反馈升级</span>}
        </div>
      )}

      <div className="flex gap-2 mb-2 flex-wrap">
        {NODE_TYPES.map((t) => (
          <button key={t.type} onClick={() => addNode(t.type)}
            className={`${t.color} hover:opacity-80 px-2.5 py-1 rounded text-xs`}>＋{t.label}</button>
        ))}
        <span className="text-[10px] text-[var(--muted)] self-center ml-2">
          拖动节点移动｜从节点<b className="text-yellow-400">右侧圆点</b>拖到另一节点连线｜单击选中编辑
        </span>
      </div>

      <div ref={canvasRef} onMouseMove={onMouseMove} onMouseUp={onMouseUp}
        className="relative flex-1 bg-gray-950 border border-[var(--border)] rounded-lg overflow-auto min-h-[420px]"
        onClick={() => setSelected(null)}>

        <svg className="absolute inset-0 pointer-events-none" width="100%" height="100%">
          <defs><marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 z" fill="#6b7280" />
          </marker></defs>
          {edges.map((e, i) => {
            const a = nodes.find((n) => n.node_key === e.from_node);
            const b = nodes.find((n) => n.node_key === e.to_node);
            if (!a || !b) return null;
            const x1 = a.position_x + W, y1 = a.position_y + H / 2;
            const x2 = b.position_x, y2 = b.position_y + H / 2;
            return (
              <g key={i}>
                <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#6b7280" strokeWidth="1.5"
                  markerEnd="url(#arrow)" />
                {e.label && <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 5} fill="#fbbf24" fontSize="11">{e.label}</text>}
              </g>
            );
          })}
          {/* 连线预览 */}
          {linkPreview && (
            <line {...linkPreview} stroke="#fbbf24" strokeWidth="2" strokeDasharray="5,3" />
          )}
        </svg>

        {nodes.map((n) => {
          const color = NODE_TYPES.find((t) => t.type === n.node_type)?.color || "bg-white/[0.15]";
          return (
            <div key={n.node_key} data-node-key={n.node_key}
              onMouseDown={(e) => {
                e.stopPropagation();
                const rect = canvasRef.current!.getBoundingClientRect();
                setSelected(n.node_key);
                dragRef.current = { key: n.node_key, dx: e.clientX - rect.left - n.position_x, dy: e.clientY - rect.top - n.position_y };
              }}
              onClick={(e) => e.stopPropagation()}
              style={{ left: n.position_x, top: n.position_y, width: W }}
              className={`absolute ${color} ${selected === n.node_key ? "ring-2 ring-white" : ""}
                rounded-md px-2 py-2 cursor-move select-none shadow-lg`}>
              <div className="text-xs font-medium truncate">{n.name}</div>
              <div className="text-[9px] opacity-70">{n.node_type}</div>
              {/* 右侧连接端口 */}
              <div onMouseDown={(e) => onPortMouseDown(e, n.node_key)}
                className="absolute -right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 bg-yellow-400 rounded-full cursor-crosshair hover:scale-125 border border-black/60" />
            </div>
          );
        })}
      </div>

      {/* 运行证据：闸门结果 + 自主率 + 报告下载 + 返工。
          没有这层，前面三批机制只存在于数据库里，老板看不到。 */}
      {execution && (
        <div className="mt-3 card-lg p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap text-[11px]">
            <span className="text-xs text-[var(--muted)]">本次运行</span>
            <span className="px-1.5 py-0.5 rounded tabular-nums"
              style={{
                background: executionStatusTone(execution.status) === "ok" ? "rgba(52,211,153,0.14)"
                  : executionStatusTone(execution.status) === "bad" ? "rgba(248,113,113,0.14)"
                  : "rgba(255,255,255,0.06)",
                color: executionStatusTone(execution.status) === "ok" ? "#6ee7b7"
                  : executionStatusTone(execution.status) === "bad" ? "#fca5a5" : "var(--muted)",
              }}>
              {executionStatusLabel(execution.status)}
            </span>
            <span className="text-[var(--muted)]">#{execution.id.slice(0, 8)} · {execution.steps.length} 步</span>
            <span className="text-[var(--muted)]">自主率 <b className="text-[var(--foreground)]">{formatAutonomy(autonomy)}</b></span>
            <button type="button" onClick={() => void downloadReport()} disabled={reportBusy}
              className="btn-primary !py-1 !px-3 text-xs disabled:opacity-40">
              {reportBusy ? "生成中…" : "⬇ 下载报告"}
            </button>
            {isTerminalStatus(execution.status) && (
              <button type="button" onClick={() => void rework()} disabled={reworkBusy}
                className="px-2 py-1 rounded text-xs bg-white/[0.08] hover:bg-white/[0.14] disabled:opacity-40">
                整轮重跑
              </button>
            )}
          </div>

          {execution.error && (
            <div className="text-[11px]" style={{ color: "#fca5a5" }}>{execution.error}</div>
          )}

          <div className="text-[10px] text-[var(--muted)]">{autonomyBreakdown(autonomy)}</div>

          {gateInfo.configured ? (
            <div className="space-y-1">
              <div className="text-[10px] text-[var(--muted)]">
                闸门 {gateInfo.rounds} 轮 · 未通过节点 {gateInfo.failedNodes.length} · 返工 {gateInfo.reworked.length}
                {gateInfo.degraded > 0 ? ` · 降级放行 ${gateInfo.degraded}` : ""}
              </div>
              {execution.gates.map((g, i) => {
                const bad = failedChecks(g);
                return (
                  <div key={`${g.node_key}-${i}`} className="text-[10px] rounded-md px-2 py-1"
                    style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                    <span className="font-medium">{g.node_name || g.node_key}</span>
                    <span className="text-[var(--muted)]"> · 第 {g.gate_round ?? 1} 轮 · {g.stage || "gate"}</span>
                    <span style={{ color: g.passed ? "#6ee7b7" : "#fca5a5" }}>
                      {" "}{g.passed ? "通过" : "未通过"}
                    </span>
                    {g.degraded && <span className="text-[var(--muted)]">（降级）</span>}
                    {typeof g.score === "number" && (
                      <span className="text-[var(--muted)]"> · 评审 {g.score.toFixed(2)}</span>
                    )}
                    {bad.length > 0 && (
                      <div className="text-[var(--muted)]">
                        {bad.map((c) => (
                          <div key={c.rule}>
                            × {c.rule}：实际 {formatCheckValue(c.actual)} / 要求 {formatCheckValue(c.expected)}
                            {c.detail ? `（${c.detail}）` : ""}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-[10px] text-[var(--muted)]">该流程没有配置验收闸门（可在节点 config.gate 里声明）</div>
          )}

          {isTerminalStatus(execution.status) && (
            <div className="flex items-center gap-2 flex-wrap pt-1"
              style={{ borderTop: "1px dashed var(--border)" }}>
              <span className="text-[10px] text-[var(--muted)]">记一条人工介入</span>
              <select value={manual.reason}
                onChange={(e) => setManual((m) => ({ ...m, reason: e.target.value as ManualReason }))}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px]">
                {MANUAL_REASONS.map((r) => (
                  <option key={r} value={r}>{MANUAL_REASON_LABELS[r]}</option>
                ))}
              </select>
              <input type="number" min={0} max={1440} value={manual.minutes}
                onChange={(e) => setManual((m) => ({ ...m, minutes: Number(e.target.value) }))}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] w-16"
                aria-label="人工介入分钟" />
              <span className="text-[10px] text-[var(--muted)]">分钟</span>
              <input value={manual.note}
                onChange={(e) => setManual((m) => ({ ...m, note: e.target.value }))}
                placeholder="做了什么（可空）"
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] flex-1 min-w-[140px]" />
              <button type="button" onClick={() => void addManualLog()} disabled={manualBusy}
                className="px-2 py-1 rounded text-[10px] bg-white/[0.08] hover:bg-white/[0.14] disabled:opacity-40">
                {manualBusy ? "记录中…" : "记录"}
              </button>
            </div>
          )}

          {isTerminalStatus(execution.status) && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] text-[var(--muted)]">交回改稿</span>
              <select value={reworkNode}
                onChange={(e) => setReworkNode(e.target.value)}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] max-w-[180px]">
                <option value="">选节点（留空=整轮重跑）</option>
                {reworkable.map((s) => (
                  <option key={s.node_key} value={s.node_key}>{s.node_name || s.node_key}</option>
                ))}
              </select>
              <input value={reworkText}
                onChange={(e) => setReworkText(e.target.value)}
                placeholder="只说哪里不对、改成什么（必填才能改稿）"
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] flex-1 min-w-[200px]" />
              <button type="button" onClick={() => void rework(reworkText.trim())}
                disabled={reworkBusy || !reworkText.trim() || !reworkNode}
                className="px-2 py-1 rounded text-[10px] bg-amber-700 hover:bg-amber-600 disabled:opacity-40">
                {reworkBusy ? "提交中…" : "交回该员工改"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* 反馈：低于 80 分自动生成下一版，把经验注入执行指令 */}
      {savedId && (
        <div className="mt-3 card-lg p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-[var(--muted)]">流程反馈</span>
            <input
              type="range"
              min={0}
              max={100}
              step={5}
              value={fbScore}
              onChange={(e) => setFbScore(Number(e.target.value))}
              className="w-36"
              aria-label="工作流评分"
            />
            <span className="tabular-nums text-xs w-8">{fbScore}</span>
            <input
              value={fbLessons}
              onChange={(e) => setFbLessons(e.target.value)}
              placeholder="经验教训，一行一条（可空）"
              className="input !py-1 text-xs flex-1 min-w-[180px]"
            />
            <button
              type="button"
              onClick={submitFeedback}
              disabled={fbBusy}
              className="btn-primary !py-1 !px-3 text-xs disabled:opacity-40 shrink-0"
            >
              {fbBusy ? "提交中…" : "提交反馈"}
            </button>
            <span className="text-[10px] text-[var(--muted)]">低于 80 分自动升一版</span>
          </div>

          {optimized && (
            <div className="text-[11px] rounded-md px-2 py-1 flex items-center gap-2 flex-wrap"
              style={{ background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.35)", color: "#6ee7b7" }}>
              已根据反馈生成 v{optimized.version}。
              <button
                type="button"
                onClick={() => void loadWorkflow(optimized.id)}
                className="underline"
              >
                载入新版
              </button>
            </div>
          )}
          {fbError && (
            <div className="text-[11px]" style={{ color: "#fca5a5" }}>{fbError}</div>
          )}

          {meta?.feedback && meta.feedback.length > 0 && (
            <div className="space-y-1">
              {meta.feedback.slice(0, 3).map((f) => (
                <div key={f.id} className="text-[10px] rounded-md px-2 py-1"
                  style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                  <span style={{ color: f.success ? "#6ee7b7" : "#fca5a5" }}>{f.score.toFixed(0)} 分</span>
                  <span className="text-[var(--muted)]">
                    {" "}· {f.source === "manual" ? "人工反馈" : "CEO 自动评分"} · {formatLocalStamp(f.created_at)}
                  </span>
                  {f.lessons.length > 0 && (
                    <span className="text-[var(--foreground)]"> — {f.lessons.join("；")}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 属性面板 */}
      {sel && (
        <div className="mt-3 card-lg p-3 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--muted)]">属性</span>
            <input value={sel.name}
              onChange={(e) => setNodes((ns) => ns.map((n) =>
                n.node_key === selected ? { ...n, name: e.target.value } : n))}
              className="bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-xs w-40" />
            {!["START", "END"].includes(sel.node_type) && (
              <button onClick={() => selected && deleteNode(selected)}
                className="text-xs bg-red-800 px-2 py-1 rounded">删除节点</button>
            )}
          </div>

          {sel.node_type === "AGENT" && (
            <>
              <select value={sel.config?.agent_id || ""}
                onChange={(e) => updateSelConfig({ agent_id: e.target.value })}
                className="w-56 bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-xs">
                <option value="">选择执行 Agent</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <input placeholder="给 Agent 的指令" value={sel.config?.instruction || ""}
                onChange={(e) => updateSelConfig({ instruction: e.target.value })}
                className="w-full bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-xs" />
            </>
          )}
          {sel.node_type === "CONDITION" && (
            <input placeholder="输出包含关键词走 yes 分支，否则 no" value={sel.config?.keyword || ""}
              onChange={(e) => updateSelConfig({ keyword: e.target.value })}
              className="w-full bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-xs" />
          )}
          {sel.node_type === "WAIT" && (
            <input type="number" placeholder="秒" value={sel.config?.seconds || 5}
              onChange={(e) => updateSelConfig({ seconds: Number(e.target.value) })}
              className="w-32 bg-white/[0.04] border border-[var(--border)] rounded px-2 py-1 text-xs" />
          )}
          {/* 条件节点出边标签编辑 */}
          {sel.node_type === "CONDITION" && edges.filter((ed) => ed.from_node === sel.node_key).length > 0 && (
            <div className="text-[10px] text-[var(--muted)] space-x-3">
              {edges.map((e, i) => e.from_node === sel.node_key ? (
                <span key={i} className="inline-flex items-center gap-1">
                  → {nodes.find((n) => n.node_key === e.to_node)?.name}
                  <input placeholder="yes/no" value={e.label || ""}
                    onChange={(ev) => setEdges((es) => es.map((x, j) =>
                      j === i ? { ...x, label: ev.target.value } : x))}
                    className="bg-white/[0.04] border border-[var(--border)] rounded px-1 py-0.5 w-16" />
                </span>
              ) : null)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
