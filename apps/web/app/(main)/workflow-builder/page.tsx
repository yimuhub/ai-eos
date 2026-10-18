"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/components/Toast";
import type { Company } from "@/lib/types";

type Node = {
  node_key: string; node_type: string; name: string;
  config: Record<string, any>; position_x: number; position_y: number;
};
type Edge = { from_node: string; to_node: string; label?: string | null };
type Agent = { id: string; name: string };

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
  const [agents, setAgents] = useState<Agent[]>([]);
  const [wfName, setName] = useState("新工作流");
  const [savedId, setSavedId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<string>("");
  const dragRef = useRef<{ key: string; dx: number; dy: number } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);  // 工作流运行轮询句柄
  const linkRef = useRef<{ from: string; x: number; y: number } | null>(null);
  const [linkPreview, setLinkPreview] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);

  useEffect(() => { api.get("/agents").then(setAgents).catch(() => {}); }, []);

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
    const res = await api.post(`/workflows?company_id=${cs[0].id}`, {
      name: wfName, nodes: nodes.map(({ node_key, node_type, name, config, position_x, position_y }) =>
        ({ node_key, node_type, name, config, position_x, position_y })),
      edges,
    });
    setSavedId(res.id);
    notify("success", `已保存：${res.name}`);
  }

  async function run() {
    if (!savedId) return;
    const res = await api.post(`/workflows/${savedId}/run`);
    setRunStatus("运行中…");
    // 句柄存 ref：离开页面要清掉，连点「运行」要先清旧的，避免叠加轮询
    if (pollRef.current) clearInterval(pollRef.current);
    let fails = 0;
    pollRef.current = setInterval(async () => {
      try {
        const ex = await api.get(`/executions/${res.execution_id}`);
        fails = 0;
        setRunStatus(`${ex.status}（${ex.steps.length} 步）`);
        if (["COMPLETED", "FAILED", "CANCELLED"].includes(ex.status)) {
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
